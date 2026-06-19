import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_async

from .database import get_setting, set_setting, add_claimed_game
from .notify import send_notification
from .state import state

logger = logging.getLogger(__name__)

# fortniteAndroidGameClient — used for device_auth CREATE (only client still active with this permission)
ANDROID_CLIENT_ID = "3f69e56c7649492c8cc29f1af08a8a12"
ANDROID_CLIENT_SECRET = "b51ee9cb12234f50a69efa67ef53812e"
ANDROID_AUTH = (ANDROID_CLIENT_ID, ANDROID_CLIENT_SECRET)

# launcherAppClient2 — used for auth code URL (web-friendly redirect) and checkout URL
LAUNCHER_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
LAUNCHER_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
LAUNCHER_AUTH = (LAUNCHER_CLIENT_ID, LAUNCHER_CLIENT_SECRET)

TOKEN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
EXCHANGE_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange"
DEVICE_AUTH_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{account_id}/deviceAuth"
FREE_GAMES_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"

AUTH_CODE_URL = (
    "https://www.epicgames.com/id/api/redirect"
    f"?clientId={LAUNCHER_CLIENT_ID}&responseType=code"
)

# Matches the Chromium version bundled in the playwright:v1.44.0 image (Chrome 125).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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

        ex_resp = await client.get(EXCHANGE_URL, headers={"Authorization": f"Bearer {launcher_token}"})
        if ex_resp.status_code != 200:
            raise RuntimeError(f"Failed to get exchange code: {ex_resp.text}")
        exchange_code = ex_resp.json()["code"]

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


async def _login_with_device_auth(client: httpx.AsyncClient) -> tuple[str, str]:
    account_id = await get_setting("device_account_id")
    device_id = await get_setting("device_id")
    device_secret = await get_setting("device_secret")

    resp = await _post_with_retry(
        client,
        TOKEN_URL,
        auth=ANDROID_AUTH,
        data={
            "grant_type": "device_auth",
            "account_id": account_id,
            "device_id": device_id,
            "secret": device_secret,
        },
    )
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("errorMessage") or data.get("error_description") or resp.text)

    return data["access_token"], data["account_id"]


async def is_connected() -> bool:
    return bool(await get_setting("device_account_id"))


async def get_free_games(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(FREE_GAMES_URL, params={"locale": "en-US", "country": "DE"})
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


def _generate_checkout_url(games: list[dict]) -> str:
    offers = "&".join(f"offers=1-{g['namespace']}-{g['id']}" for g in games)
    checkout = (
        f"https://store.epicgames.com/purchase"
        f"?highlightColor=0078f2&{offers}&orderId&purchaseToken&showNavigation=true"
    )
    return (
        f"https://www.epicgames.com/id/login"
        f"?noHostRedirect=true"
        f"&redirectUrl={quote(checkout, safe='')}"
        f"&client_id={LAUNCHER_CLIENT_ID}"
    )


async def _claim_with_browser(access_token: str, exchange_code: str, game: dict) -> bool | None:
    """Attempt to claim a free game using a headed Chromium via Xvfb.

    Running headed (headless=False) gives Chrome a real rendering context
    (WebGL via Mesa software renderer, canvas, proper browser chrome) so
    hCaptcha's fingerprinter scores it as a legitimate browser and often
    uses invisible challenge mode.

    Returns:
      True  — game claimed successfully
      None  — game already owned (no action needed)
      False — captcha went visual / timed out (caller should send notification fallback)
    """
    offer_url = (
        "https://store.epicgames.com/purchase"
        f"?highlightColor=0078f2"
        f"&offers=1-{game['namespace']}-{game['id']}"
        f"&orderId=&purchaseToken=&showNavigation=true"
    )

    async with async_playwright() as p:
        # headed=True — DISPLAY is set by xvfb-run in the container CMD.
        # No --disable-gpu so Chrome uses Mesa software rendering (better fingerprint).
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(
                locale="en-US",
                user_agent=_USER_AGENT,
            )
            page = await ctx.new_page()
            await stealth_async(page)

            def _log_req(request):
                url = request.url
                if "payment-website-pci.ol.epicgames.com" in url or "talon" in url:
                    logger.info("REQ  %s %s", request.method, url)
            page.on("request", _log_req)

            async def _log_api(response):
                url = response.url
                if any(skip in url for skip in ("static-assets-prod", "tracking.epicgames.com")):
                    return
                if any(url.lower().endswith(e) for e in (".js", ".css", ".woff2", ".woff", ".ico", ".png", ".svg")):
                    return
                try:
                    body = await response.text()
                    logger.info("API  %d %s %s → %s", response.status, response.request.method, url, body[:300])
                except Exception:
                    logger.info("API  %d %s", response.status, url)
            page.on("response", _log_api)

            # Exchange-code login — establishes XSRF-TOKEN + EPIC_SESSION_AP cookies.
            login_url = (
                f"https://www.epicgames.com/id/login/exchange"
                f"?exchangeCode={exchange_code}"
                f"&redirectUrl=https%3A%2F%2Fwww.epicgames.com%2F"
            )
            logger.info("Browser: establishing web session for %s", game["title"])
            await page.goto(login_url, wait_until="networkidle", timeout=30000)
            cookie_names = [c["name"] for c in await ctx.cookies()]
            logger.info("Browser: after exchange login — url=%s cookies=%s", page.url, cookie_names)

            # Inject bearer token; no epicCountry cookie so Epic detects DE from the IP.
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
                {
                    "name": "EPIC_SESSION_LOCALE",
                    "value": "en-US",
                    "domain": ".epicgames.com",
                    "path": "/",
                },
            ])

            logger.info("Browser: navigating to purchase page for %s", game["title"])
            nav = await page.goto(offer_url, wait_until="domcontentloaded", timeout=30000)
            logger.info("Browser: page loaded — status=%s url=%s", nav.status if nav else "?", page.url)

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(3000)

            async def _visible_button_texts() -> list[str]:
                texts = []
                for btn in await page.locator("button").all():
                    try:
                        if await btn.is_visible(timeout=300):
                            texts.append(repr((await btn.inner_text()).strip()))
                    except Exception:
                        pass
                return texts

            logger.info("Browser: visible buttons — %s", await _visible_button_texts())

            # ── Step 1: click "Add to library" / "Place Order" ──────────────────
            btn = None
            for sel in [
                "button:has-text('Add to library')",
                "button:has-text('Place Order')",
                "button:has-text('Confirm')",
                "button:has-text('Get')",
                "[data-testid='purchase-cta-button']",
                "button[data-component='PurchaseButton']",
                "button.btn-primary",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        btn = el
                        logger.info("Browser: found purchase button — %s", sel)
                        break
                except PlaywrightTimeout:
                    continue

            if not btn:
                body_text = (await page.locator("body").inner_text()).lower()
                if "already own" in body_text or "already in your library" in body_text:
                    logger.info("Browser: %s is already owned (no purchase button)", game["title"])
                    return None
                html_snippet = (await page.content())[:2000]
                logger.warning(
                    "Browser: no purchase button found for %s (url=%s). HTML: %s",
                    game["title"], page.url, html_snippet,
                )
                return False

            await btn.click()
            logger.info("Browser: clicked 'Add to library' for %s", game["title"])

            # With a headed browser, hCaptcha often resolves invisibly — the URL
            # hash leaves #/free-checkout once the order is placed.
            url_changed = False
            try:
                await page.wait_for_function(
                    "() => !window.location.href.includes('free-checkout')",
                    timeout=30000,
                )
                url_changed = True
            except PlaywrightTimeout:
                pass

            if url_changed:
                logger.info("Browser: claimed after step 1 for %s — url=%s", game["title"], page.url)
                return True

            logger.info("Browser: URL still on free-checkout after 30s — step 1 buttons: %s",
                        await _visible_button_texts())

            # ── Step 2: EU right-of-withdrawal consent (German IP) ───────────────
            for accept_sel in [
                "button:has-text('I accept')",
                "button:has-text('I Agree')",
                "button:has-text('I agree')",
                "button:has-text('Agree')",
            ]:
                try:
                    accept_btn = page.locator(accept_sel).first
                    if await accept_btn.is_visible(timeout=3000):
                        label = (await accept_btn.inner_text()).strip()
                        logger.info("Browser: clicking '%s' (EU consent) for %s", label, game["title"])
                        await accept_btn.click()

                        # Talon fires hCaptcha after this click.  With a headed
                        # browser the risk score is lower → invisible mode likely.
                        try:
                            await page.wait_for_function(
                                "() => !window.location.href.includes('free-checkout')",
                                timeout=30000,
                            )
                            logger.info("Browser: claimed after step 2 for %s — url=%s",
                                        game["title"], page.url)
                            return True
                        except PlaywrightTimeout:
                            logger.info("Browser: URL still on free-checkout after EU consent + 30s — "
                                        "buttons: %s", await _visible_button_texts())
                        break
                except PlaywrightTimeout:
                    continue

            # ── Final body-text heuristics ───────────────────────────────────────
            body_text = (await page.locator("body").inner_text()).lower()
            logger.info("Browser: final body text for %s: %.400s", game["title"], body_text)

            if "added to your library" in body_text or "added to library" in body_text:
                logger.info("Browser: body confirms success for %s", game["title"])
                return True
            if "already own" in body_text or "already in your library" in body_text:
                logger.info("Browser: %s is already owned", game["title"])
                return None
            if "add to library" not in body_text and "i accept" not in body_text:
                # Checkout UI is gone — assume order placed
                logger.info("Browser: checkout UI gone — assuming success for %s", game["title"])
                return True

            logger.warning(
                "Browser: hCaptcha went visual in headed mode — fallback to notification for %s (url=%s)",
                game["title"], page.url,
            )
            return False
        finally:
            await browser.close()


async def claim_game(client: httpx.AsyncClient, access_token: str, account_id: str, game: dict) -> bool | None:
    """Claim a single game via headed browser.

    Returns True (claimed), None (already owned), False (captcha blocked → use notification).
    """
    logger.info("Claiming: %s  offerId=%s namespace=%s", game["title"], game["id"], game["namespace"])
    ex_resp = await client.get(EXCHANGE_URL, headers={"Authorization": f"Bearer {access_token}"})
    if ex_resp.status_code != 200:
        logger.error("Exchange code request failed for %s: %s", game["title"], ex_resp.text)
        return False
    exchange_code = ex_resp.json()["code"]
    return await _claim_with_browser(access_token, exchange_code, game)


async def run_claim_job():
    logger.info("Claim job started at %s", datetime.now(timezone.utc).isoformat())
    state.last_run_status = "running"
    state.last_run_time = datetime.now(timezone.utc).isoformat()
    state.pending_checkout_url = ""
    state.pending_game_titles = []

    if not await is_connected():
        logger.warning("No account connected — skipping claim job")
        state.last_run_status = "failed"
        return

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

        claimed_titles: list[str] = []
        failed_games: list[dict] = []

        for game in free_games:
            result = await claim_game(client, access_token, account_id, game)
            if result is True:
                logger.info("Claimed: %s", game["title"])
                await add_claimed_game(game["title"], game.get("cover_url"), game["id"])
                claimed_titles.append(game["title"])
            elif result is None:
                logger.info("Already owned: %s", game["title"])
            else:
                logger.warning("Auto-claim failed (captcha blocked): %s — queuing notification", game["title"])
                failed_games.append(game)

    state.last_run_status = "success"
    logger.info("Claim job complete. Claimed: %s. Failed: %s.",
                claimed_titles or "none",
                [g["title"] for g in failed_games] or "none")

    if claimed_titles:
        await _notify(f"Claimed {len(claimed_titles)} free Epic game(s): {', '.join(claimed_titles)}")

    if failed_games:
        checkout_url = _generate_checkout_url(failed_games)
        titles_str = ", ".join(g["title"] for g in failed_games)
        state.pending_checkout_url = checkout_url
        state.pending_game_titles = [g["title"] for g in failed_games]
        await _notify(
            f"Auto-claim failed (hCaptcha visual) for: {titles_str}\n"
            f"Click to claim manually:\n{checkout_url}"
        )


async def _notify(message: str):
    url = await get_setting("notify_url")
    webhook_type = await get_setting("notify_type")
    if url and webhook_type:
        await send_notification(message, url, webhook_type)
