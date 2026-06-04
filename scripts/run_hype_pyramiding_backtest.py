from __future__ import annotations

import argparse
import csv
import itertools
import math
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.fetch_hype_hl_data import fetch_hype_data  # noqa: E402


INTERVALS = ["1d", "4h"]
REGIME_MODES = ["local", "daily_macro"]
ACTIVE_STATES = {"STARTER_LONG", "ADD1_LONG", "ADD2_LONG", "RUNNER_LONG"}
SUBWINDOWS = [
    ("w1", None, "2025-06-30"),
    ("w2", "2025-07-01", "2026-01-31"),
    ("w3", "2026-02-01", None),
]


@dataclass(frozen=True)
class BacktestConfig:
    phase: int
    config_id: str
    symbol: str = "HYPE"
    timeframe: str = "1d"
    regime_mode: str = "local"
    initial_equity: float = 10_000.0
    max_thesis_risk_pct: float = 0.01
    starter_stop_atr: float = 2.0
    risk_allocations: tuple[float, ...] = (1.0,)
    add1_trigger_atr: float = 1.0
    add2_trigger_atr: float = 2.0
    partial_tp_r: float | None = None
    runner_method: str = "none"
    giveback_pct: float = 0.35
    taker_fee_bps: float = 0.0
    cooldown_bars: int = 0
    max_drawdown_exclusion_pct: float = 40.0
    funding_block_threshold: float | None = None


@dataclass
class Lot:
    name: str
    entry_price: float
    qty: float


@dataclass
class TradeState:
    state: str = "FLAT"
    lots: list[Lot] = field(default_factory=list)
    trade_id: int = 0
    entry_time: str = ""
    entry_price: float = 0.0
    starter_stop: float = 0.0
    global_stop: float = 0.0
    atr_at_starter: float = 0.0
    add1_time: str = ""
    add1_price: float = 0.0
    add1_qty: float = 0.0
    add2_time: str = ""
    add2_price: float = 0.0
    add2_qty: float = 0.0
    partial_tp_time: str = ""
    partial_tp_price: float = 0.0
    partial_tp_qty: float = 0.0
    runner_start_time: str = ""
    max_unrealized_pnl: float = 0.0
    max_giveback_pct: float = 0.0
    fees_paid: float = 0.0
    funding_pnl: float = 0.0
    funding_notional_hours: float = 0.0
    realized_pnl: float = 0.0
    state_path: list[str] = field(default_factory=list)
    cooldown_remaining: int = 0


def _lot_notional(lots: list[Lot]) -> float:
    return sum(l.entry_price * l.qty for l in lots)


def _run_id() -> str:
    return "pyramiding_backtest_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else ["status"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_price_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "timestamp" in df.columns:
        df.index = pd.to_datetime(df.pop("timestamp"), utc=True)
    elif "date" in df.columns:
        df.index = pd.to_datetime(df.pop("date"), utc=True)
    else:
        df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def load_funding_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["funding_rate"], index=pd.DatetimeIndex([], tz="UTC"))
    df = pd.read_csv(path)
    if df.empty:
        return pd.DataFrame(columns=["funding_rate"], index=pd.DatetimeIndex([], tz="UTC"))
    df.index = pd.to_datetime(df.pop("timestamp"), utc=True, format="mixed")
    if "funding_rate" not in df.columns:
        raise ValueError("funding CSV must contain funding_rate")
    return df.sort_index()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma20"] = out["close"].rolling(20, min_periods=20).mean()
    out["ma100"] = out["close"].rolling(100, min_periods=100).mean()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14, min_periods=14).mean()
    up = out["high"].diff()
    down = -out["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = out["atr14"].replace(0, pd.NA)
    plus_di = 100.0 * plus_dm.rolling(14, min_periods=14).mean() / atr
    minus_di = 100.0 * minus_dm.rolling(14, min_periods=14).mean() / atr
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA) * 100.0
    out["adx14"] = dx.rolling(14, min_periods=14).mean()
    out["is_bull_local"] = out["close"] > out["ma100"]
    out["is_chop_adx"] = out["adx14"] < 20
    out["is_chop_ma_band"] = ((out["close"] - out["ma100"]).abs() <= out["atr14"]).rolling(5, min_periods=5).sum() >= 5
    return out


def apply_daily_macro(local_df: pd.DataFrame, daily_df: pd.DataFrame) -> pd.DataFrame:
    daily = compute_indicators(daily_df)
    # Daily candle close is only known after the daily candle completes.
    macro = pd.DataFrame({"daily_close": daily["close"], "daily_ma100": daily["ma100"]}, index=daily.index + pd.Timedelta(days=1))
    macro["is_bull_daily"] = macro["daily_close"] > macro["daily_ma100"]
    merged = local_df.copy()
    union_index = merged.index.union(macro.index)
    daily_ma100 = macro["daily_ma100"].reindex(union_index).sort_index().ffill()
    daily_bull = macro["is_bull_daily"].astype("boolean").reindex(union_index).sort_index().ffill()
    merged["daily_ma100"] = daily_ma100.reindex(merged.index)
    merged["is_bull_daily"] = daily_bull.reindex(merged.index).eq(True)
    return merged


def count_bull_episodes(df: pd.DataFrame, cfg: BacktestConfig) -> int:
    data = compute_indicators(df)
    if cfg.regime_mode == "daily_macro":
        data = apply_daily_macro(data, df if cfg.timeframe == "1d" else df)
    flags = data["is_bull_daily"] if cfg.regime_mode == "daily_macro" and "is_bull_daily" in data else data["is_bull_local"]
    count = 0
    prev = False
    for flag in flags.fillna(False).astype(bool):
        if flag and not prev:
            count += 1
        prev = bool(flag)
    return count


def calculate_position_qty(*, equity: float, max_thesis_risk_pct: float, allocation: float, entry_price: float, stop_price: float) -> float:
    distance = abs(entry_price - stop_price)
    if distance <= 0:
        return 0.0
    return equity * max_thesis_risk_pct * allocation / distance


def _avg_entry(lots: list[Lot]) -> float:
    qty = sum(l.qty for l in lots)
    return sum(l.qty * l.entry_price for l in lots) / qty if qty else 0.0


def _qty(lots: list[Lot]) -> float:
    return sum(l.qty for l in lots)


def _open_risk(lots: list[Lot], stop: float) -> float:
    return sum(max(l.entry_price - stop, 0.0) * l.qty for l in lots)


def _unrealized(lots: list[Lot], price: float) -> float:
    return sum((price - l.entry_price) * l.qty for l in lots)


def _fee(notional: float, cfg: BacktestConfig) -> float:
    return abs(notional) * cfg.taker_fee_bps / 10000.0


def _funding_between(
    funding: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    include_start: bool = False,
    include_end: bool = True,
) -> pd.DataFrame:
    if funding.empty:
        return funding
    left = funding.index >= start if include_start else funding.index > start
    right = funding.index <= end if include_end else funding.index < end
    return funding[left & right]


def _accrue_funding(
    st: TradeState,
    funding: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    include_start: bool = False,
    include_end: bool = True,
) -> float:
    if st.state not in ACTIVE_STATES:
        return 0.0
    notional = _lot_notional(st.lots)
    payment = 0.0
    rows = _funding_between(funding, start, end, include_start=include_start, include_end=include_end)
    for _, frow in rows.iterrows():
        payment += -notional * float(frow["funding_rate"])
    st.funding_pnl += payment
    st.funding_notional_hours += notional * len(rows)
    return payment


def _is_bull(row: pd.Series, cfg: BacktestConfig) -> bool:
    return bool(row["is_bull_daily"] if cfg.regime_mode == "daily_macro" else row["is_bull_local"])


def _close_below_regime(row: pd.Series, cfg: BacktestConfig) -> bool:
    if cfg.regime_mode == "daily_macro":
        return not bool(row.get("is_bull_daily", False))
    return bool(row["close"] < row["ma100"])


def _close_trade(ts: pd.Timestamp, price: float, reason: str, cfg: BacktestConfig, st: TradeState, trades: list[dict[str, Any]], equity: float) -> float:
    qty = _qty(st.lots)
    gross_pnl = _unrealized(st.lots, price)
    fee = _fee(qty * price, cfg)
    st.fees_paid += fee
    net_pnl = gross_pnl + st.funding_pnl - st.fees_paid
    trades.append(
        {
            "trade_id": st.trade_id,
            "symbol": cfg.symbol,
            "timeframe": cfg.timeframe,
            "regime_mode": cfg.regime_mode,
            "entry_time": st.entry_time,
            "entry_price": st.entry_price,
            "starter_qty": st.lots[0].qty if st.lots else 0.0,
            "starter_stop": st.starter_stop,
            "add1_time": st.add1_time,
            "add1_price": st.add1_price,
            "add1_qty": st.add1_qty,
            "add2_time": st.add2_time,
            "add2_price": st.add2_price,
            "add2_qty": st.add2_qty,
            "partial_tp_time": st.partial_tp_time,
            "partial_tp_price": st.partial_tp_price,
            "partial_tp_qty": st.partial_tp_qty,
            "runner_start_time": st.runner_start_time,
            "exit_time": ts.isoformat(),
            "exit_price": price,
            "exit_reason": reason,
            "final_qty_closed": qty,
            "gross_pnl": gross_pnl,
            "fees_paid": st.fees_paid,
            "funding_pnl": st.funding_pnl,
            "funding_notional_hours": st.funding_notional_hours,
            "net_pnl": net_pnl,
            "net_return_pct": net_pnl / equity * 100.0 if equity else 0.0,
            "max_unrealized_pnl": st.max_unrealized_pnl,
            "max_giveback_pct": st.max_giveback_pct,
            "bars_held": len(st.state_path),
            "state_path": ">".join(st.state_path),
        }
    )
    return net_pnl


def _effective_sample_size(data: pd.DataFrame, cfg: BacktestConfig) -> int:
    flags = data["is_bull_daily"] if cfg.regime_mode == "daily_macro" and "is_bull_daily" in data else data["is_bull_local"]
    count = 0
    prev = False
    for flag in flags.fillna(False).astype(bool):
        if flag and not prev:
            count += 1
        prev = bool(flag)
    return count


def run_single_backtest(df: pd.DataFrame, daily_df: pd.DataFrame, funding: pd.DataFrame, cfg: BacktestConfig) -> dict[str, Any]:
    data = compute_indicators(df)
    if cfg.regime_mode == "daily_macro":
        data = apply_daily_macro(data, daily_df)
    effective_sample_size = _effective_sample_size(data, cfg)
    equity = cfg.initial_equity
    equity_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    add_debug_rows: list[dict[str, Any]] = []
    st = TradeState()
    last_ts: pd.Timestamp | None = None

    for i in range(len(data) - 1):
        row = data.iloc[i]
        next_row = data.iloc[i + 1]
        ts = pd.Timestamp(data.index[i])
        next_ts = pd.Timestamp(data.index[i + 1])
        if pd.isna(row["ma100"]) or pd.isna(row["atr14"]):
            last_ts = ts
            continue

        if st.state in ACTIVE_STATES and last_ts is not None:
            _accrue_funding(st, funding, last_ts, ts)

        if st.state in ACTIVE_STATES:
            st.state_path.append(st.state)
            unreal = _unrealized(st.lots, float(row["close"]))
            st.max_unrealized_pnl = max(st.max_unrealized_pnl, unreal)
            if st.max_unrealized_pnl > 0:
                st.max_giveback_pct = max(st.max_giveback_pct, (st.max_unrealized_pnl - unreal) / st.max_unrealized_pnl * 100.0)

            exit_price = None
            exit_reason = None
            if float(row["low"]) <= st.global_stop:
                exit_price = st.global_stop
                exit_reason = "STOP_HIT"
            elif cfg.phase >= 4 and st.state == "RUNNER_LONG":
                if cfg.runner_method == "giveback" and st.max_unrealized_pnl > 0 and unreal <= st.max_unrealized_pnl * (1.0 - cfg.giveback_pct):
                    exit_price = float(next_row["open"])
                    exit_reason = "GIVEBACK_STOP"
                elif cfg.runner_method == "ma20" and float(row["close"]) < float(row["ma20"]):
                    exit_price = float(next_row["open"])
                    exit_reason = "MA20_TRAIL"
                elif cfg.runner_method == "chandelier" and float(row["close"]) < max(float(row["close"]), float(row["high"])) - 2.0 * float(row["atr14"]):
                    exit_price = float(next_row["open"])
                    exit_reason = "CHANDELIER_TRAIL"
            if exit_price is None and _close_below_regime(row, cfg):
                exit_price = float(next_row["open"])
                exit_reason = "REGIME_FLIP" if cfg.regime_mode == "daily_macro" else "MA100_BREAK"
            if exit_price is not None:
                _accrue_funding(st, funding, ts, next_ts, include_end=False)
                equity += _close_trade(next_ts, exit_price, str(exit_reason), cfg, st, trades, cfg.initial_equity)
                st = TradeState(state="COOLDOWN", cooldown_remaining=cfg.cooldown_bars)
                last_ts = ts
                equity_rows.append({"timestamp": ts.isoformat(), "equity": equity, "state": st.state, "position_qty": 0.0})
                continue

            if cfg.phase >= 4 and st.state != "RUNNER_LONG" and cfg.partial_tp_r:
                risk_r = max((st.entry_price - st.starter_stop) * _qty(st.lots), 0.0)
                if risk_r > 0 and unreal >= cfg.partial_tp_r * risk_r:
                    sell_qty = _qty(st.lots) * 0.5
                    fee = _fee(sell_qty * float(next_row["open"]), cfg)
                    st.partial_tp_time = next_ts.isoformat()
                    st.partial_tp_price = float(next_row["open"])
                    st.partial_tp_qty = sell_qty
                    st.runner_start_time = next_ts.isoformat()
                    st.fees_paid += fee
                    equity += sell_qty * (float(next_row["open"]) - _avg_entry(st.lots)) - fee
                    for lot in st.lots:
                        lot.qty *= 0.5
                    st.state = "RUNNER_LONG"

            if st.state == "STARTER_LONG" and cfg.phase >= 2:
                trigger = st.entry_price + cfg.add1_trigger_atr * st.atr_at_starter
                condition_met = float(row["close"]) >= trigger and _unrealized(st.lots, float(row["close"])) > 0
                add_debug_rows.append(
                    {
                        "config_id": cfg.config_id,
                        "bar_time": ts.isoformat(),
                        "close": float(row["close"]),
                        "trigger_threshold": trigger,
                        "atr_at_starter": st.atr_at_starter,
                        "condition_met": condition_met,
                    }
                )
                if condition_met:
                    entry = float(next_row["open"])
                    stop = st.global_stop
                    alloc = cfg.risk_allocations[1] if len(cfg.risk_allocations) > 1 else 0.0
                    q = calculate_position_qty(equity=cfg.initial_equity, max_thesis_risk_pct=cfg.max_thesis_risk_pct, allocation=alloc, entry_price=entry, stop_price=stop)
                    candidate = st.lots + [Lot("add1", entry, q)]
                    if q > 0 and _open_risk(candidate, stop) <= cfg.initial_equity * cfg.max_thesis_risk_pct:
                        fee = _fee(q * entry, cfg)
                        st.fees_paid += fee
                        equity -= fee
                        st.lots = candidate
                        st.add1_time = next_ts.isoformat()
                        st.add1_price = entry
                        st.add1_qty = q
                        st.global_stop = st.entry_price
                        st.state = "ADD1_LONG"
            elif st.state == "ADD1_LONG" and cfg.phase >= 3:
                trigger = st.entry_price + cfg.add2_trigger_atr * st.atr_at_starter
                if float(row["close"]) >= trigger and float(row["close"]) > _avg_entry(st.lots):
                    entry = float(next_row["open"])
                    stop = st.global_stop
                    alloc = cfg.risk_allocations[2] if len(cfg.risk_allocations) > 2 else 0.0
                    q = calculate_position_qty(equity=cfg.initial_equity, max_thesis_risk_pct=cfg.max_thesis_risk_pct, allocation=alloc, entry_price=entry, stop_price=stop)
                    candidate = st.lots + [Lot("add2", entry, q)]
                    if q > 0 and _open_risk(candidate, stop) <= cfg.initial_equity * cfg.max_thesis_risk_pct:
                        fee = _fee(q * entry, cfg)
                        st.fees_paid += fee
                        equity -= fee
                        st.lots = candidate
                        st.add2_time = next_ts.isoformat()
                        st.add2_price = entry
                        st.add2_qty = q
                        st.global_stop = st.add1_price or st.entry_price
                        st.state = "ADD2_LONG"

        elif st.state == "COOLDOWN":
            st.cooldown_remaining -= 1
            if st.cooldown_remaining <= 0:
                st.state = "FLAT"
        elif st.state == "FLAT" and _is_bull(row, cfg):
            if cfg.funding_block_threshold is not None and not funding.empty:
                recent = funding[funding.index <= ts].tail(1)
                if not recent.empty and float(recent.iloc[0]["funding_rate"]) > cfg.funding_block_threshold:
                    last_ts = ts
                    equity_rows.append({"timestamp": ts.isoformat(), "equity": equity, "state": st.state, "position_qty": 0.0})
                    continue
            entry = float(next_row["open"])
            stop = entry - cfg.starter_stop_atr * float(row["atr14"])
            q = calculate_position_qty(equity=equity, max_thesis_risk_pct=cfg.max_thesis_risk_pct, allocation=cfg.risk_allocations[0], entry_price=entry, stop_price=stop)
            if q > 0:
                fee = _fee(q * entry, cfg)
                st = TradeState(
                    state="STARTER_LONG",
                    lots=[Lot("starter", entry, q)],
                    trade_id=len(trades) + 1,
                    entry_time=next_ts.isoformat(),
                    entry_price=entry,
                    starter_stop=stop,
                    global_stop=stop,
                    atr_at_starter=float(row["atr14"]),
                    fees_paid=fee,
                    state_path=["STARTER_LONG"],
                )
                equity -= fee

        position_qty = _qty(st.lots) if st.state in ACTIVE_STATES else 0.0
        mark = float(row["close"])
        equity_rows.append({"timestamp": ts.isoformat(), "equity": equity + (_unrealized(st.lots, mark) if position_qty else 0.0), "state": st.state, "position_qty": position_qty})
        last_ts = ts

    if st.state in ACTIVE_STATES:
        last_ts_idx = pd.Timestamp(data.index[-1])
        last_price = float(data["close"].iloc[-1])
        if last_ts is not None:
            _accrue_funding(st, funding, last_ts, last_ts_idx, include_end=False)
        equity += _close_trade(last_ts_idx, last_price, "END_OF_DATA", cfg, st, trades, cfg.initial_equity)

    equity_df = pd.DataFrame(equity_rows)
    return {"summary": summarize(cfg, trades, equity_df, effective_sample_size), "trades": trades, "equity_curve": equity_df, "add_debug": add_debug_rows}


def summarize(cfg: BacktestConfig, trades: list[dict[str, Any]], equity_df: pd.DataFrame, effective_sample_size: int = 0) -> dict[str, Any]:
    net_pnls = [float(t["net_pnl"]) for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    returns = [float(t["net_return_pct"]) for t in trades]
    durations = [float(t["bars_held"]) for t in trades]
    equity = equity_df["equity"] if not equity_df.empty else pd.Series([cfg.initial_equity])
    max_dd = (equity / equity.cummax() - 1.0).min() * 100.0 if not equity.empty else 0.0
    fees = sum(float(t["fees_paid"]) for t in trades)
    funding = sum(float(t["funding_pnl"]) for t in trades)
    funding_notional_hours = sum(float(t.get("funding_notional_hours", 0.0)) for t in trades)
    gross = sum(float(t["gross_pnl"]) for t in trades)
    net = sum(net_pnls)
    add1 = sum(1 for t in trades if t["add1_time"])
    add2 = sum(1 for t in trades if t["add2_time"])
    runner = sum(1 for t in trades if t["runner_start_time"])
    positive = [p for p in net_pnls if p > 0]
    top_trade_contribution_pct = max(positive) / sum(positive) if positive and sum(positive) > 0 else 0.0
    single_trade_dominated = top_trade_contribution_pct > 0.60
    excluded_for_drawdown = abs(max_dd) > cfg.max_drawdown_exclusion_pct
    reasons = []
    if excluded_for_drawdown:
        reasons.append("MAX_DRAWDOWN_GT_40")
    if single_trade_dominated:
        reasons.append("SINGLE_TRADE_DOMINATED")
    net_return = net / cfg.initial_equity * 100.0
    net_return_over_max_dd = net_return / abs(max_dd) if max_dd else 0.0
    transitions = sum(str(t.get("state_path", "")).count(">") + 1 for t in trades)
    return {
        "phase": cfg.phase,
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "regime_mode": cfg.regime_mode,
        "config_id": cfg.config_id,
        "trade_count": len(trades),
        "effective_sample_size": effective_sample_size,
        "sample_size_warning": effective_sample_size < 30,
        "win_rate": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "gross_return_pct": gross / cfg.initial_equity * 100.0,
        "net_return_pct": net_return,
        "max_drawdown_pct": float(max_dd),
        "return_over_max_dd": net_return_over_max_dd,
        "net_return_over_max_dd": net_return_over_max_dd,
        "avg_trade_duration_bars": mean(durations) if durations else 0.0,
        "avg_trade_duration_hours": 0.0,
        "avg_win_pct": mean([p / cfg.initial_equity * 100.0 for p in wins]) if wins else 0.0,
        "avg_loss_pct": mean([p / cfg.initial_equity * 100.0 for p in losses]) if losses else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (math.inf if wins else 0.0),
        "fees_paid": fees,
        "funding_pnl": funding,
        "funding_notional_hours": funding_notional_hours,
        "add1_hit_rate": add1 / len(trades) * 100.0 if trades else 0.0,
        "add2_hit_rate": add2 / len(trades) * 100.0 if trades else 0.0,
        "runner_activation_rate": runner / len(trades) * 100.0 if trades else 0.0,
        "cooldown_bars": cfg.cooldown_bars,
        "state_transition_count": transitions,
        "top_trade_contribution_pct": top_trade_contribution_pct,
        "single_trade_dominated": single_trade_dominated,
        "excluded_for_drawdown": excluded_for_drawdown,
        "exclusion_reason": "|".join(reasons),
    }


def _phase_configs(symbol: str, phase: int, intervals: list[str], regime_modes: list[str]) -> list[BacktestConfig]:
    cfgs: list[BacktestConfig] = []
    if phase == 1:
        for interval, regime, stop in itertools.product(["1d"], ["local"], [1.5, 2.0, 2.5]):
            cfgs.append(BacktestConfig(phase=1, config_id=f"p1_{interval}_{regime}_stop{stop}", symbol=symbol, timeframe=interval, regime_mode=regime, starter_stop_atr=stop))
    elif phase == 2:
        for interval, regime, trig in itertools.product(["1d"], ["local"], [0.5, 1.0, 1.5, 2.0]):
            cfgs.append(BacktestConfig(phase=2, config_id=f"p2_{interval}_{regime}_a1{trig}", symbol=symbol, timeframe=interval, regime_mode=regime, risk_allocations=(0.6, 0.4), add1_trigger_atr=trig))
    elif phase == 3:
        for interval, regime, alloc, a2 in itertools.product(["1d"], ["local"], [(0.5, 0.3, 0.2), (0.4, 0.35, 0.25)], [1.5, 2.0, 2.5]):
            cfgs.append(BacktestConfig(phase=3, config_id=f"p3_{interval}_{regime}_{'-'.join(str(x) for x in alloc)}_a2{a2}", symbol=symbol, timeframe=interval, regime_mode=regime, risk_allocations=alloc, add1_trigger_atr=1.0, add2_trigger_atr=a2))
    elif phase == 4:
        for interval, regime, method in itertools.product(["1d"], ["local"], ["giveback", "ma20", "chandelier"]):
            cfgs.append(BacktestConfig(phase=4, config_id=f"p4_{interval}_{regime}_{method}", symbol=symbol, timeframe=interval, regime_mode=regime, risk_allocations=(0.5, 0.3, 0.2), add1_trigger_atr=1.0, add2_trigger_atr=2.0, partial_tp_r=2.0, runner_method=method))
    elif phase == 5:
        for interval, regime in itertools.product(["1d"], ["local"]):
            cfgs.append(BacktestConfig(phase=5, config_id=f"p5_{interval}_{regime}_cost", symbol=symbol, timeframe=interval, regime_mode=regime, risk_allocations=(0.5, 0.3, 0.2), add1_trigger_atr=1.0, add2_trigger_atr=2.0, partial_tp_r=2.0, runner_method="giveback", taker_fee_bps=3.5))
    elif phase == 6:
        # v1.1: 4h daily-macro sanity check only. 1h is skipped by default.
        cfgs.append(BacktestConfig(phase=6, config_id="p6_4h_daily_macro_sanity", symbol=symbol, timeframe="4h", regime_mode="daily_macro", risk_allocations=(0.5, 0.3, 0.2), add1_trigger_atr=1.0, add2_trigger_atr=2.0, partial_tp_r=2.0, runner_method="giveback", taker_fee_bps=3.5))
    elif phase == 7:
        for cooldown in [0, 3, 5, 10]:
            cfgs.append(BacktestConfig(phase=7, config_id=f"p7_1d_local_cd{cooldown}", symbol=symbol, timeframe="1d", regime_mode="local", risk_allocations=(0.5, 0.3, 0.2), add1_trigger_atr=1.0, add2_trigger_atr=2.0, partial_tp_r=2.0, runner_method="giveback", taker_fee_bps=3.5, cooldown_bars=cooldown))
    return cfgs


def _slice_window(df: pd.DataFrame, label: str) -> pd.DataFrame:
    _, start, end = next(w for w in SUBWINDOWS if w[0] == label)
    out = df
    if start:
        out = out[out.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        out = out[out.index <= pd.Timestamp(end, tz="UTC")]
    return out


def _add_phase1_subwindows(row: dict[str, Any], df: pd.DataFrame, daily: pd.DataFrame, funding: pd.DataFrame, cfg: BacktestConfig) -> None:
    genuine_positives: list[str] = []
    for label, _, _ in SUBWINDOWS:
        window = _slice_window(df, label)
        if len(window) < 110:
            value = 0.0
            top_contribution = 0.0
            genuinely_positive = False
        else:
            result = run_single_backtest(window, daily, funding, cfg)
            value = float(result["summary"]["net_return_pct"])
            top_contribution = float(result["summary"]["top_trade_contribution_pct"])
            genuinely_positive = value > 0 and top_contribution <= 0.60
        row[f"{label}_net_return_pct"] = value
        row[f"{label}_top_trade_contribution_pct"] = top_contribution
        row[f"{label}_genuinely_positive"] = genuinely_positive
        if genuinely_positive:
            genuine_positives.append(label)
    row["positive_only_in_w3"] = genuine_positives == ["w3"] or len(genuine_positives) <= 1


def _eligible(row: dict[str, Any]) -> bool:
    return not bool(row.get("excluded_for_drawdown")) and not bool(row.get("single_trade_dominated"))


def _simplicity_score(row: dict[str, Any]) -> tuple[int, int, int]:
    adds = float(row.get("add1_hit_rate", 0.0)) + float(row.get("add2_hit_rate", 0.0))
    runner = 1 if float(row.get("runner_activation_rate", 0.0)) > 0 else 0
    timeframe_rank = {"1d": 0, "4h": 1, "1h": 2}.get(str(row.get("timeframe")), 3)
    return (int(adds > 0) + int(adds > 50), runner, timeframe_rank)


def select_phase_winner(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [r for r in rows if _eligible(r)]
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda r: (float(r["net_return_over_max_dd"]), float(r["win_rate"]), -float(r["state_transition_count"])), reverse=True)
    best = ordered[0]
    for row in ordered[1:]:
        best_score = float(best["net_return_over_max_dd"])
        if best_score and float(row["net_return_over_max_dd"]) >= best_score * 0.95 and _simplicity_score(row) < _simplicity_score(best):
            best = row
    return best


def should_run_one_hour_sanity(reference_net_return_pct: float, four_hour_net_return_pct: float) -> bool:
    return abs(float(four_hour_net_return_pct) - float(reference_net_return_pct)) > 15.0


def run_all_phases(symbol: str, data_dir: Path, out_dir: Path, intervals: list[str] | None = None) -> dict[str, Any]:
    intervals = intervals or INTERVALS
    out_dir.mkdir(parents=True, exist_ok=True)
    daily = load_price_csv(data_dir / f"{symbol}_1d.csv")
    funding = load_funding_csv(data_dir / f"{symbol}_funding.csv")
    all_rows: list[dict[str, Any]] = []
    best_trade_log: list[dict[str, Any]] = []
    add_debug_log: list[dict[str, Any]] = []
    phase_winners: list[dict[str, Any]] = []
    config_by_id: dict[str, BacktestConfig] = {}
    for phase in range(1, 8):
        rows: list[dict[str, Any]] = []
        if phase == 6:
            best_so_far = select_phase_winner(all_rows)
            best_cfg = config_by_id.get(str(best_so_far["config_id"])) if best_so_far else None
            cfgs = []
            if best_cfg:
                cfgs.append(replace(
                    best_cfg,
                    phase=6,
                    config_id=f"p6_4h_daily_macro_{best_cfg.config_id}",
                    timeframe="4h",
                    regime_mode="daily_macro",
                ))
        else:
            best_so_far = None
            cfgs = _phase_configs(symbol, phase, intervals, REGIME_MODES if phase != 7 else ["local"])
        for cfg in cfgs:
            config_by_id[cfg.config_id] = cfg
            df = load_price_csv(data_dir / f"{symbol}_{cfg.timeframe}.csv")
            result = run_single_backtest(df, daily, funding, cfg)
            add_debug_log.extend(result.get("add_debug", []))
            if phase == 1:
                _add_phase1_subwindows(result["summary"], df, daily, funding, cfg)
            rows.append(result["summary"])
            if phase != 6 and _eligible(result["summary"]):
                all_rows.append(result["summary"])
            if phase != 6 and _eligible(result["summary"]) and (not best_trade_log or result["summary"]["net_return_over_max_dd"] >= max((float(r.get("net_return_over_max_dd", -10**9)) for r in all_rows), default=-10**9)):
                best_trade_log = result["trades"]
        if phase == 6 and rows and best_so_far:
            divergence = abs(float(rows[0]["net_return_pct"]) - float(best_so_far["net_return_pct"]))
            run_one_hour = should_run_one_hour_sanity(float(best_so_far["net_return_pct"]), float(rows[0]["net_return_pct"]))
            rows[0]["sanity_divergence_gt_15"] = run_one_hour
            rows[0]["one_hour_sanity_ran"] = False
            rows[0]["sanity_reference_config_id"] = best_so_far["config_id"]
            rows[0]["sanity_reference_net_return_pct"] = best_so_far["net_return_pct"]
            if run_one_hour and (data_dir / f"{symbol}_1h.csv").exists():
                best_cfg = config_by_id.get(str(best_so_far["config_id"]))
                if best_cfg:
                    cfg_1h = replace(
                        best_cfg,
                        phase=6,
                        config_id=f"p6_1h_conditional_{best_cfg.config_id}",
                        timeframe="1h",
                        regime_mode="daily_macro",
                    )
                    df_1h = load_price_csv(data_dir / f"{symbol}_1h.csv")
                    result_1h = run_single_backtest(df_1h, daily, funding, cfg_1h)
                    add_debug_log.extend(result_1h.get("add_debug", []))
                    result_1h["summary"]["sanity_divergence_gt_15"] = run_one_hour
                    result_1h["summary"]["one_hour_sanity_ran"] = True
                    result_1h["summary"]["sanity_reference_config_id"] = best_so_far["config_id"]
                    result_1h["summary"]["sanity_reference_net_return_pct"] = best_so_far["net_return_pct"]
                    rows.append(result_1h["summary"])
        winner = select_phase_winner(rows)
        if winner:
            phase_winners.append(winner)
        _write_csv(out_dir / f"phase{phase}_{_phase_name(phase)}.csv", rows)
    ranked = sorted(all_rows, key=lambda r: (float(r["net_return_over_max_dd"]), float(r["win_rate"]), -float(r["state_transition_count"])), reverse=True)
    _write_csv(out_dir / "ranked_configs.csv", ranked)
    _write_csv(out_dir / "trade_log_best.csv", best_trade_log)
    _write_csv(out_dir / "add_debug_log.csv", add_debug_log)
    _write_report(out_dir, ranked, phase_winners)
    return {"ranked": ranked, "trade_log_best": best_trade_log, "out_dir": out_dir, "phase_winners": phase_winners, "add_debug_log": add_debug_log}


def _phase_name(phase: int) -> str:
    return {
        1: "baseline",
        2: "one_add",
        3: "two_adds",
        4: "runner",
        5: "cost_adjusted",
        6: "timeframe_comparison",
        7: "cooldown",
    }[phase]


def _write_report(out_dir: Path, ranked: list[dict[str, Any]], phase_winners: list[dict[str, Any]]) -> None:
    best = ranked[0] if ranked else None
    recommendation = "NEEDS_MORE_DATA"
    phase1 = next((r for r in phase_winners if int(r["phase"]) == 1), None)
    positive_only_w3 = bool(phase1 and phase1.get("positive_only_in_w3"))
    survives_4h = any(
        int(r["phase"]) == 6
        and str(r.get("timeframe")) == "4h"
        and _eligible(r)
        and not bool(r.get("sanity_divergence_gt_15"))
        for r in phase_winners
    )
    beats_starter = bool(best and phase1 and float(best["net_return_over_max_dd"]) > float(phase1["net_return_over_max_dd"]))
    if positive_only_w3:
        recommendation = "NEEDS_MORE_DATA"
    elif best and best["trade_count"] >= 5:
        if int(best["phase"]) == 1:
            recommendation = "IMPLEMENT_STARTER_ONLY"
        elif int(best["phase"]) == 2 and beats_starter and survives_4h:
            recommendation = "IMPLEMENT_STARTER_PLUS_ONE_ADD"
        elif int(best["phase"]) >= 4 and beats_starter and survives_4h:
            recommendation = "IMPLEMENT_RUNNER_ONLY_AFTER_PARTIAL_TP"
        else:
            recommendation = "NEEDS_MORE_DATA"
    lines = [
        "# HYPE Pyramiding Backtest Report",
        "",
        "Research-only backtest. Signals are evaluated on candle close and fills happen at the next candle open.",
        "",
        "## Best Ranked Config",
        "",
        json_like(best) if best else "No ranked configs.",
        "",
        "## Recommendation",
        "",
        f"`{recommendation}`",
        "",
        "## Phase 1 Sub-Window Robustness",
        "",
        "| Window | Net Return % | Top Trade Contribution % | Genuinely Positive |",
        "|---|---:|---:|---|",
    ]
    if phase1:
        for label, _, _ in SUBWINDOWS:
            lines.append(
                "| "
                f"{label.upper()} | "
                f"{float(phase1.get(f'{label}_net_return_pct', 0.0)):.4f} | "
                f"{float(phase1.get(f'{label}_top_trade_contribution_pct', 0.0)):.4f} | "
                f"{bool(phase1.get(f'{label}_genuinely_positive', False))} |"
            )
    else:
        lines.append("| n/a | 0.0000 | 0.0000 | False |")
    lines.extend([
        "",
        "## Notes",
        "",
        "- Position sizing is dollar-risk-at-stop based.",
        "- Funding is applied from hourly funding rows while position is open.",
        "- Configs with max drawdown above 40% or top-trade contribution above 60% are excluded from final ranking but kept in raw phase CSVs.",
        "- v1.1 rankings are hypothesis generation, not statistical validation; sample size uses bull episodes, not bars or trades.",
        f"- Phase 1 positive-only-in-W3 downgrade active: `{positive_only_w3}`.",
        f"- 4h daily-macro sanity survived: `{survives_4h}`.",
    ])
    (out_dir / "backtest_report.md").write_text("\n".join(lines) + "\n")


def json_like(row: dict[str, Any] | None) -> str:
    if not row:
        return "{}"
    keys = ["phase", "config_id", "timeframe", "regime_mode", "trade_count", "net_return_pct", "max_drawdown_pct", "win_rate", "fees_paid", "funding_pnl"]
    return "\n".join(f"- {k}: `{row.get(k)}`" for k in keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run explicit HYPE pyramiding research backtest")
    parser.add_argument("--symbol", default="HYPE")
    parser.add_argument("--data-dir", default="data/hyperliquid")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--fetch-if-missing", action="store_true")
    parser.add_argument("--all-phases", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    needed = [data_dir / f"{args.symbol}_{i}.csv" for i in INTERVALS] + [data_dir / f"{args.symbol}_funding.csv"]
    if args.fetch_if_missing and any(not p.exists() for p in needed):
        fetch_hype_data(symbol=args.symbol, network=args.network, start=args.start, end=args.end, data_dir=data_dir, refresh=False)
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise FileNotFoundError("missing required data files: " + ", ".join(missing))
    out_dir = Path(args.output_dir) if args.output_dir else Path("reports/research") / args.symbol.lower() / ("pyramiding_backtest_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    result = run_all_phases(args.symbol, data_dir, out_dir)
    print("HYPE_PYRAMIDING_BACKTEST_COMPLETE")
    print("run_dir=" + str(result["out_dir"]))
    if result["ranked"]:
        print("best_config_id=" + str(result["ranked"][0]["config_id"]))
        print("best_net_return_pct=" + str(result["ranked"][0]["net_return_pct"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
