from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts.run_live_hl import main as run_live_main
from src.core.config_loader import load_config, validate_config
from src.core.models import BotConfig
from src.hl.auth import validate_mainnet_intent


TESTNET_CONFIG_PATH = "config/btc_long_thesis.yaml"
MAINNET_CONFIG_PATH = "config/btc_long_thesis_mainnet.yaml"
TESTNET_WALLET = "0x2222222222222222222222222222222222222222"
MAINNET_WALLET = "0x1111111111111111111111111111111111111111"


def _clone(cfg: BotConfig) -> BotConfig:
    return BotConfig(**cfg.model_dump())


def _mainnet_config() -> BotConfig:
    with patch.dict(os.environ, {
        "HL_ALLOW_MAINNET": "true",
        "HL_MAINNET_ACCOUNT_ADDRESS": MAINNET_WALLET,
    }):
        return load_config(MAINNET_CONFIG_PATH)


def test_mainnet_rejected_without_env_var():
    cfg = _mainnet_config()
    env = {k: v for k, v in os.environ.items() if k != "HL_ALLOW_MAINNET"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="HL_ALLOW_MAINNET=true env var"):
            validate_config(cfg)


def test_mainnet_rejected_without_mainnet_confirmed_field():
    cfg = _mainnet_config()
    cfg.bot.pop("mainnet_confirmed", None)
    with patch.dict(os.environ, {"HL_ALLOW_MAINNET": "true"}, clear=True):
        with pytest.raises(ValueError, match="mainnet_confirmed: true"):
            validate_config(cfg)


def test_mainnet_rejected_without_cli_flag():
    with patch.dict(os.environ, {
        "HL_ALLOW_MAINNET": "true",
        "HL_MAINNET_AGENT_PRIVATE_KEY": "0x" + "a" * 64,
        "HL_MAINNET_ACCOUNT_ADDRESS": MAINNET_WALLET,
    }):
        with pytest.raises(SystemExit, match="--mainnet flag was not passed"):
            run_live_main(MAINNET_CONFIG_PATH, mainnet=False)


def test_mainnet_accepted_when_all_four_layers_present():
    cfg = _mainnet_config()
    with patch.dict(os.environ, {"HL_ALLOW_MAINNET": "true"}, clear=True):
        validate_mainnet_intent(cfg, cli_has_mainnet_flag=True)


def test_testnet_unaffected_by_mainnet_gate():
    env = {k: v for k, v in os.environ.items() if k != "HL_ALLOW_MAINNET"}
    env["HL_TESTNET_ACCOUNT_ADDRESS"] = TESTNET_WALLET
    with patch.dict(os.environ, env, clear=True):
        cfg = load_config(TESTNET_CONFIG_PATH)
    assert cfg.bot["mode"] == "testnet"


def test_validate_mainnet_intent_raises_on_missing_env():
    cfg = _mainnet_config()
    env = {k: v for k, v in os.environ.items() if k != "HL_ALLOW_MAINNET"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="Layer 2 failed"):
            validate_mainnet_intent(cfg, cli_has_mainnet_flag=True)


def test_validate_mainnet_intent_raises_on_missing_cli_flag():
    cfg = _mainnet_config()
    with patch.dict(os.environ, {"HL_ALLOW_MAINNET": "true"}, clear=True):
        with pytest.raises(ValueError, match="Layer 3 failed"):
            validate_mainnet_intent(cfg, cli_has_mainnet_flag=False)


def test_validate_mainnet_intent_passes_when_all_present():
    cfg = _mainnet_config()
    with patch.dict(os.environ, {"HL_ALLOW_MAINNET": "true"}, clear=True):
        validate_mainnet_intent(cfg, cli_has_mainnet_flag=True)


def test_mainnet_config_file_has_conservative_capital_cap():
    raw = yaml.safe_load(Path(MAINNET_CONFIG_PATH).read_text())
    assert raw["mainnet_confirmed"] is True
    assert raw["bot"]["mode"] == "mainnet"
    assert raw["capital"]["starting_equity"] == 100
    assert raw["capital"]["max_total_capital_at_risk_pct"] == 0.03
    assert raw["capital"]["max_symbol_exposure_pct"] <= 0.20
    assert raw["entry"]["starter"]["exposure_pct"] <= 0.04
    assert raw["leverage"]["enabled"] is False
