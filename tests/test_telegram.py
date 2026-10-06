"""Unit tests for `notify.telegram`.

No real network/Telegram API calls are made: `send_flags_notification` is
exercised either in `dry_run=True` mode, or against a fake bot object with an
async `send_message(chat_id, text, parse_mode)` -- see `FakeBot` below.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from nessebar_budget.db.models import Flag
from nessebar_budget.notify import telegram


def _dt(*args, **kwargs) -> dt.datetime:
    """Naive datetime fixture helper (ruff DTZ001-safe: built via a
    tz-aware call, then stripped, matching this project's convention of
    naive-but-UTC datetimes -- see e.g. `db/repo.py`'s `_now()`)."""
    return dt.datetime(*args, tzinfo=dt.UTC, **kwargs).replace(tzinfo=None)


class FakeBot:
    """Records calls instead of talking to the real Telegram API."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


def _flag(id_: int, rule: str, severity: str, message: str) -> Flag:
    return Flag(
        id=id_,
        rule=rule,
        severity=severity,
        message=message,
        created_at=_dt(2026, 1, 1),
    )


def test_format_flags_message_empty() -> None:
    assert telegram.format_flags_message([], site_base_url="https://x.test") == "Няма нови сигнали за уведомяване."


def test_format_flags_message_includes_emoji_message_and_link() -> None:
    flags = [_flag(42, "late_publication", "high", "Изисква обяснение.")]
    message = telegram.format_flags_message(flags, site_base_url="https://x.test/site")
    assert "\U0001f6a8" in message  # high-severity emoji
    assert "Изисква обяснение." in message
    assert "https://x.test/site/flags/#42" in message
    assert message.startswith("<b>")


def test_format_flags_message_escapes_html_in_user_text() -> None:
    flags = [_flag(1, "r", "info", "A <b>bold</b> & tricky message")]
    message = telegram.format_flags_message(flags, site_base_url="https://x.test")
    assert "<b>bold</b>" not in message
    assert "&lt;b&gt;" in message
    assert "&amp;" in message


def test_format_flags_message_summarizes_overflow() -> None:
    flags = [_flag(i, "r", "warning", f"Съобщение {i}") for i in range(15)]
    message = telegram.format_flags_message(flags, site_base_url="https://x.test", max_flags=10)
    assert "Съобщение 0" in message
    assert "Съобщение 9" in message
    assert "Съобщение 10" not in message
    assert "...и още 5" in message


def test_send_flags_notification_dry_run_never_requires_bot(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    flags = [_flag(1, "r", "info", "Нещо")]
    message = asyncio.run(telegram.send_flags_notification(flags, dry_run=True))
    assert message is not None
    assert "Нещо" in message


def test_send_flags_notification_warns_and_noops_when_unconfigured(monkeypatch, caplog) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    flags = [_flag(1, "r", "info", "Нещо")]
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(telegram.send_flags_notification(flags))
    assert result is None
    assert "not configured" in caplog.text


def test_send_flags_notification_sends_via_injected_bot(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    fake_bot = FakeBot()
    flags = [_flag(7, "overspend_vs_plan", "warning", "Проверете разхода.")]

    result = asyncio.run(telegram.send_flags_notification(flags, bot=fake_bot))

    assert result is not None
    assert len(fake_bot.calls) == 1
    call = fake_bot.calls[0]
    assert call["chat_id"] == "12345"
    assert "Проверете разхода." in call["text"]
    assert call["parse_mode"] == "HTML"


def test_send_message_legacy_path_noops_when_unconfigured(monkeypatch, caplog) -> None:
    # pipeline.py's _step_notify_pending calls this directly per-flag; make
    # sure the legacy no-op path still works unchanged.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with caplog.at_level(logging.WARNING):
        asyncio.run(telegram.send_message("hello"))
    assert "not configured" in caplog.text
