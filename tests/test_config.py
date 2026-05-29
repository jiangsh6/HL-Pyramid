"""
Section 22.1 — Config tests (6 tests).
"""
import pytest

from src.core.config_loader import load_config, validate_config, config_snapshot_hash
from src.core.models import BotConfig


CONFIG_PATH = "config/mu_long_thesis.yaml"


def _base_cfg() -> BotConfig:
    return load_config(CONFIG_PATH)


# ── test_config_loads_valid_yaml ─────────────────────────────────────────────

def test_config_loads_valid_yaml():
    cfg = _base_cfg()
    assert cfg.bot["mode"] == "paper"
    assert cfg.symbol["ticker"] == "MU"
    assert cfg.thesis["target_price"] == 2000
    assert cfg.data["source"] == "yfinance"


# ── test_config_rejects_live_mode_in_v01 ────────────────────────────────────

def test_config_rejects_live_mode_in_v01():
    cfg = _base_cfg()
    cfg.bot["mode"] = "live"
    with pytest.raises(ValueError, match="bot.mode"):
        validate_config(cfg)


# ── test_config_requires_event_date_when_event_risk_enabled ─────────────────

def test_config_requires_event_date_when_event_risk_enabled():
    cfg = _base_cfg()
    cfg.event_risk["enabled"] = True
    del cfg.thesis["event_date"]
    with pytest.raises(ValueError, match="event_date"):
        validate_config(cfg)


# ── test_config_rejects_mismatched_add_sizes_count ──────────────────────────

def test_config_rejects_mismatched_add_sizes_count():
    cfg = _base_cfg()
    cfg.add["add_sizes_pct"] = [0.08, 0.06]   # only 2 elements, max_add_count=4
    with pytest.raises(ValueError, match="add_sizes_pct"):
        validate_config(cfg)


# ── test_config_snapshot_hash_is_deterministic ──────────────────────────────

def test_config_snapshot_hash_is_deterministic():
    cfg = _base_cfg()
    h1 = config_snapshot_hash(cfg)
    h2 = config_snapshot_hash(cfg)
    assert h1 == h2
    assert len(h1) == 64  # SHA256 hex digest


# ── test_config_rejects_duplicate_event_date_in_event_risk_block ────────────

def test_config_rejects_duplicate_event_date_in_event_risk_block():
    cfg = _base_cfg()
    cfg.event_risk["event_date"] = "2026-06-24"   # must NOT live here
    with pytest.raises(ValueError, match="event_risk block must not contain event_date"):
        validate_config(cfg)
