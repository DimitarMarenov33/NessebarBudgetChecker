"""Telegram notification helpers.

Two send paths coexist here, mirroring the same split as `analysis.engine`
(see that module's docstring for the general pattern):

- `send_message(text)` is the original, minimal one-message helper. Kept
  exactly as-is because `pipeline.py`'s `_step_notify_pending` (owned by a
  different workstream) imports and calls it directly, once per pending
  flag.
- `send_flags_notification(flags, ...)` is the real notifier for this
  project: one grouped, HTML-formatted Bulgarian message per run (the first
  ~10 flags in full, any remainder summarized by severity), used by the
  `notify-pending` CLI command.

Both no-op with a logged warning if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are
not configured -- this is the only outbound network call this project makes
on its own initiative, and only when explicitly configured.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from telegram import Bot
from telegram.constants import ParseMode

from nessebar_budget.config import get_settings
from nessebar_budget.db.models import Flag

logger = logging.getLogger(__name__)

#: Bulgarian-citizen-facing severity -> (emoji, label). `info`/`warning`/
#: `high` are the only severities any rule in `analysis.rules` produces.
SEVERITY_EMOJI: dict[str, str] = {"info": "ℹ️", "warning": "⚠️", "high": "\U0001f6a8"}
SEVERITY_LABEL_BG: dict[str, str] = {
    "info": "информация",
    "warning": "предупреждение",
    "high": "сериозно",
}
_SEVERITY_RANK: dict[str, int] = {"high": 0, "warning": 1, "info": 2}

#: Flags shown in full in one notification message; any remainder is
#: summarized as a per-severity count instead of being omitted silently.
MAX_FLAGS_IN_MESSAGE = 10


class SendsMessages(Protocol):
    """What `send_flags_notification` needs from a `bot`: just enough to let
    tests inject a fake in place of a real `telegram.Bot`."""

    async def send_message(self, chat_id: Any, text: str, parse_mode: Any = None) -> Any: ...


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send_message(text: str) -> None:
    """Send a single plain-text `text` to the configured Telegram chat.

    No-ops with a logged warning if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are
    not configured. Kept for `pipeline.py`'s per-flag `_step_notify_pending`;
    prefer `send_flags_notification` for new code (one grouped message/run).
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


def format_flags_message(
    flags: list[Flag],
    *,
    site_base_url: str,
    max_flags: int = MAX_FLAGS_IN_MESSAGE,
) -> str:
    """Build one HTML-formatted, Bulgarian message grouping `flags`.

    Flags are shown worst-severity-first (`high` > `warning` > `info`); the
    first `max_flags` (after that sort) are shown in full: severity emoji,
    the flag's own (already Bulgarian, citizen-facing) `message`, and a
    `{site_base_url}/flags/#<flag-id>` deep link. Any remainder is summarized
    as a per-severity count rather than sent in full or dropped silently.
    """
    if not flags:
        return "Няма нови сигнали за уведомяване."

    ordered = sorted(flags, key=lambda f: _SEVERITY_RANK.get(f.severity, 99))
    shown, rest = ordered[:max_flags], ordered[max_flags:]

    lines = [f"<b>Бюджетен монитор Несебър — {len(flags)} нов(и) сигнал(и)</b>", ""]
    for flag in shown:
        emoji = SEVERITY_EMOJI.get(flag.severity, "•")
        message = _escape_html(flag.message)
        link = f"{site_base_url}/flags/#{flag.id}"
        lines.append(f'{emoji} {message}\n<a href="{link}">{link}</a>')
        lines.append("")

    if rest:
        counts: dict[str, int] = {}
        for flag in rest:
            counts[flag.severity] = counts.get(flag.severity, 0) + 1
        parts = [
            f"{SEVERITY_EMOJI.get(severity, '')} {count} {SEVERITY_LABEL_BG.get(severity, severity)}"
            for severity, count in sorted(counts.items())
        ]
        lines.append(f"...и още {len(rest)}: " + ", ".join(parts) + ".")

    return "\n".join(lines).strip()


async def send_flags_notification(
    flags: list[Flag],
    *,
    bot: SendsMessages | None = None,
    dry_run: bool = False,
) -> str | None:
    """Send one grouped notification for `flags` (see `format_flags_message`).

    - With `dry_run=True`, never sends anything and never requires Telegram
      to be configured: just returns the formatted message, for the
      `notify-pending --dry-run` CLI command to print.
    - Otherwise, no-ops with a logged warning (returning `None`) if
      TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing.
    - `bot` lets callers/tests inject a fake bot-like object (just needs an
      async `send_message(chat_id, text, parse_mode)`); a real
      `telegram.Bot` is constructed from settings otherwise.

    Returns the message text that was sent (or would have been sent, under
    `dry_run`), or `None` if the not-configured no-op path was taken.
    """
    settings = get_settings()
    message = format_flags_message(flags, site_base_url=settings.site_base_url)

    if dry_run:
        return message

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing); "
            "skipping send_flags_notification."
        )
        return None

    active_bot: SendsMessages = bot if bot is not None else Bot(token=settings.telegram_bot_token)
    await active_bot.send_message(
        chat_id=settings.telegram_chat_id, text=message, parse_mode=ParseMode.HTML
    )
    return message
