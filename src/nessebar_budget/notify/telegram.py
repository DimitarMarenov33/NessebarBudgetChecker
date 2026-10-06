"""Telegram notification helper.

Sends plain text messages to a configured Telegram chat using
python-telegram-bot. This is the only outbound network call this project
makes on its own initiative, and only when explicitly configured via
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import logging

from telegram import Bot

from nessebar_budget.config import get_settings

logger = logging.getLogger(__name__)


async def send_message(text: str) -> None:
    """Send `text` to the configured Telegram chat.

    No-ops with a logged warning if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID
    are not configured.
    """
    settings = get_settings()

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing); "
            "skipping send_message."
        )
        return

    bot = Bot(token=settings.telegram_bot_token)
    await bot.send_message(chat_id=settings.telegram_chat_id, text=text)
