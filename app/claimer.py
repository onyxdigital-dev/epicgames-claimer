import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

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


def _generate_checkout_url(games: list[dict]) -> str:
    """Build an Epic Store checkout URL pre-populated with all free game offers.
    Wrapped in a login redirect so the user lands on the checkout page regardless
    of whether they are currently logged in on that browser."""
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
            free_games = await get_free_games(client)
        except Exception as e:
            logger.error("Failed to fetch free games: %s", e)
            state.last_run_status = "failed"
            return

    state.last_run_status = "success"

    if not free_games:
        logger.info("No free games available this week.")
        return

    checkout_url = _generate_checkout_url(free_games)
    titles = [g["title"] for g in free_games]
    state.pending_checkout_url = checkout_url
    state.pending_game_titles = titles

    logger.info("Free games available: %s", ", ".join(titles))
    logger.info("Checkout URL generated — sending notification")

    await _notify(
        f"Free Epic game(s) available: {', '.join(titles)}\n\nClaim here (opens Epic checkout):\n{checkout_url}"
    )
    logger.info("Claim job complete — notification sent for %d game(s).", len(free_games))


async def _notify(message: str):
    url = await get_setting("notify_url")
    webhook_type = await get_setting("notify_type")
    if url and webhook_type:
        await send_notification(message, url, webhook_type)
