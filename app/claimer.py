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

ANDROID_CLIENT_ID = "3f69e56c7649492c8cc29f1af08a8a12"
ANDROID_CLIENT_SECRET = "b51ee9cb12234f50a69efa67ef53812e"
ANDROID_AUTH = (ANDROID_CLIENT_ID, ANDROID_CLIENT_SECRET)

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

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Epic's checkout uses hcaptcha with render=explicit.  This script intercepts
# window.hcaptcha.render() to capture the onverify callback so we can fire it
# with a CapSolver-solved token without needing user interaction.
_HCAPTCHA_INTERCEPT_SCRIPT = """
(function () {
    window.__hcaptchaCallback = null;

    function installProxy() {
        if (!window.hcaptcha) return false;
        var _origRender = window.hcaptcha.render;
        window.hcaptcha.render = function (container, params) {
            if (params) {
                var cb = params.callback;
                if (typeof cb === 'function') {
                    window.__hcaptchaCallback = cb;
                } else if (typeof cb === 'string' && window[cb]) {
                    window.__hcaptchaCallback = window[cb];
                }
            }
            return _origRender.call(this, container, params);
        };
        return true;
    }

    var poll = setInterval(function () {
        if (installProxy()) clearInterval(poll);
    }, 50);
})();
"""

CAPSOLVER_URL = "https://api.capsolver.com"
EPIC_HCAPTCHA_SITEKEY = "86194cdd-0462-4873-8866-05a00840a83a"
EPIC_PURCHASE_URL = "https://store.epicgames.com/purchase"


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
    """One-time setup: launcher auth code → exchange code → Android token → device_auth."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _post_with_retry(
            client, TOKEN_URL,
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
            client, TOKEN_URL,
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
        client, TOKEN_URL,
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
        for offer_group in (promotions.get("promotionalOffers") or []):
            for offer in offer_group.get("promotionalOffers", []):
                if offer.get("discountSetting", {}).get("discountPercentage") == 0:
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
                    logger.info("  Free: %s (id=%s ns=%s)", title, el.get("id"), el.get("namespace"))
    logger.info("Found %d free game(s)", len(free))
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


async def _solve_with_capsolver(api_key: str, rq_data: str) -> str:
    """Submit an hCaptcha task to CapSolver and wait for the token."""
    task: dict = {
        "type": "HCaptchaTaskProxyless",
        "websiteURL": EPIC_PURCHASE_URL,
        "websiteKey": EPIC_HCAPTCHA_SITEKEY,
        "userAgent": _USER_AGENT,
        "isEnterprise": True,
        "enterprisePayload": {"rqdata": rq_data},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        create_resp = await client.post(
            f"{CAPSOLVER_URL}/createTask",
            json={"clientKey": api_key, "task": task},
        )
        created = create_resp.json()
        if created.get("errorId", 1) != 0:
            raise RuntimeError(
                f"CapSolver createTask: {created.get('errorCode')} — {created.get('errorDescription')}"
            )
        task_id = created["taskId"]
        logger.info("CapSolver: task %s created (rqdata=%s)", task_id, bool(rq_data))

        for _ in range(100):  # poll up to ~5 minutes
            await asyncio.sleep(3)
            res = (await client.post(
                f"{CAPSOLVER_URL}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )).json()

            status = res.get("status")
            if status == "ready":
                token = res["solution"]["gRecaptchaResponse"]
                logger.info("CapSolver: solved (token %d chars)", len(token))
                return token
            if status != "processing":
                raise RuntimeError(f"CapSolver unexpected result: {res}")

    raise RuntimeError("CapSolver: timed out after 5 minutes")


async def _claim_with_browser(
    access_token: str,
    exchange_code: str,
    game: dict,
    capsolver_key: str | None,
) -> bool | None:
    """Claim a free game via headless Chromium + CapSolver for captcha.

    Returns:
      True  — claimed
      None  — already owned
      False — failed (captcha blocked without CapSolver, or CapSolver error)
    """
    offer_url = (
        f"https://store.epicgames.com/purchase"
        f"?highlightColor=0078f2"
        f"&offers=1-{game['namespace']}-{game['id']}"
        f"&orderId=&purchaseToken=&showNavigation=true"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(locale="en-US", user_agent=_USER_AGENT)
            page = await ctx.new_page()
            await stealth_async(page)
            # Intercept hcaptcha.render to capture Epic's onverify callback
            await page.add_init_script(_HCAPTCHA_INTERCEPT_SCRIPT)

            # Passively capture Talon rqdata from network (needed for CapSolver)
            captured_rqdata: dict[str, str | None] = {"value": None}

            async def _on_response(response):
                if "talon-service-prod.ecosec.on.epicgames.com/v1/init/execute" in response.url:
                    try:
                        data = await response.json()
                        rq = data.get("h_captcha", {}).get("data")
                        if rq:
                            captured_rqdata["value"] = rq
                            logger.info("Talon rqdata captured (%d chars)", len(rq))
                    except Exception:
                        pass
                elif "payment-website-pci.ol.epicgames.com" in response.url or "talon" in response.url:
                    logger.info("API  %d %s %s", response.status, response.request.method, response.url)

            page.on("response", _on_response)

            # ── Exchange-code login ──────────────────────────────────────────────
            login_url = (
                f"https://www.epicgames.com/id/login/exchange"
                f"?exchangeCode={exchange_code}"
                f"&redirectUrl=https%3A%2F%2Fwww.epicgames.com%2F"
            )
            logger.info("Browser: session login for %s", game["title"])
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            cookie_names = [c["name"] for c in await ctx.cookies()]
            logger.info("Browser: cookies after login — %s", cookie_names)

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

            # ── Navigate to purchase page ────────────────────────────────────────
            logger.info("Browser: navigating to purchase page for %s", game["title"])
            await page.goto(offer_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(3000)

            async def _visible_buttons() -> list[str]:
                texts = []
                for btn in await page.locator("button").all():
                    try:
                        if await btn.is_visible(timeout=300):
                            texts.append(repr((await btn.inner_text()).strip()))
                    except Exception:
                        pass
                return texts

            logger.info("Browser: buttons — %s", await _visible_buttons())

            # ── Step 1: find and click Add to library ────────────────────────────
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
                        logger.info("Browser: found button — %s", sel)
                        break
                except PlaywrightTimeout:
                    continue

            if not btn:
                body = (await page.locator("body").inner_text()).lower()
                if "already own" in body or "already in your library" in body:
                    logger.info("Browser: %s already owned", game["title"])
                    return None
                logger.warning("Browser: no purchase button for %s (url=%s)", game["title"], page.url)
                return False

            await btn.click()
            logger.info("Browser: clicked 'Add to library' for %s", game["title"])

            # Quick check — no EU dialog, invisible captcha may have passed
            try:
                await page.wait_for_function(
                    "() => !window.location.href.includes('free-checkout')",
                    timeout=5000,
                )
                logger.info("Browser: claimed immediately for %s (no EU dialog)", game["title"])
                return True
            except PlaywrightTimeout:
                pass

            # ── Step 2: EU right-of-withdrawal consent ───────────────────────────
            clicked_accept = False
            for accept_sel in [
                "button:has-text('I accept')",
                "button:has-text('I Agree')",
                "button:has-text('I agree')",
                "button:has-text('Agree')",
            ]:
                try:
                    accept_btn = page.locator(accept_sel).first
                    await accept_btn.wait_for(state="visible", timeout=30000)
                    label = (await accept_btn.inner_text()).strip()
                    await accept_btn.click()
                    logger.info("Browser: clicked '%s' (EU consent) for %s", label, game["title"])
                    clicked_accept = True
                    break
                except PlaywrightTimeout:
                    continue

            if not clicked_accept:
                logger.warning("Browser: EU consent button not found for %s", game["title"])

            # Give invisible captcha a chance to auto-pass (15 s)
            try:
                await page.wait_for_function(
                    "() => !window.location.href.includes('free-checkout')",
                    timeout=15000,
                )
                logger.info("Browser: claimed (invisible captcha) for %s", game["title"])
                return True
            except PlaywrightTimeout:
                pass

            # ── Step 3: visual captcha — use CapSolver ───────────────────────────
            if not capsolver_key:
                logger.warning(
                    "Browser: captcha went visual for %s — no CapSolver key, falling back to notification",
                    game["title"],
                )
                return False

            # Wait for rqdata from Talon's response handler (required for Epic's enterprise hCaptcha)
            for _ in range(8):
                if captured_rqdata["value"]:
                    break
                await page.wait_for_timeout(500)
            rq_data = captured_rqdata["value"]

            if not rq_data:
                logger.warning(
                    "Browser: Talon rqdata not captured for %s — cannot solve enterprise hCaptcha, falling back to notification",
                    game["title"],
                )
                return False

            logger.info("Browser: solving with CapSolver (rqdata=%d chars)...", len(rq_data))
            try:
                token = await _solve_with_capsolver(capsolver_key, rq_data)
            except RuntimeError as e:
                logger.error("CapSolver failed for %s: %s", game["title"], e)
                return False

            # Inject the solved token: set the hidden textarea and fire Epic's callback
            await page.evaluate("""
                (token) => {
                    var area = document.querySelector('textarea[name="h-captcha-response"]');
                    if (area) {
                        var setter = Object.getOwnPropertyDescriptor(
                            HTMLTextAreaElement.prototype, 'value'
                        ).set;
                        setter.call(area, token);
                        area.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    if (window.__hcaptchaCallback) {
                        window.__hcaptchaCallback(token);
                    }
                }
            """, token)
            logger.info("Browser: token injected for %s", game["title"])

            try:
                await page.wait_for_function(
                    "() => !window.location.href.includes('free-checkout')",
                    timeout=30000,
                )
                logger.info("Browser: claimed (CapSolver) for %s — url=%s", game["title"], page.url)
                return True
            except PlaywrightTimeout:
                body = (await page.locator("body").inner_text()).lower()
                logger.warning(
                    "Browser: token injected but URL unchanged for %s. body=%.300s",
                    game["title"], body,
                )
                return False

        finally:
            await browser.close()


async def claim_game(
    client: httpx.AsyncClient, access_token: str, account_id: str, game: dict
) -> bool | None:
    """Returns True (claimed), None (already owned), False (failed → notification fallback)."""
    logger.info("Claiming: %s  id=%s ns=%s", game["title"], game["id"], game["namespace"])
    ex_resp = await client.get(EXCHANGE_URL, headers={"Authorization": f"Bearer {access_token}"})
    if ex_resp.status_code != 200:
        logger.error("Exchange code failed for %s: %s", game["title"], ex_resp.text)
        return False
    exchange_code = ex_resp.json()["code"]
    capsolver_key = (await get_setting("capsolver_key")) or None
    return await _claim_with_browser(access_token, exchange_code, game, capsolver_key)


async def run_claim_job():
    logger.info("Claim job started at %s", datetime.now(timezone.utc).isoformat())
    state.last_run_status = "running"
    state.last_run_time = datetime.now(timezone.utc).isoformat()
    state.pending_checkout_url = ""
    state.pending_game_titles = []

    if not await is_connected():
        logger.warning("No account connected — skipping")
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
            logger.info("No free games this week.")
            state.last_run_status = "success"
            return

        claimed_titles: list[str] = []
        failed_games: list[dict] = []

        for game in free_games:
            result = await claim_game(client, access_token, account_id, game)
            if result is True:
                await add_claimed_game(game["title"], game.get("cover_url"), game["id"])
                claimed_titles.append(game["title"])
            elif result is None:
                logger.info("Already owned: %s", game["title"])
            else:
                failed_games.append(game)

    state.last_run_status = "success"
    logger.info("Done. Claimed=%s Failed=%s",
                claimed_titles or "none", [g["title"] for g in failed_games] or "none")

    if claimed_titles:
        await _notify(f"Claimed {len(claimed_titles)} free game(s): {', '.join(claimed_titles)}")

    if failed_games:
        checkout_url = _generate_checkout_url(failed_games)
        titles_str = ", ".join(g["title"] for g in failed_games)
        state.pending_checkout_url = checkout_url
        state.pending_game_titles = [g["title"] for g in failed_games]
        capsolver_key = await get_setting("capsolver_key")
        if capsolver_key:
            await _notify(
                f"CapSolver failed to auto-claim: {titles_str}\n"
                f"Claim manually:\n{checkout_url}"
            )
        else:
            await _notify(
                f"Free Epic game(s) available: {titles_str}\n"
                f"Claim here (add a CapSolver key in Settings for auto-claiming):\n{checkout_url}"
            )


async def _notify(message: str):
    url = await get_setting("notify_url")
    webhook_type = await get_setting("notify_type")
    if url and webhook_type:
        await send_notification(message, url, webhook_type)
