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


async def _claim_with_browser(access_token: str, exchange_code: str, game: dict) -> bool:
    """Use headless Chromium (Playwright) to click through the Epic Store checkout.

    A real browser is required because store.epicgames.com is behind Cloudflare.
    We log in via the exchange code so Playwright gets a full web session — the
    launcher Bearer token alone is enough to render the page but not to place orders.
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

            page = await ctx.new_page()
            page.on("console", lambda m: logger.debug("browser[%s] %s", m.type, m.text) if m.type == "error" else None)

            # Log all outgoing requests to the payment backend (captures POST bodies)
            def _log_req(request):
                if "payment-website-pci.ol.epicgames.com" in request.url:
                    body = request.post_data or ""
                    logger.info("REQ %s %s body=%s", request.method, request.url, body[:300])
            page.on("request", _log_req)

            # Log all non-static API responses (skip JS/CSS/font/image assets)
            async def _log_api(response):
                url = response.url
                if "static-assets-prod" in url:
                    return
                if any(url.lower().endswith(e) for e in (".js", ".css", ".woff2", ".woff", ".ico", ".png")):
                    return
                if "tracking.epicgames.com" in url:
                    return
                try:
                    body = await response.text()
                    logger.info("API %s %s → %d: %s",
                                response.request.method, url, response.status, body[:400])
                except Exception:
                    logger.info("API %s → %d", url, response.status)
            page.on("response", _log_api)

            # Step 1: exchange-code login to www (gets XSRF-TOKEN + EPIC_SESSION_AP)
            # Redirect to www, NOT store — navigating to store first triggers a CF
            # re-challenge on the /purchase path.
            login_url = (
                f"https://www.epicgames.com/id/login/exchange"
                f"?exchangeCode={exchange_code}"
                f"&redirectUrl=https%3A%2F%2Fwww.epicgames.com%2F"
            )
            logger.info("Browser: establishing web session for %s", game["title"])
            await page.goto(login_url, wait_until="networkidle", timeout=30000)
            cookie_map = {c["name"]: c["value"] for c in await ctx.cookies()}
            logger.info("Browser: after exchange login — url=%s cookies=%s", page.url, list(cookie_map.keys()))

            # Step 2: inject EPIC_BEARER_TOKEN (needed to render the checkout page)
            # + locale helpers. XSRF-TOKEN and EPIC_SESSION_AP are already set.
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
            logger.info("Browser: step 1 — clicked Add to library for %s", game["title"])
            await page.wait_for_timeout(2000)

            # Step 2: click "I accept" if an age/EULA dialog appeared
            accepted = False
            for accept_sel in [
                "button:has-text('I accept')",
                "button:has-text('Accept')",
                "button:has-text('Agree')",
                "button:has-text('I Agree')",
            ]:
                try:
                    accept_btn = page.locator(accept_sel).first
                    if await accept_btn.is_visible(timeout=2000):
                        logger.info("Browser: step 2 — accepting terms dialog for %s", game["title"])
                        await accept_btn.click()
                        accepted = True
                        # After EULA accept the React app re-calls /initialize and
                        # /order-preview before the button becomes interactive.
                        # Wait for that round-trip before touching anything.
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except PlaywrightTimeout:
                            pass
                        await page.wait_for_timeout(3000)

                        # Log button state after EULA to see what's available
                        _btns = await page.locator("button").all()
                        _vis = []
                        for _b in _btns:
                            try:
                                if await _b.is_visible(timeout=300):
                                    _vis.append(repr((await _b.inner_text()).strip()))
                            except Exception:
                                pass
                        logger.info("Browser: buttons after EULA accept — %s", _vis)
                        break
                except PlaywrightTimeout:
                    continue

            # Step 3: click Add to library again after EULA
            if accepted:
                try:
                    add_again = page.locator("button:has-text('Add to library')").first
                    await add_again.wait_for(state="visible", timeout=10000)
                    await add_again.scroll_into_view_if_needed()
                    logger.info("Browser: step 3 — clicking Add to library again after EULA for %s", game["title"])
                    await add_again.click()
                    # Wait for the place-order API call to complete
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except PlaywrightTimeout:
                        pass
                    await page.wait_for_timeout(3000)

                    # Log button state after step 3 to see what changed
                    _btns = await page.locator("button").all()
                    _vis = []
                    for _b in _btns:
                        try:
                            if await _b.is_visible(timeout=300):
                                _vis.append(repr((await _b.inner_text()).strip()))
                        except Exception:
                            pass
                    logger.info("Browser: buttons after step 3 click — %s", _vis)
                except PlaywrightTimeout:
                    logger.warning("Browser: step 3 button not found/visible for %s", game["title"])

            # Wait for URL hash to leave /free-checkout (primary success signal)
            try:
                await page.wait_for_function(
                    "() => !window.location.href.includes('free-checkout')",
                    timeout=20000,
                )
                logger.info("Browser: checkout complete for %s — url=%s", game["title"], page.url)
                return True
            except PlaywrightTimeout:
                pass

            body_text = (await page.locator("body").inner_text()).lower()
            logger.info("Browser: final body text for %s: %s", game["title"], body_text[:500])

            if "added to your library" in body_text or "added to library" in body_text:
                logger.info("Browser: confirmed added to library for %s", game["title"])
                return True
            if "already own" in body_text or "already in your library" in body_text:
                logger.info("Browser: %s is already owned", game["title"])
                return False
            if "add to library" not in body_text and "i accept" not in body_text:
                logger.info("Browser: checkout UI gone — assuming success for %s", game["title"])
                return True

            logger.warning(
                "Browser: confirmation not detected for %s (url=%s). Body text: %s",
                game["title"], page.url, body_text[:1000],
            )
            return False
        finally:
            await browser.close()


async def claim_game(client: httpx.AsyncClient, access_token: str, account_id: str, game: dict) -> bool:
    logger.info("Claiming %s — offerId=%s namespace=%s", game["title"], game["id"], game["namespace"])
    # Exchange code is one-time-use and short-lived — request it right before the browser opens.
    ex_resp = await client.get(EXCHANGE_URL, headers={"Authorization": f"Bearer {access_token}"})
    if ex_resp.status_code != 200:
        logger.error("Failed to get exchange code for browser login: %s", ex_resp.text)
        return False
    exchange_code = ex_resp.json()["code"]
    return await _claim_with_browser(access_token, exchange_code, game)


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
