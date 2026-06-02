from __future__ import annotations

from src.core.config_loader import load_config


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def test_hype_soak_config_loads_with_hype_symbol_and_coin(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)

    cfg = load_config("config/hype_testnet_soak.yaml")

    assert cfg.bot["mode"] == "testnet"
    assert cfg.symbol["ticker"] == "HYPE"
    assert cfg.hl["network"] == "testnet"
    assert cfg.hl["coin"] == "HYPE"


def test_hype_soak_uses_1h_bars(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)

    cfg = load_config("config/hype_testnet_soak.yaml")

    assert cfg.data["bar_interval"] == "1h"
    assert cfg.hl["bar_interval"] == "1h"


def test_hype_soak_risk_settings_match_current_testnet_account(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)

    cfg = load_config("config/hype_testnet_soak.yaml")

    assert cfg.capital["starting_equity"] == 900
    assert cfg.capital["max_total_capital_at_risk_pct"] == 0.03
    assert cfg.capital["starting_equity"] * cfg.capital["max_total_capital_at_risk_pct"] == 27


def test_hype_soak_notifications_are_enabled_for_supervision(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)

    cfg = load_config("config/hype_testnet_soak.yaml")

    assert cfg.notifications["enabled"] is True
    assert cfg.notifications["telegram_enabled"] is True
    assert cfg.notifications["heartbeat_enabled"] is True
    assert cfg.notifications["trade_alerts_enabled"] is True
    assert cfg.notifications["decision_alerts_enabled"] is True
    assert cfg.notifications["summary_alerts_enabled"] is True
    assert cfg.notifications["weekly_summary_enabled"] is False


def test_btc_configs_remain_btc(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setenv("HL_MAINNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")

    realistic = load_config("config/btc_testnet_realistic_probe.yaml")
    soak = load_config("config/btc_testnet_soak.yaml")
    mainnet = load_config("config/btc_long_thesis_mainnet.yaml")

    assert realistic.symbol["ticker"] == "BTC"
    assert realistic.hl["coin"] == "BTC"
    assert soak.symbol["ticker"] == "BTC"
    assert soak.hl["coin"] == "BTC"
    assert mainnet.symbol["ticker"] == "BTC"
    assert mainnet.hl["coin"] == "BTC"
    assert mainnet.hl["network"] == "mainnet"
