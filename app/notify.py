import logging

import httpx

logger = logging.getLogger(__name__)


async def send_notification(message: str, url: str, webhook_type: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if webhook_type == "ntfy":
                await client.post(url, content=message.encode())

            elif webhook_type == "gotify":
                await client.post(
                    f"{url.rstrip('/')}/message",
                    json={"title": "Epic Games Claimer", "message": message, "priority": 5},
                )

            elif webhook_type == "pushover":
                # url should be "apptoken|userkey"
                parts = url.split("|")
                if len(parts) != 2:
                    logger.error("Pushover url must be 'apptoken|userkey'")
                    return
                await client.post(
                    "https://api.pushover.net/1/messages.json",
                    data={
                        "token": parts[0],
                        "user": parts[1],
                        "title": "Epic Games Claimer",
                        "message": message,
                    },
                )

            else:
                logger.warning("Unknown webhook type: %s", webhook_type)

    except Exception as e:
        logger.warning("Failed to send notification: %s", e)
