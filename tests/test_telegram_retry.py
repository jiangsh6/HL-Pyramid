from __future__ import annotations

from src.notifications import telegram


class _OKResponse:
    def raise_for_status(self) -> None:
        return None


def test_send_message_succeeds_on_first_attempt(monkeypatch):
    notifier = telegram.TelegramNotifier(enabled=True, token="token", chat_id="123", retry_count=2)
    calls = []

    def fake_post(*args, **kwargs):  # noqa: ANN001
        calls.append((args, kwargs))
        return _OKResponse()

    monkeypatch.setattr(telegram.requests, "post", fake_post)

    assert notifier.send_message("hello") is True
    assert len(calls) == 1


def test_send_message_succeeds_after_retry(monkeypatch):
    notifier = telegram.TelegramNotifier(
        enabled=True,
        token="token",
        chat_id="123",
        retry_count=2,
        retry_delay_seconds=0.25,
    )
    calls = []
    sleeps = []

    def fake_post(*args, **kwargs):  # noqa: ANN001
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise RuntimeError("temporary telegram failure")
        return _OKResponse()

    monkeypatch.setattr(telegram.requests, "post", fake_post)
    monkeypatch.setattr(telegram.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert notifier.send_message("hello") is True
    assert len(calls) == 2
    assert sleeps == [0.25]


def test_send_message_fails_after_retries_exhausted(monkeypatch):
    notifier = telegram.TelegramNotifier(
        enabled=True,
        token="token",
        chat_id="123",
        retry_count=2,
        retry_delay_seconds=0.5,
    )
    calls = []
    sleeps = []

    def fake_post(*args, **kwargs):  # noqa: ANN001
        calls.append((args, kwargs))
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram.requests, "post", fake_post)
    monkeypatch.setattr(telegram.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert notifier.send_message("hello") is False
    assert len(calls) == 3
    assert sleeps == [0.5, 0.5]


def test_retry_count_is_respected(monkeypatch):
    notifier = telegram.TelegramNotifier(enabled=True, token="token", chat_id="123", retry_count=1)
    calls = []

    def fake_post(*args, **kwargs):  # noqa: ANN001
        calls.append((args, kwargs))
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram.requests, "post", fake_post)

    assert notifier.send_message("hello") is False
    assert len(calls) == 2


def test_notify_event_failure_does_not_escape_into_trading_flow(monkeypatch):
    notifier = telegram.TelegramNotifier(enabled=True, token="token", chat_id="123", retry_count=1)
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert telegram.notify_event(notifier, {"type": "safety", "event": "test"}) is False


def test_retry_config_is_loaded_from_notification_config(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    notifier = telegram.TelegramNotifier.from_config({
        "enabled": True,
        "telegram_enabled": True,
        "retry_count": 2,
        "retry_delay_seconds": 1.5,
    })

    assert notifier.enabled is True
    assert notifier.retry_count == 2
    assert notifier.retry_delay_seconds == 1.5
