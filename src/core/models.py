from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, model_validator


EPSILON = 1e-12


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
    """One perpetual position lot (base or add-on)."""
    model_config = {"extra": "forbid"}
    lot_id:      str
    entry_price: float
    qty:         float         = 0.0
    entry_date:  date

    @model_validator(mode="after")
    def _validate_qty(self) -> "LotRecord":
        if self.qty < -EPSILON:
            raise ValueError("lot qty must be non-negative")
        if abs(self.qty) <= EPSILON:
            self.qty = 0.0
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
    model_config = {"extra": "forbid"}
    symbol:               str
    state:                BotState           = BotState.FLAT
    prior_state:          Optional[BotState] = None
    protect_profit_mode:  bool               = False
    runner_mode_active:       bool               = False
    runner_target_qty:        Optional[float]   = None

    thesis_enabled: bool          = True
    entry_date:     Optional[date] = None

    base_lot:   Optional[LotRecord] = None
    addon_lots: List[LotRecord]     = []

    avg_entry_price:              Optional[float] = None
    current_position_qty:         float          = 0.0
    original_base_qty:            float          = 0.0
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
    # daily_pnl resets at the start of each new calendar day in decision_engine.
    # Used by the max_daily_loss check (Section 16.3) so historical realized
    # PnL doesn't mask a fresh-day drawdown.
    daily_pnl:     float = 0.0
    days_to_event: Optional[int] = None
    # HL Phase 1: funding PnL placeholder (computed in Phase 6)
    cumulative_funding_pnl: float = 0.0
    last_funding_rate: float = 0.0
    next_funding_timestamp: Optional[datetime] = None
    # HL Phase 3: live account state (populated by reconciler)
    liquidation_price: Optional[float] = None
    margin_used_usd:   float = 0.0
    leverage_used:     float = 0.0
    dust_position:     bool = False
    dust_qty:          float = 0.0
    dust_notional:     float = 0.0
    dust_reason:       Optional[str] = None

    pending_order: Optional["PendingOrder"] = None

    halted:       bool           = False
    halt_reason:  Optional[str]  = None
    last_action:  Optional[str]  = None
    last_updated: Optional[datetime] = None

    @model_validator(mode="after")
    def _validate_position_qty(self) -> "ThesisState":
        if self.current_position_qty < -EPSILON:
            raise ValueError("current_position_qty must be non-negative")
        if abs(self.current_position_qty) <= EPSILON:
            self.current_position_qty = 0.0
        if self.original_base_qty < -EPSILON:
            raise ValueError("original_base_qty must be non-negative")
        if abs(self.original_base_qty) <= EPSILON:
            self.original_base_qty = 0.0
        if self.runner_target_qty is not None and self.runner_target_qty < -EPSILON:
            raise ValueError("runner_target_qty must be non-negative")
        if self.dust_qty < -EPSILON:
            raise ValueError("dust_qty must be non-negative")
        if abs(self.dust_qty) <= EPSILON:
            self.dust_qty = 0.0
        if self.dust_notional < -EPSILON:
            raise ValueError("dust_notional must be non-negative")
        if abs(self.dust_notional) <= EPSILON:
            self.dust_notional = 0.0
        return self


class PendingOrder(BaseModel):
    model_config = {"extra": "forbid"}
    oid: Optional[int] = None
    client_order_id: Optional[str] = None
    symbol: Optional[str] = None
    action: str
    side: str
    reduce_only: bool = False
    order_type: str = "limit"
    qty: float = 0.0
    qty_submitted: float = 0.0
    qty_filled: float = 0.0
    qty_remaining: float = 0.0
    limit_px: float
    status: str
    created_at: datetime
    last_checked_at: Optional[datetime] = None
    source_decision_id: Optional[str] = None
    state_before: Optional[str] = None
    intended_state_after: Optional[str] = None
    applied_state_after: Optional[str] = None

    @model_validator(mode="after")
    def _validate_qty(self) -> "PendingOrder":
        if self.qty < -EPSILON:
            raise ValueError("pending order qty must be non-negative")
        if abs(self.qty) <= EPSILON:
            self.qty = 0.0
        if self.qty_submitted <= EPSILON and self.qty > EPSILON:
            self.qty_submitted = self.qty
        if self.qty_remaining <= EPSILON and self.qty_submitted > EPSILON:
            self.qty_remaining = self.qty_submitted - self.qty_filled
        if abs(self.qty_submitted) <= EPSILON:
            self.qty_submitted = 0.0
        if abs(self.qty_filled) <= EPSILON:
            self.qty_filled = 0.0
        if abs(self.qty_remaining) <= EPSILON:
            self.qty_remaining = 0.0
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
    RECONCILE_POSITION   = "reconcile_position"


class Decision(BaseModel):
    model_config = {"extra": "forbid"}
    action:                   ActionType
    qty:                      float                      = 0.0
    reason:                   str
    blockers:                 List[str]                  = []
    new_state:                Optional[BotState]         = None
    new_protect_profit_mode:  Optional[bool]             = None
    indicators:               Optional[IndicatorSnapshot] = None

    @model_validator(mode="after")
    def _validate_qty(self) -> "Decision":
        if self.qty < -EPSILON:
            raise ValueError("decision qty must be non-negative")
        if abs(self.qty) <= EPSILON:
            self.qty = 0.0
        return self


# ── Fill ──────────────────────────────────────────────────────────────────────

class Fill(BaseModel):
    model_config = {"extra": "forbid"}
    action:       ActionType
    qty:          float    = 0.0
    fill_price:   float
    slippage_bps: float
    realized_pnl: float   = 0.0
    commission:   float   = 0.0
    timestamp:    datetime
    order_id:     Optional[int] = None
    order_status: Optional[str] = None

    @model_validator(mode="after")
    def _validate_qty(self) -> "Fill":
        if self.qty < -EPSILON:
            raise ValueError("fill qty must be non-negative")
        if abs(self.qty) <= EPSILON:
            self.qty = 0.0
        return self


class OrderResultStatus(str, Enum):
    BLOCKED_PRE_ORDER = "blocked_pre_order"
    SUBMITTED_UNFILLED = "submitted_unfilled"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    CANCEL_FAILED = "cancel_failed"
    UNKNOWN = "unknown"
    EXECUTION_ERROR = "execution_error"


class ActionClass(str, Enum):
    OPENING = "opening"
    ADDING = "adding"
    RISK_REDUCING = "risk_reducing"
    EMERGENCY_EXIT = "emergency_exit"
    CANCEL = "cancel"
    NON_ORDER = "non_order"


class OrderResult(BaseModel):
    model_config = {"extra": "forbid"}
    status: OrderResultStatus
    oid: Optional[int] = None
    client_order_id: Optional[str] = None
    action: ActionType
    side: str
    reduce_only: bool = False
    submitted_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_px: Optional[float] = None
    limit_px: Optional[float] = None
    realized_pnl: float = 0.0
    commission: float = 0.0
    raw_status_sanitized: Optional[str] = None
    exchange_error_sanitized: Optional[str] = None
    state_before: Optional[str] = None
    intended_state_after: Optional[str] = None
    applied_state_after: Optional[str] = None
    created_at: datetime

    @model_validator(mode="after")
    def _validate_qtys(self) -> "OrderResult":
        for field in ("submitted_qty", "filled_qty", "remaining_qty", "realized_pnl", "commission"):
            value = getattr(self, field)
            if field in {"submitted_qty", "filled_qty", "remaining_qty"} and value < -EPSILON:
                raise ValueError(f"{field} must be non-negative")
            if abs(value) <= EPSILON:
                setattr(self, field, 0.0)
        if self.remaining_qty <= EPSILON and self.submitted_qty > EPSILON:
            remaining = self.submitted_qty - self.filled_qty
            self.remaining_qty = 0.0 if abs(remaining) <= EPSILON else max(remaining, 0.0)
        return self


class ReconciliationStatus(str, Enum):
    OK = "ok"
    REFRESHED_PENDING_ORDER = "refreshed_pending_order"
    RECONSTRUCTED_POSITION = "reconstructed_position"
    CLEARED_PENDING_ORDER = "cleared_pending_order"
    HALTED = "halted"


class ReconciliationResult(BaseModel):
    model_config = {"extra": "forbid"}
    status: ReconciliationStatus
    reason: str
    exchange_position_qty: float = 0.0
    local_position_qty: float = 0.0
    open_orders_count: int = 0
    matched_fill_count: int = 0
    reconstructed_avg_entry_price: Optional[float] = None
    reconstructed_qty: float = 0.0
    pending_order_updated: bool = False
    halt_reason: Optional[str] = None
    applied_state_after: Optional[str] = None
    notes: List[str] = []

    @model_validator(mode="after")
    def _validate_qtys(self) -> "ReconciliationResult":
        for field in ("exchange_position_qty", "local_position_qty", "reconstructed_qty"):
            value = getattr(self, field)
            if abs(value) <= EPSILON:
                setattr(self, field, 0.0)
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
    probe:         Optional[dict] = None
