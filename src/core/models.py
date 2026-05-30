from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, model_validator


# ── State Machine ────────────────────────────────────────────────────────────

class BotState(str, Enum):
    FLAT            = "FLAT"
    STARTER_LONG    = "STARTER_LONG"
    BASE_LONG       = "BASE_LONG"
    PYRAMID_LONG    = "PYRAMID_LONG"
    REDUCE_MODE     = "REDUCE_MODE"
    EVENT_RISK_MODE = "EVENT_RISK_MODE"
    RUNNER_LONG     = "RUNNER_LONG"
    EXITED          = "EXITED"
    HALTED          = "HALTED"


# ── Core data models ─────────────────────────────────────────────────────────

class LotRecord(BaseModel):
    """One position lot (base or add-on).

    ``contracts`` is the primary field (float, supports fractional crypto lots).
    ``shares`` is kept as ``Optional[int]`` for backward compat with existing
    tests and equity code; it is always auto-populated from ``contracts`` by
    the model validator (truncated to int).
    """
    lot_id:      str
    entry_price: float
    contracts:   float         = 0.0   # primary — float contract size
    entry_date:  date
    shares:      Optional[int] = None  # compat; auto-synced ← do not use in new code

    @model_validator(mode="after")
    def _sync_contracts_shares(self) -> "LotRecord":
        # Old code path: shares= was set, contracts defaults to 0
        if self.contracts == 0.0 and self.shares is not None:
            self.contracts = float(self.shares)
        # Always ensure shares is an int for backward compat
        if self.shares is None:
            self.shares = int(self.contracts)  # truncates; HL fractional lots get 0
        return self


class IndicatorSnapshot(BaseModel):
    date:                   date
    adj_close:              float
    open:                   float
    high:                   float
    low:                    float
    volume:                 float
    prev_adj_close:         float
    ma5:                    float
    ma10:                   float
    ma20:                   float
    ma50:                   float
    atr14:                  float
    avg_volume_20d:         float
    prior_highest_high_20d: float   # rolling max(high,20).shift(1)
    drawdown_from_20d_high: float   # uses prior_highest_high_20d
    distance_from_ma10:     float   # (adj_close - ma10) / ma10
    distance_from_ma20:     float   # (adj_close - ma20) / ma20
    intraday_return:        float   # (adj_close - raw_open) / raw_open
    gap_up_pct:             float   # max(0, (raw_open - prev_adj_close) / prev_adj_close)
    gap_down_pct:           float   # max(0, (prev_adj_close - raw_open) / prev_adj_close)
    # close is always populated = adj_close value (alias for crypto-perp compat)
    close:                  Optional[float]    = None
    # bar_time is set only for sub-daily bars with tz-aware DatetimeIndex (HL path);
    # stays None for date-only yfinance data (backward compat).
    bar_time:               Optional[datetime] = None

    @model_validator(mode="after")
    def _populate_close(self) -> "IndicatorSnapshot":
        """Ensure close is always set; defaults to adj_close for equity compat."""
        if self.close is None:
            self.close = self.adj_close
        if self.bar_time is not None:
            self.date = self.bar_time.date()
        return self


class ThesisState(BaseModel):
    symbol:               str
    state:                BotState           = BotState.FLAT
    prior_state:          Optional[BotState] = None
    protect_profit_mode:  bool               = False
    runner_mode_active:       bool               = False
    runner_target_contracts:  Optional[float]   = None  # primary (new)
    runner_target_shares:     Optional[int]     = None  # compat

    thesis_enabled: bool          = True
    entry_date:     Optional[date] = None

    base_lot:   Optional[LotRecord] = None
    addon_lots: List[LotRecord]     = []

    avg_entry_price:              Optional[float] = None
    # Position size — contracts is primary, shares is compat (always populated)
    current_position_contracts:   float          = 0.0
    current_position_shares:      Optional[int]  = None  # compat; auto-synced
    current_notional:             float          = 0.0
    # Per-asset contract metadata (set from hl.sz_decimals config or HLAssetMeta)
    sz_decimals:                  int            = 0

    add_count:      int            = 0
    last_add_price: Optional[float] = None

    highest_price_since_entry: Optional[float] = None
    peak_unrealized_pnl_pct:   float            = 0.0
    initial_stop_price:        Optional[float]  = None
    trailing_stop_price:       Optional[float]  = None

    tp_levels_triggered:       List[bool] = [False, False, False, False]
    target_price_tp_triggered: bool       = False

    realized_pnl:  float = 0.0
    unrealized_pnl: float = 0.0
    thesis_pnl:    float = 0.0
    days_to_event: Optional[int] = None
    # HL Phase 1: funding PnL placeholder (computed in Phase 6)
    cumulative_funding_pnl: float = 0.0
    last_funding_rate: float = 0.0
    next_funding_timestamp: Optional[datetime] = None
    # HL Phase 3: live account state (populated by reconciler)
    liquidation_price: Optional[float] = None
    margin_used_usd:   float = 0.0
    leverage_used:     float = 0.0

    halted:       bool           = False
    halt_reason:  Optional[str]  = None
    last_action:  Optional[str]  = None
    last_updated: Optional[datetime] = None

    @model_validator(mode="after")
    def _sync_position_fields(self) -> "ThesisState":
        # current_position: old code sets shares= → derive contracts
        if self.current_position_shares is not None and self.current_position_contracts == 0.0:
            self.current_position_contracts = float(self.current_position_shares)
        # Always ensure shares is populated for backward compat
        if self.current_position_shares is None:
            self.current_position_shares = int(self.current_position_contracts)
        # runner_target: sync both directions
        if self.runner_target_shares is not None and self.runner_target_contracts is None:
            self.runner_target_contracts = float(self.runner_target_shares)
        if self.runner_target_contracts is not None and self.runner_target_shares is None:
            self.runner_target_shares = int(self.runner_target_contracts)
        return self


# ── Decision ──────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    NO_ACTION            = "no_action"
    BUY_STARTER          = "buy_starter"
    BUY_BASE             = "buy_base"
    BUY_ADDON            = "buy_addon"
    SELL_REDUCE_ADDON    = "sell_reduce_addon"
    SELL_REDUCE_BASE     = "sell_reduce_base"
    SELL_TAKE_PROFIT     = "sell_take_profit"
    SELL_STOP            = "sell_stop"
    SELL_TRAILING_STOP   = "sell_trailing_stop"
    SELL_EVENT_DERISKING = "sell_event_derisking"
    EXIT_ALL             = "exit_all"
    HALT                 = "halt"


class Decision(BaseModel):
    action:                   ActionType
    contracts:                float                      = 0.0  # primary (new)
    reason:                   str
    blockers:                 List[str]                  = []
    new_state:                Optional[BotState]         = None
    new_protect_profit_mode:  Optional[bool]             = None
    indicators:               Optional[IndicatorSnapshot] = None
    shares:                   int                        = 0    # compat — kept as int

    @model_validator(mode="after")
    def _sync_contracts(self) -> "Decision":
        if self.contracts == 0.0 and self.shares != 0:
            self.contracts = float(self.shares)
        elif self.contracts != 0.0 and self.shares == 0:
            self.shares = int(self.contracts)
        return self


# ── Fill ──────────────────────────────────────────────────────────────────────

class Fill(BaseModel):
    action:       ActionType
    contracts:    float    = 0.0   # primary (new)
    fill_price:   float
    slippage_bps: float
    realized_pnl: float   = 0.0
    commission:   float   = 0.0
    timestamp:    datetime
    shares:       int      = 0    # compat — kept as int

    @model_validator(mode="after")
    def _sync_contracts(self) -> "Fill":
        if self.contracts == 0.0 and self.shares != 0:
            self.contracts = float(self.shares)
        elif self.contracts != 0.0 and self.shares == 0:
            self.shares = int(self.contracts)
        return self


# ── Hyperliquid asset metadata ───────────────────────────────────────────────

class HLAssetMeta(BaseModel):
    """Exchange-reported metadata for one Hyperliquid asset (Phase 2)."""
    coin:         str
    sz_decimals:  int    # decimal places for contract size (e.g. 3 for BTC)
    min_size:     float  # minimum order size in contracts
    max_leverage: float  # exchange-reported max leverage


# ── Config model ──────────────────────────────────────────────────────────────

class BotConfig(BaseModel):
    """
    Full config model. Nested structure mirrors YAML sections in Section 5.
    validate_config() enforces Section 5.1 rules after loading.
    """
    model_config = {"extra": "forbid"}

    bot:           dict
    symbol:        dict
    thesis:        dict
    capital:       dict
    leverage:      dict
    entry:         dict
    add:           dict
    reduce:        dict
    take_profit:   dict
    risk:          dict
    event_risk:    dict
    execution:     dict
    data:          dict
    logging:       dict
    notifications: dict
    # HL Phase 1: optional Hyperliquid-specific config block
    hl:            Optional[dict] = None
