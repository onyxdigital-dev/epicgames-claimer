import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from .database import get_setting, set_setting, add_claimed_game, get_claimed_games
from .notify import send_notification
from .state import state

logger = logging.getLogger(__name__)

# Epic Games OAuth client credentials — public launcher credentials embedded in
# the Epic Games Launcher app. Configurable via env vars in case Epic rotates them.
# Find current values at: https://github.com/Tectors/EpicGamesAPIDocs
import os as _os
EPIC_CLIENT_ID = _os.environ.get("EPIC_CLIENT_ID", "34a02cf8f4414e29b15921876da36f9a")
EPIC_CLIENT_SECRET = _os.environ.get("EPIC_CLIENT_SECRET", "daafbccc737745039dffe53d94fc76cf")
EPIC_AUTH = (EPIC_CLIENT_ID, EPIC_CLIENT_SECRET)

LOGIN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
FREE_GAMES_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
ORDER_URL = "https://payment-website-pci.ol.epicgames.com/purchase"
ENTITLEMENT_URL = "https://entitlement-public-service-prod08.ol.epicgames.com/entitlement/api/account/{account_id}/entitlements"

TWOFA_TIMEOUT = 600  # 10 minutes


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    for attempt in range(3):
        try:
            resp = await client.post(url, **kwargs)
            return resp
        except httpx.RequestError as e:
            if attempt == 2:
                raise
            logger.warning("Network error (attempt %d/3): %s", attempt + 1, e)
            await asyncio.sleep(30)


async def login(client: httpx.AsyncClient, email: str, password: str) -> tuple[str, dict]:
    """Returns ("ok", data) or ("2fa_required", data)."""
    resp = await _post_with_retry(
        client,
        LOGIN_URL,
        auth=EPIC_AUTH,
        data={
            "grant_type": "password",
            "username": email,
            "password": password,
            "includePerms": "false",
        },
    )
    data = resp.json()

    if resp.status_code == 200:
        return "ok", data

    error = data.get("errorCode", "")
    if "mfa" in error or data.get("continuation_token"):
        return "2fa_required", data

    raise RuntimeError(f"Login failed: {data.get('errorMessage', resp.text)}")


async def submit_2fa(client: httpx.AsyncClient, continuation_token: str, code: str) -> dict:
    resp = await _post_with_retry(
        client,
        LOGIN_URL,
        auth=EPIC_AUTH,
        data={
            "grant_type": "otp",
            "otp": code,
            "continuation_token": continuation_token,
            "includePerms": "false",
        },
    )
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"2FA failed: {data.get('errorMessage', resp.text)}")
    return data


async def get_free_games(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(FREE_GAMES_URL, params={"locale": "en-US", "country": "US"})
    resp.raise_for_status()
    elements = resp.json()["data"]["Catalog"]["searchStore"]["elements"]

    free = []
    for el in elements:
        promotions = el.get("promotions") or {}
        offers = promotions.get("promotionalOffers") or []
        for offer_group in offers:
            for offer in offer_group.get("promotionalOffers", []):
                if offer.get("discountSetting", {}).get("discountPercentage") == 0:
                    cover = None
                    for img in el.get("keyImages", []):
                        if img.get("type") == "Thumbnail":
                            cover = img.get("url")
                            break
                    free.append({
                        "id": el.get("id") or el.get("productSlug", ""),
                        "namespace": el.get("namespace", ""),
                        "title": el.get("title", "Unknown"),
                        "cover_url": cover,
                    })
    return free


async def claim_game(client: httpx.AsyncClient, access_token: str, account_id: str, game: dict) -> bool:
    """Returns True if claimed, False if already owned."""
    headers = {"Authorization": f"Bearer {access_token}"}

    # Use the store purchase endpoint with a zero-cost order
    resp = await _post_with_retry(
        client,
        ORDER_URL,
        headers=headers,
        json={
            "salesChannel": "Launcher-purchase-client",
            "entitlementSource": "Launcher-purchase-client",
            "returnSplitPaymentItems": False,
            "lineOffers": [
                {
                    "offerId": game["id"],
                    "quantity": 1,
                    "namespace": game["namespace"],
                }
            ],
            "totalAmount": 0,
            "currency": "USD",
        },
    )

    if resp.status_code in (200, 201):
        return True

    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    error = body.get("errorCode", "")
    if "already_owned" in error or "already_purchased" in error:
        return False

    logger.warning("Claim returned %d for %s: %s", resp.status_code, game["title"], resp.text)
    return False


async def run_claim_job():
    logger.info("Claim job started at %s", datetime.now(timezone.utc).isoformat())
    state.last_run_status = "running"
    state.last_run_time = datetime.now(timezone.utc).isoformat()

    email = await get_setting("epic_email")
    password = await get_setting("epic_password")

    if not email or not password:
        logger.error("Epic credentials not configured.")
        state.last_run_status = "failed"
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            status, data = await login(client, email, password)
        except RuntimeError as e:
            logger.error("Login error: %s", e)
            state.last_run_status = "failed"
            await _notify(f"Epic Games login failed: {e}")
            return

        if status == "2fa_required":
            continuation_token = data.get("continuation_token", "")
            logger.info("2FA required — waiting for user input (timeout: %ds)", TWOFA_TIMEOUT)

            loop = asyncio.get_event_loop()
            state.twofa_future = loop.create_future()
            state.waiting_for_2fa = True
            state.twofa_deadline = time.time() + TWOFA_TIMEOUT

            await _notify(
                "Epic Games needs your 2FA code. Open the dashboard to submit it."
            )

            try:
                code = await asyncio.wait_for(state.twofa_future, timeout=TWOFA_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("2FA timed out after %ds", TWOFA_TIMEOUT)
                state.last_run_status = "failed"
                await _notify("Epic Games 2FA timed out — claim aborted.")
                return
            finally:
                state.waiting_for_2fa = False
                state.twofa_future = None
                state.twofa_deadline = None

            try:
                data = await submit_2fa(client, continuation_token, code)
            except RuntimeError as e:
                logger.error("2FA submission failed: %s", e)
                state.last_run_status = "failed"
                await _notify(f"Epic Games 2FA failed: {e}")
                return

        access_token = data.get("access_token", "")
        account_id = data.get("account_id", "")

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
            result = await claim_game(client, access_token, account_id, game)
            if result:
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
