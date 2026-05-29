from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


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
    lot_id:      str
    entry_price: float
    shares:      int
    entry_date:  date


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


class ThesisState(BaseModel):
    symbol:               str
    state:                BotState           = BotState.FLAT
    prior_state:          Optional[BotState] = None
    protect_profit_mode:  bool               = False
    runner_mode_active:   bool               = False
    runner_target_shares: Optional[int]      = None

    thesis_enabled: bool          = True
    entry_date:     Optional[date] = None

    base_lot:   Optional[LotRecord] = None
    addon_lots: List[LotRecord]     = []

    avg_entry_price:          Optional[float] = None
    current_position_shares:  int             = 0
    current_notional:         float           = 0.0

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

    halted:       bool           = False
    halt_reason:  Optional[str]  = None
    last_action:  Optional[str]  = None
    last_updated: Optional[datetime] = None


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
    shares:                   int                        = 0
    reason:                   str
    blockers:                 List[str]                  = []
    new_state:                Optional[BotState]         = None
    new_protect_profit_mode:  Optional[bool]             = None
    indicators:               Optional[IndicatorSnapshot] = None


# ── Fill ──────────────────────────────────────────────────────────────────────

class Fill(BaseModel):
    action:       ActionType
    shares:       int
    fill_price:   float
    slippage_bps: float
    realized_pnl: float   = 0.0
    commission:   float   = 0.0
    timestamp:    datetime


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
