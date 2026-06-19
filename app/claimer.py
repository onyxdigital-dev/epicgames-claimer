import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

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

    free = []
    for el in elements:
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
                        "title": el.get("title", "Unknown"),
                        "cover_url": cover,
                    })
    return free


async def claim_game(client: httpx.AsyncClient, access_token: str, account_id: str, game: dict) -> bool:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = await _post_with_retry(
        client,
        ORDER_URL,
        headers=headers,
        json={
            "salesChannel": "Launcher-purchase-client",
            "entitlementSource": "Launcher-purchase-client",
            "returnSplitPaymentItems": False,
            "lineOffers": [{"offerId": game["id"], "quantity": 1, "namespace": game["namespace"]}],
            "totalAmount": 0,
            "currency": "USD",
        },
    )
    if resp.status_code in (200, 201):
        return True
    body = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
    error = body.get("errorCode", "")
    if "already_owned" in error or "already_purchased" in error:
        return False
    logger.warning("Claim returned %d for %s: %s", resp.status_code, game["title"], resp.text)
    return False


async def run_claim_job():
    logger.info("Claim job started at %s", datetime.now(timezone.utc).isoformat())
    state.last_run_status = "running"
    state.last_run_time = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=30) as client:
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
