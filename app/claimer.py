import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .database import get_setting, set_setting, add_claimed_game
from .notify import send_notification
from .state import state

logger = logging.getLogger(__name__)

# fortniteAndroidGameClient — used for device_auth CREATE (only client still active with this permission)
ANDROID_CLIENT_ID = "3f69e56c7649492c8cc29f1af08a8a12"
ANDROID_CLIENT_SECRET = "b51ee9cb12234f50a69efa67ef53812e"
ANDROID_AUTH = (ANDROID_CLIENT_ID, ANDROID_CLIENT_SECRET)

# launcherAppClient2 — used for auth code URL (web-friendly redirect) and claiming free games
LAUNCHER_CLIENT_ID = os.environ.get("EPIC_CLIENT_ID", "34a02cf8f4414e29b15921876da36f9a")
LAUNCHER_CLIENT_SECRET = os.environ.get("EPIC_CLIENT_SECRET", "daafbccc737745039dffe53d94fc76cf")
LAUNCHER_AUTH = (LAUNCHER_CLIENT_ID, LAUNCHER_CLIENT_SECRET)

TOKEN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
EXCHANGE_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange"
DEVICE_AUTH_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{account_id}/deviceAuth"
FREE_GAMES_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
ORDER_URL = "https://payment-website-pci.ol.epicgames.com/purchase"

# Use launcher client for the auth URL — its redirect is web-based, not a mobile deep link
AUTH_CODE_URL = (
    "https://www.epicgames.com/id/api/redirect"
    f"?clientId={LAUNCHER_CLIENT_ID}&responseType=code"
)


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    for attempt in range(3):
        try:
            return await client.post(url, **kwargs)
        except httpx.RequestError as e:
            if attempt == 2:
                raise
            logger.warning("Network error (attempt %d/3): %s", attempt + 1, e)
            await asyncio.sleep(30)


async def connect_with_auth_code(auth_code: str) -> dict:
    """One-time setup: exchange launcher auth code -> get exchange code -> get Android
    token -> create device_auth (Android client has the CREATE permission)."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: exchange the launcher auth code for a launcher token
        resp = await _post_with_retry(
            client,
            TOKEN_URL,
            auth=LAUNCHER_AUTH,
            data={"grant_type": "authorization_code", "code": auth_code},
        )
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(data.get("errorMessage") or data.get("error_description") or resp.text)

        launcher_token = data["access_token"]

        # Step 2: get an exchange code from the launcher session
        ex_resp = await client.get(EXCHANGE_URL, headers={"Authorization": f"Bearer {launcher_token}"})
        if ex_resp.status_code != 200:
            raise RuntimeError(f"Failed to get exchange code: {ex_resp.text}")
        exchange_code = ex_resp.json()["code"]

        # Step 3: exchange for an Android client token (has device_auth CREATE permission)
        android_resp = await _post_with_retry(
            client,
            TOKEN_URL,
            auth=ANDROID_AUTH,
            data={"grant_type": "exchange_code", "exchange_code": exchange_code},
        )
        android_data = android_resp.json()
        if android_resp.status_code != 200:
            raise RuntimeError(android_data.get("errorMessage") or android_data.get("error_description") or android_resp.text)

        android_token = android_data["access_token"]
        account_id = android_data["account_id"]

        # Step 4: create device_auth credentials (permanent, never expire)
        da_resp = await client.post(
            DEVICE_AUTH_URL.format(account_id=account_id),
            headers={"Authorization": f"Bearer {android_token}"},
        )
        if da_resp.status_code != 200:
            raise RuntimeError(f"Failed to create device auth: {da_resp.text}")

        da = da_resp.json()
        await set_setting("device_account_id", da["accountId"])
        await set_setting("device_id", da["deviceId"])
        await set_setting("device_secret", da["secret"])

        logger.info("Account connected and device auth credentials stored.")
        return data


async def _login_with_device_auth(client: httpx.AsyncClient) -> str:
    """Log in via device_auth (iOS client) then exchange for a launcher token.
    Returns a launcher access_token with freePurchase permission."""
    account_id = await get_setting("device_account_id")
    device_id = await get_setting("device_id")
    secret = await get_setting("device_secret")

    if not all([account_id, device_id, secret]):
        raise RuntimeError("No device auth credentials stored — connect your account first.")

    # Step 1: device_auth login with Android client
    resp = await _post_with_retry(
        client,
        TOKEN_URL,
        auth=ANDROID_AUTH,
        data={
            "grant_type": "device_auth",
            "account_id": account_id,
            "device_id": device_id,
            "secret": secret,
        },
    )
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("errorMessage") or data.get("error_description") or resp.text)

    ios_token = data["access_token"]

    # Step 2: get an exchange code from the iOS session
    ex_resp = await client.get(EXCHANGE_URL, headers={"Authorization": f"Bearer {ios_token}"})
    if ex_resp.status_code != 200:
        raise RuntimeError(f"Failed to get exchange code: {ex_resp.text}")
    exchange_code = ex_resp.json()["code"]

    # Step 3: exchange for a launcher token (has freePurchase permission)
    launcher_resp = await _post_with_retry(
        client,
        TOKEN_URL,
        auth=LAUNCHER_AUTH,
        data={"grant_type": "exchange_code", "exchange_code": exchange_code},
    )
    launcher_data = launcher_resp.json()
    if launcher_resp.status_code != 200:
        raise RuntimeError(launcher_data.get("errorMessage") or launcher_data.get("error_description") or launcher_resp.text)

    return launcher_data["access_token"], launcher_data["account_id"]


async def is_connected() -> bool:
    return bool(await get_setting("device_account_id"))


async def get_free_games(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(FREE_GAMES_URL, params={"locale": "en-US", "country": "US"})
    resp.raise_for_status()
    elements = resp.json()["data"]["Catalog"]["searchStore"]["elements"]
    logger.info("Free games API returned %d elements", len(elements))

    free = []
    for el in elements:
        title = el.get("title", "Unknown")
        promotions = el.get("promotions") or {}
        offer_groups = promotions.get("promotionalOffers") or []

        if not offer_groups:
            continue

        for offer_group in offer_groups:
            for offer in offer_group.get("promotionalOffers", []):
                discount = offer.get("discountSetting", {}).get("discountPercentage")
                logger.debug("  %s — discountPercentage: %s", title, discount)
                if discount == 0:
                    cover = next(
                        (img["url"] for img in el.get("keyImages", []) if img.get("type") == "Thumbnail"),
                        None,
                    )
                    free.append({
                        "id": el.get("id") or el.get("productSlug", ""),
                        "namespace": el.get("namespace", ""),
                        "title": title,
                        "cover_url": cover,
                    })
                    logger.info("  Found free game: %s (id=%s namespace=%s)", title, el.get("id"), el.get("namespace"))

    logger.info("Found %d free game(s) to claim", len(free))
    return free


async def _claim_with_browser(access_token: str, game: dict) -> bool:
    """Use headless Chromium (Playwright) to click through the Epic Store checkout.

    A real browser is required because:
    - store.epicgames.com is behind Cloudflare bot protection (blocks plain HTTP)
    - The payment endpoint needs a purchaseToken generated by the store's JS
    """
    offer_url = (
        "https://store.epicgames.com/purchase"
        f"?highlightColor=0078f2"
        f"&offers=1-{game['namespace']}-{game['id']}"
        f"&orderId=&purchaseToken=&showNavigation=true"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            ctx = await browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            # Inject the launcher OAuth token as the Epic web bearer cookie
            await ctx.add_cookies([
                {
                    "name": "EPIC_BEARER_TOKEN",
                    "value": access_token,
                    "domain": ".epicgames.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "None",
                },
                {"name": "EPIC_SESSION_LOCALE", "value": "en-US", "domain": ".epicgames.com", "path": "/"},
                {"name": "epicCountry", "value": "US", "domain": ".epicgames.com", "path": "/"},
            ])

            page = await ctx.new_page()
            page.on("console", lambda m: logger.debug("browser[%s] %s", m.type, m.text) if m.type == "error" else None)

            logger.info("Browser: navigating to purchase page for %s", game["title"])
            nav = await page.goto(offer_url, wait_until="domcontentloaded", timeout=30000)
            logger.info("Browser: page loaded — status=%s url=%s", nav.status if nav else "?", page.url)

            # Wait for the React SPA to finish its API calls, then give it a moment to render
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(3000)

            # Log every visible button so we can see what the page is offering
            try:
                all_buttons = await page.locator("button").all()
                visible = []
                for b in all_buttons:
                    try:
                        if await b.is_visible(timeout=300):
                            visible.append(repr((await b.inner_text()).strip()))
                    except Exception:
                        pass
                logger.info("Browser: visible buttons — %s", visible)
            except Exception as e:
                logger.debug("Browser: button enumeration error: %s", e)

            # Find and click the purchase/confirm button
            btn = None
            for sel in [
                "button:has-text('Add to library')",
                "button:has-text('Place Order')",
                "button:has-text('Order')",
                "button:has-text('Confirm')",
                "button:has-text('Get')",
                "button:has-text('Check Out')",
                "button:has-text('Continue')",
                "[data-testid='purchase-cta-button']",
                "[data-testid='confirm-btn']",
                "button[data-component='PurchaseButton']",
                "button.btn-primary",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        btn = el
                        logger.info("Browser: found button via selector: %s", sel)
                        break
                except PlaywrightTimeout:
                    continue

            if not btn:
                html = await page.content()
                logger.warning(
                    "Browser: no purchase button found for %s (url=%s). Page HTML start: %s",
                    game["title"], page.url, html[:3000],
                )
                return False

            await btn.click()
            logger.info("Browser: clicked purchase button for %s", game["title"])

            # Wait for the page to react to the click (button disappears or URL changes)
            try:
                await page.wait_for_function(
                    "() => !document.body.innerText.toLowerCase().includes('add to library')",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                pass

            await page.wait_for_timeout(2000)

            # Log buttons and URL after click to see the resulting state
            try:
                post_buttons = await page.locator("button").all()
                post_visible = []
                for b in post_buttons:
                    try:
                        if await b.is_visible(timeout=300):
                            post_visible.append(repr((await b.inner_text()).strip()))
                    except Exception:
                        pass
                logger.info("Browser: post-click url=%s buttons=%s", page.url, post_visible)
            except Exception as e:
                logger.debug("Browser: post-click enumeration error: %s", e)

            # Check if already confirmed via URL or page text
            html = await page.content()
            low = html.lower()

            success_phrases = ["thank you", "your order", "order confirmed", "added to library", "successfully added"]
            already_phrases = ["already own", "already_purchased", "already in your library"]

            if any(p in low for p in success_phrases):
                logger.info("Browser: order confirmed for %s", game["title"])
                return True
            if any(p in low for p in already_phrases):
                logger.info("Browser: %s is already owned", game["title"])
                return False

            # Wait a bit longer for confirmation UI
            try:
                await page.wait_for_selector(
                    ":text('Thank you'), :text('Your order'), :text('Order confirmed'), "
                    ":text('Added to library'), :text('Successfully added'), :text('Success')",
                    timeout=10000,
                )
                logger.info("Browser: order confirmed for %s", game["title"])
                return True
            except PlaywrightTimeout:
                html = await page.content()
                logger.warning(
                    "Browser: confirmation not detected for %s (url=%s). Post-click HTML start: %s",
                    game["title"], page.url, html[:3000],
                )
                return False
        finally:
            await browser.close()


async def claim_game(client: httpx.AsyncClient, access_token: str, account_id: str, game: dict) -> bool:
    logger.info("Claiming %s — offerId=%s namespace=%s", game["title"], game["id"], game["namespace"])
    return await _claim_with_browser(access_token, game)


async def run_claim_job():
    logger.info("Claim job started at %s", datetime.now(timezone.utc).isoformat())
    state.last_run_status = "running"
    state.last_run_time = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            access_token, account_id = await _login_with_device_auth(client)
        except RuntimeError as e:
            logger.error("Login error: %s", e)
            state.last_run_status = "failed"
            await _notify(f"Epic Games login failed: {e}")
            return

        try:
            free_games = await get_free_games(client)
        except Exception as e:
            logger.error("Failed to fetch free games: %s", e)
            state.last_run_status = "failed"
            return

        if not free_games:
            logger.info("No free games available this week.")
            state.last_run_status = "success"
            return

        claimed_count = 0
        for game in free_games:
            if await claim_game(client, access_token, account_id, game):
                logger.info("Claimed: %s", game["title"])
                await add_claimed_game(game["title"], game.get("cover_url"), game["id"])
                claimed_count += 1
            else:
                logger.info("Already owned: %s", game["title"])

        state.last_run_status = "success"
        logger.info("Claim job complete. Claimed %d new game(s).", claimed_count)
        if claimed_count:
            titles = ", ".join(g["title"] for g in free_games)
            await _notify(f"Claimed {claimed_count} free Epic game(s): {titles}")


async def _notify(message: str):
    url = await get_setting("notify_url")
    webhook_type = await get_setting("notify_type")
    if url and webhook_type:
        await send_notification(message, url, webhook_type)
