"""Small Bot API bridge for Rich Messages missing from the pinned aiogram schema."""

import aiohttp


class RichMessageError(Exception):
    """Telegram rejected a Rich Message request."""


async def call_rich_api(token: str, method: str, payload: dict):
    """Send a JSON Bot API request without placing the bot token in logs."""
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://api.telegram.org/bot{token}/{method}", json=payload
            ) as response:
                result = await response.json(content_type=None)
    except Exception:
        raise RichMessageError("Telegram Bot API is unavailable") from None

    if not result.get("ok"):
        description = str(result.get("description", "Rich Message request failed"))
        raise RichMessageError(description.replace(token, "[redacted]"))

    return result.get("result")
