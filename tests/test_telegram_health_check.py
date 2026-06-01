from __future__ import annotations

from pathlib import Path

import yaml

from scripts import test_telegram_notification as health

TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def _write_config(tmp_path: Path, *, enabled: bool = True, telegram_enabled: bool = True) -> Path:
    raw = yaml.safe_load(Path("config/btc_testnet_realistic_probe.yaml").read_text())
    raw["hl"]["wallet_address"] = TESTNET_WALLET
    raw["notifications"]["enabled"] = enabled
    raw["notifications"]["telegram_enabled"] = telegram_enabled
    raw["bot"]["run_id"] = "test-health-run"
    path = tmp_path / "health_config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


class _OKResponse:
    def raise_for_status(self) -> None:
        return None


class _BadResponse:
    def raise_for_status(self) -> None:
        raise RuntimeError("telegram rejected")


def test_build_health_check_event_includes_required_fields(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    config = health.load_config(str(cfg_path))

    event = health.build_health_check_event(config, str(cfg_path))

    assert event["event"] == "telegram_health_check"
    assert event["network"] == "testnet"
    assert event["config_path"] == str(cfg_path)
    assert event["run_id"] == "test-health-run"
    assert event["timestamp"]


def test_health_check_refuses_when_notifications_disabled(tmp_path, monkeypatch, capsys):
    cfg_path = _write_config(tmp_path, enabled=False, telegram_enabled=True)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    code = health.main(["--config", str(cfg_path)])

    assert code == 2
    assert "notifications_disabled" in capsys.readouterr().out


def test_health_check_refuses_when_telegram_env_missing(tmp_path, monkeypatch, capsys):
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    code = health.main(["--config", str(cfg_path)])

    assert code == 2
    assert "missing_TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def test_health_check_sends_one_message_without_touching_state(tmp_path, monkeypatch, capsys):
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    calls = []

    def fake_post(url, json, timeout):  # noqa: ANN001
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _OKResponse()

    monkeypatch.setattr("src.notifications.telegram.requests.post", fake_post)

    code = health.main(["--config", str(cfg_path)])

    assert code == 0
    assert len(calls) == 1
    assert "telegram_health_check" in calls[0]["json"]["text"]
    assert not (tmp_path / "state.json").exists()
    assert "TELEGRAM_HEALTH_CHECK_SENT" in capsys.readouterr().out


def test_health_check_send_failure_is_fail_soft_for_trading_state(tmp_path, monkeypatch, capsys):
    cfg_path = _write_config(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("src.notifications.telegram.requests.post", lambda *a, **kw: _BadResponse())

    code = health.main(["--config", str(cfg_path)])

    assert code == 1
    assert not (tmp_path / "state.json").exists()
    assert "send_failed" in capsys.readouterr().out
