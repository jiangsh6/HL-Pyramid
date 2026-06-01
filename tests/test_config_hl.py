"""
HL Phase 1 — Config validation for Hyperliquid mode.

All 4 named tests:
  test_testnet_mode_accepted
  test_hyperliquid_source_accepted
  test_hl_block_required_when_source_is_hyperliquid
  test_mainnet_blocked_without_env_var
"""
from __future__ import annotations

import copy
import os
from unittest.mock import patch

import pytest

from src.core.config_loader import load_config, validate_config
from src.core.models import BotConfig


HL_CONFIG_PATH  = "config/btc_long_thesis.yaml"
EQ_CONFIG_PATH  = "config/mu_long_thesis.yaml"
TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_btc() -> BotConfig:
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        return load_config(HL_CONFIG_PATH)


def _clone(cfg: BotConfig) -> BotConfig:
    """Deep-copy a BotConfig via its dict representation."""
    return BotConfig(**cfg.model_dump())


# ── Named tests ──────────────────────────────────────────────────────────────

def test_testnet_mode_accepted():
    """bot.mode='testnet' must load and validate without error."""
    cfg = _load_btc()
    assert cfg.bot["mode"] == "testnet"


def test_hyperliquid_source_accepted():
    """data.source='hyperliquid' must load and validate without error."""
    cfg = _load_btc()
    assert cfg.data["source"] == "hyperliquid"


def test_hl_block_required_when_source_is_hyperliquid():
    """
    When data.source='hyperliquid', omitting the hl block must raise ValueError.
    """
    cfg = _load_btc()
    cfg_no_hl = _clone(cfg)
    cfg_no_hl.hl = None    # remove the hl block

    with pytest.raises(ValueError, match="'hl' config block"):
        validate_config(cfg_no_hl)


def test_mainnet_blocked_without_env_var():
    """
    hl.network='mainnet' without HL_ALLOW_MAINNET=true env var must raise ValueError.
    Decision 1: config alone cannot unlock mainnet.
    """
    cfg = _load_btc()
    cfg_mainnet = _clone(cfg)
    cfg_mainnet.hl = {**cfg_mainnet.hl, "network": "mainnet"}

    # Ensure the env var is NOT set
    os.environ.pop("HL_ALLOW_MAINNET", None)

    with pytest.raises(ValueError, match="HL_ALLOW_MAINNET"):
        validate_config(cfg_mainnet)


# ── Extra coverage ───────────────────────────────────────────────────────────

def test_mainnet_allowed_with_env_var():
    """
    hl.network='mainnet' WITH HL_ALLOW_MAINNET=true must pass config validation.
    """
    cfg = _load_btc()
    cfg_mainnet = _clone(cfg)
    cfg_mainnet.hl = {**cfg_mainnet.hl, "network": "mainnet"}

    try:
        os.environ["HL_ALLOW_MAINNET"] = "true"
        validate_config(cfg_mainnet)   # must not raise
    finally:
        os.environ.pop("HL_ALLOW_MAINNET", None)


def test_paper_mode_still_accepted():
    """Existing equity config with mode='paper' must still pass validation (no regression)."""
    cfg = load_config(EQ_CONFIG_PATH)
    assert cfg.bot["mode"] == "paper"
    validate_config(cfg)   # must not raise


def test_yfinance_source_still_accepted():
    """data.source='yfinance' must still pass validation (no regression)."""
    cfg = load_config(EQ_CONFIG_PATH)
    assert cfg.data["source"] == "yfinance"
    validate_config(cfg)


def test_live_mode_still_rejected():
    """bot.mode='live' must still raise ValueError after the mode expansion."""
    cfg = load_config(EQ_CONFIG_PATH)
    cfg.bot["mode"] = "live"
    with pytest.raises(ValueError, match="bot.mode"):
        validate_config(cfg)


def test_unknown_source_rejected():
    """data.source='coinbase' must be rejected."""
    cfg = _load_btc()
    cfg.data["source"] = "coinbase"
    with pytest.raises(ValueError, match="data.source"):
        validate_config(cfg)


def test_hl_missing_required_field():
    """hl block missing 'coin' must raise ValueError."""
    cfg = _load_btc()
    incomplete_hl = {k: v for k, v in cfg.hl.items() if k != "coin"}
    cfg_bad = _clone(cfg)
    cfg_bad.hl = incomplete_hl
    with pytest.raises(ValueError, match="hl.coin"):
        validate_config(cfg_bad)


def test_hl_invalid_network_rejected():
    """hl.network='live' must raise ValueError."""
    cfg = _load_btc()
    cfg_bad = _clone(cfg)
    cfg_bad.hl = {**cfg_bad.hl, "network": "live"}
    with pytest.raises(ValueError, match="hl.network"):
        validate_config(cfg_bad)


def test_btc_config_snapshot_hash_deterministic():
    """config_snapshot_hash must return the same value on repeated calls."""
    from src.core.config_loader import config_snapshot_hash
    cfg = _load_btc()
    h1 = config_snapshot_hash(cfg)
    h2 = config_snapshot_hash(cfg)
    assert h1 == h2
    assert len(h1) == 64
