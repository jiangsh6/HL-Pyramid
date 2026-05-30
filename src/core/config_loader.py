from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from .models import BotConfig


def load_config(path: str) -> BotConfig:
    """Load YAML config file and return a validated BotConfig."""
    raw = yaml.safe_load(Path(path).read_text())
    mainnet_confirmed = raw.pop("mainnet_confirmed", False)
    if mainnet_confirmed:
        raw.setdefault("bot", {})["mainnet_confirmed"] = mainnet_confirmed
    cfg = BotConfig(**raw)
    validate_config(cfg)
    return cfg


def validate_config(cfg: BotConfig) -> None:
    """
    Enforce all 9 validation rules from Section 5.1.
    Raises ValueError on any violation.
    """
    bot      = cfg.bot
    thesis   = cfg.thesis
    capital  = cfg.capital
    add      = cfg.add
    data_cfg = cfg.data
    event    = cfg.event_risk
    tp       = cfg.take_profit

    # Rule 1: bot.mode must be "paper", "testnet", or "mainnet".
    # Mainnet is allowed only behind explicit env-var and config confirmations.
    mode = bot.get("mode")
    if mode == "mainnet":
        if os.environ.get("HL_ALLOW_MAINNET") != "true":
            raise ValueError(
                "mainnet mode is not enabled; "
                "mainnet mode requires HL_ALLOW_MAINNET=true env var. "
                "Set this explicitly to confirm you intend to trade real funds."
            )
        if bot.get("mainnet_confirmed") is not True:
            raise ValueError(
                "mainnet mode requires mainnet_confirmed: true in config. "
                "Add this field explicitly to confirm intent."
            )
    _valid_modes = {"paper", "testnet", "mainnet"}
    if mode not in _valid_modes:
        raise ValueError(
            f"bot.mode must be 'paper', 'testnet', or 'mainnet' "
            f"(got '{mode}') — live/mainnet trading not supported"
        )

    # Rule 2: thesis.event_date must be a valid ISO date when event_risk.enabled
    if event.get("enabled", False):
        event_date_raw = thesis.get("event_date")
        if not event_date_raw:
            raise ValueError(
                "thesis.event_date is required when event_risk.enabled = true"
            )
        try:
            from datetime import date
            date.fromisoformat(str(event_date_raw))
        except (ValueError, TypeError):
            raise ValueError(
                f"thesis.event_date must be a valid ISO date string (got '{event_date_raw}')"
            )

    # Rule 3: thesis.target_price must be > 0
    target_price = thesis.get("target_price")
    if target_price is None or target_price <= 0:
        raise ValueError(
            f"thesis.target_price must be > 0 (got '{target_price}')"
        )

    # Rule 4: add.add_sizes_pct must have exactly add.max_add_count elements
    max_add_count = add.get("max_add_count")
    add_sizes_pct = add.get("add_sizes_pct", [])
    if len(add_sizes_pct) != max_add_count:
        raise ValueError(
            f"add.add_sizes_pct must have exactly {max_add_count} elements "
            f"(got {len(add_sizes_pct)})"
        )

    # Rule 5: All exposure percentages must be in range (0.0, 1.0]
    exposure_fields = [
        ("capital.max_total_capital_at_risk_pct", capital.get("max_total_capital_at_risk_pct")),
        ("capital.max_symbol_exposure_pct",        capital.get("max_symbol_exposure_pct")),
        ("capital.max_initial_exposure_pct",        capital.get("max_initial_exposure_pct")),
        ("capital.max_starter_exposure_pct",        capital.get("max_starter_exposure_pct")),
        ("capital.max_addon_exposure_pct",          capital.get("max_addon_exposure_pct")),
        ("capital.reserve_cash_pct",                capital.get("reserve_cash_pct")),
    ]
    for field_name, val in exposure_fields:
        if val is not None and not (0.0 < val <= 1.0):
            raise ValueError(
                f"{field_name} must be in range (0.0, 1.0] (got {val})"
            )

    # Rule 6: data.source must be "yfinance" or "hyperliquid"
    _valid_sources = {"yfinance", "hyperliquid"}
    if data_cfg.get("source") not in _valid_sources:
        raise ValueError(
            f"data.source must be 'yfinance' or 'hyperliquid' "
            f"(got '{data_cfg.get('source')}')"
        )

    # HL-specific validation (only when source = "hyperliquid")
    if data_cfg.get("source") == "hyperliquid":
        _validate_hl_block(cfg)

    # Rule 7: event_risk block must NOT contain event_date
    if "event_date" in event:
        raise ValueError(
            "event_risk block must not contain event_date — "
            "read it from thesis.event_date instead"
        )

    # Rule 8: take_profit block must NOT contain target_price
    if "target_price" in tp and not isinstance(tp.get("target_price"), dict):
        raise ValueError(
            "take_profit block must not contain a scalar target_price — "
            "read it from thesis.target_price instead"
        )

    # Rule 9: capital.max_total_capital_at_risk_pct must be in range (0.0, 1.0) exclusive of 1.0
    risk_pct = capital.get("max_total_capital_at_risk_pct")
    if risk_pct is not None and not (0.0 < risk_pct < 1.0):
        raise ValueError(
            f"capital.max_total_capital_at_risk_pct must be in range (0.0, 1.0) exclusive "
            f"(got {risk_pct})"
        )


def _validate_hl_block(cfg: BotConfig) -> None:
    """
    Validate the `hl` config block when data.source = "hyperliquid".
    Enforces Decision 1 (testnet-first) and Decision 2 (mainnet gate).
    """
    hl: dict = cfg.hl or {}
    if not hl:
        raise ValueError(
            "data.source='hyperliquid' requires an 'hl' config block with "
            "hl.network, hl.coin, hl.bar_interval, hl.wallet_address"
        )

    # Network must be testnet or mainnet
    network = hl.get("network")
    if network not in ("testnet", "mainnet"):
        raise ValueError(
            f"hl.network must be 'testnet' or 'mainnet' (got '{network}')"
        )

    # Decision 1 + Decision 2: mainnet requires explicit env-var confirmation.
    # HL_ALLOW_MAINNET=true must be set; config alone cannot unlock mainnet.
    if network == "mainnet":
        if os.environ.get("HL_ALLOW_MAINNET", "").strip().lower() != "true":
            raise ValueError(
                "hl.network='mainnet' requires the environment variable "
                "HL_ALLOW_MAINNET=true to be explicitly set. "
                "Use hl.network='testnet' for all development phases."
            )

    # Required string fields
    for field in ("coin", "bar_interval", "wallet_address"):
        if not hl.get(field):
            raise ValueError(
                f"hl.{field} is required when data.source='hyperliquid'"
            )

    # bar_interval must be a recognised cadence when present
    bar_interval = hl.get("bar_interval")
    if bar_interval is not None:
        _valid_intervals = {"1m", "5m", "15m", "1h", "4h", "1d"}
        if bar_interval not in _valid_intervals:
            raise ValueError(
                f"hl.bar_interval must be one of {sorted(_valid_intervals)} "
                f"(got '{bar_interval}')"
            )

    # Optional but validated when present
    sz_dec = hl.get("sz_decimals")
    if sz_dec is not None:
        if not isinstance(sz_dec, int) or not (0 <= sz_dec <= 8):
            raise ValueError(
                f"hl.sz_decimals must be an integer in [0, 8] (got {sz_dec!r})"
            )

    min_sz = hl.get("min_size")
    if min_sz is not None:
        try:
            min_sz_f = float(min_sz)
        except (TypeError, ValueError):
            raise ValueError(
                f"hl.min_size must be a positive float (got {min_sz!r})"
            )
        if min_sz_f <= 0:
            raise ValueError(
                f"hl.min_size must be > 0 (got {min_sz_f})"
            )


def config_snapshot_hash(cfg: BotConfig) -> str:
    """SHA256 of deterministic sorted-key JSON serialization of the config."""
    raw = json.dumps(cfg.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
