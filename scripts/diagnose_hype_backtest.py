#!/usr/bin/env python3
"""
HYPE Pyramiding Backtest — Diagnostic Script for v1.1 Run
=========================================================

Purpose:
    Before accepting the IMPLEMENT_STARTER_ONLY recommendation from
    reports/research/hype/pyramiding_backtest_20260604T032442Z, validate
    that three suspected bugs are NOT corrupting the result:

    BUG 1: funding_pnl is inconsistent across phases
           Phase1 starter-only (9 trades): funding = -208
           Phase5 cost-adjusted (23 trades): funding = -45
           Same asset, same period. Should not be possible if funding is
           correctly attributed as Sigma(notional × funding_rate × hours).

    BUG 2: Add1 hit_rate = 0.0 across ALL Phase 2 configs
           Four different trigger ATRs (0.5, 1.0, 1.5, 2.0) produced
           IDENTICAL net returns. Add1 fired zero times in any of them.
           This means "adds add no value" was never actually tested.

    BUG 3: positive_only_in_w3 = False is technically true, but W1's
           +9.26% net return is almost entirely from a single 151-day
           trade. The Revision 4 downgrade was bypassed by one trade.

Usage:
    python diagnose_hype_backtest.py \\
        --run-dir reports/research/hype/pyramiding_backtest_20260604T032442Z \\
        --data-dir data/hyperliquid

This script does not modify the backtest. It only reads the run outputs
and the source data, recomputes funding and add-trigger conditions
independently, and prints a verdict for each bug.

Each verdict is one of:
    CONFIRMED  - the bug is present, the evidence is in the numbers below
    REFUTED    - the data does not support the suspicion
    INCONCLUSIVE - the run did not log enough information to tell

If any verdict is CONFIRMED, the IMPLEMENT_STARTER_ONLY recommendation
is invalid and the backtest must be rerun after fixing the underlying
code path.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ----------------------------- BUG 1 ---------------------------------

def diagnose_funding_attribution(run_dir: Path, data_dir: Path) -> str:
    """
    Recompute funding_pnl independently for the best Phase 1 config and
    compare against the value the backtest logged.

    Method:
        For each trade in trade_log_best.csv:
            1. Load HYPE_funding.csv hourly funding rows between entry_time
               and exit_time (inclusive of entry, exclusive of exit).
            2. For each funding timestamp t, take notional at t.
               For a starter-only trade with no adds, notional is constant
               from entry_price * starter_qty for the entire hold.
            3. funding_for_trade = -sum(notional_t × funding_rate_t) for longs.
        Sum across all 9 trades, compare to the funding_pnl logged in
        ranked_configs.csv for p1_1d_local_stop1.5 (= -208.47).

    Pass criterion:
        |recomputed - logged| / |logged| < 0.05
    """
    print("=" * 70)
    print("BUG 1: funding attribution consistency")
    print("=" * 70)

    trade_log = pd.read_csv(run_dir / "trade_log_best.csv")
    funding = pd.read_csv(data_dir / "HYPE_funding.csv")

    # Normalize funding timestamp column - the fetcher may have written
    # different column names; handle the common ones.
    ts_col = next(
        (c for c in funding.columns
         if c.lower() in ("time", "timestamp", "funding_time", "ts")),
        None,
    )
    rate_col = next(
        (c for c in funding.columns
         if c.lower() in ("funding_rate", "rate", "funding")),
        None,
    )
    if ts_col is None or rate_col is None:
        print(f"  INCONCLUSIVE: cannot find timestamp/rate columns in funding.csv")
        print(f"  available columns: {list(funding.columns)}")
        return "INCONCLUSIVE"

    funding[ts_col] = pd.to_datetime(funding[ts_col], utc=True, format="mixed")
    funding = funding.sort_values(ts_col).reset_index(drop=True)

    trade_log["entry_time"] = pd.to_datetime(trade_log["entry_time"], utc=True)
    trade_log["exit_time"] = pd.to_datetime(trade_log["exit_time"], utc=True)

    recomputed_total = 0.0
    per_trade_rows = []
    for _, t in trade_log.iterrows():
        mask = (funding[ts_col] >= t["entry_time"]) & (funding[ts_col] < t["exit_time"])
        slice_ = funding.loc[mask]
        # starter-only: notional constant = entry_price × starter_qty
        notional = t["entry_price"] * t["starter_qty"]
        # long: positive funding_rate is a cost to longs -> negative PnL
        trade_funding = -(notional * slice_[rate_col]).sum()
        recomputed_total += trade_funding
        per_trade_rows.append({
            "trade_id": t["trade_id"],
            "bars_held": t["bars_held"],
            "notional": round(notional, 2),
            "funding_hours": len(slice_),
            "logged_funding": round(t["funding_pnl"], 2),
            "recomputed_funding": round(trade_funding, 2),
            "delta": round(trade_funding - t["funding_pnl"], 2),
        })

    ranked = pd.read_csv(run_dir / "ranked_configs.csv")
    logged_total = float(
        ranked.loc[ranked["config_id"] == "p1_1d_local_stop1.5", "funding_pnl"].iloc[0]
    )

    print(pd.DataFrame(per_trade_rows).to_string(index=False))
    print()
    print(f"  Logged total (Phase1 best):     {logged_total:>10.2f}")
    print(f"  Independently recomputed total: {recomputed_total:>10.2f}")
    print(f"  Absolute delta:                 {abs(recomputed_total - logged_total):>10.2f}")

    if abs(logged_total) < 1e-6:
        verdict = "INCONCLUSIVE"
    elif abs(recomputed_total - logged_total) / abs(logged_total) < 0.05:
        verdict = "REFUTED"
    else:
        verdict = "CONFIRMED"

    print(f"\n  VERDICT: {verdict}")
    if verdict == "CONFIRMED":
        print("  ACTION: funding attribution in the backtest does NOT match")
        print("  Sigma(notional × funding_rate) over actual hold hours.")
        print("  Locate the funding accrual code path and fix before rerun.")
    return verdict


# ----------------------------- BUG 2 ---------------------------------

def diagnose_add1_never_fires(run_dir: Path, data_dir: Path) -> str:
    """
    Check whether the Add1 trigger condition was ever satisfiable on the
    daily HYPE data given the recorded Phase 1 entries.

    Method:
        Replay the 9 Phase-1 entries from trade_log_best.csv. For each
        trade, walk daily bars from entry_time to exit_time. At each bar,
        compute:
            condition_met[trigger_atr] = (close >= entry_price + trigger_atr * ATR_at_entry)
                                         AND (close > entry_price)
        For trigger_atr in [0.5, 1.0, 1.5, 2.0], count how many bars
        across all trades would have triggered Add1.

    Pass criterion (REFUTING the bug):
        At least one trigger value has >0 satisfied bars in trades that
        eventually closed profitably, AND the Phase 2 CSV records that
        Add1 fired at least once for that trigger.

    If satisfied bars exist in the raw data but add1_hit_rate = 0.0 in
    Phase 2, the bug is CONFIRMED (the trigger logic in the backtest is
    broken, not the trigger threshold).
    """
    print("\n" + "=" * 70)
    print("BUG 2: Add1 trigger never fires across all Phase 2 configs")
    print("=" * 70)

    trade_log = pd.read_csv(run_dir / "trade_log_best.csv")
    daily = pd.read_csv(data_dir / "HYPE_1d.csv")

    ts_col = next(
        (c for c in daily.columns if c.lower() in ("time", "timestamp", "date", "open_time")),
        None,
    )
    if ts_col is None:
        print(f"  INCONCLUSIVE: cannot find timestamp column in HYPE_1d.csv")
        print(f"  available columns: {list(daily.columns)}")
        return "INCONCLUSIVE"

    daily[ts_col] = pd.to_datetime(daily[ts_col], utc=True)
    daily = daily.sort_values(ts_col).reset_index(drop=True)

    # ATR14 on daily
    high = daily["high"]
    low = daily["low"]
    close = daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    daily["atr14"] = tr.rolling(14).mean()

    trade_log["entry_time"] = pd.to_datetime(trade_log["entry_time"], utc=True)
    trade_log["exit_time"] = pd.to_datetime(trade_log["exit_time"], utc=True)

    triggers = [0.5, 1.0, 1.5, 2.0]
    fired_counts = {trig: 0 for trig in triggers}
    fired_in_profitable = {trig: 0 for trig in triggers}

    for _, t in trade_log.iterrows():
        entry_row = daily.loc[daily[ts_col] == t["entry_time"]]
        if entry_row.empty or pd.isna(entry_row["atr14"].iloc[0]):
            continue
        atr_at_entry = entry_row["atr14"].iloc[0]
        entry_price = t["entry_price"]
        profitable = t["gross_pnl"] > 0

        mask = (daily[ts_col] > t["entry_time"]) & (daily[ts_col] <= t["exit_time"])
        bars = daily.loc[mask]
        for trig in triggers:
            threshold = entry_price + trig * atr_at_entry
            n_fired = ((bars["close"] >= threshold) & (bars["close"] > entry_price)).sum()
            fired_counts[trig] += n_fired
            if profitable:
                fired_in_profitable[trig] += n_fired

    print("  Trigger    Bars-eligible-across-all-trades   In-profitable-trades")
    for trig in triggers:
        print(f"  +{trig:>3.1f} ATR        {fired_counts[trig]:>10d}                {fired_in_profitable[trig]:>10d}")

    # Compare to logged
    p2 = pd.read_csv(run_dir / "phase2_one_add.csv")
    logged_hit = float(p2["add1_hit_rate"].max())
    print(f"\n  Phase 2 logged max add1_hit_rate: {logged_hit}")

    eligible_exists = max(fired_counts.values()) > 0
    if eligible_exists and logged_hit == 0.0:
        verdict = "CONFIRMED"
    elif not eligible_exists:
        # Threshold genuinely unreachable on this data with these stops
        verdict = "REFUTED_BUT_NOTE"
        print("  (Note: no bar satisfied the trigger anywhere - threshold may")
        print("  be too high relative to MA100 exit timing on daily HYPE.")
        print("  Re-test with lower trigger floors or different timeframe.)")
    else:
        verdict = "REFUTED"

    print(f"\n  VERDICT: {verdict}")
    if verdict == "CONFIRMED":
        print("  ACTION: bars existed where Add1 should have fired, but it")
        print("  never did. The add-trigger evaluation code is broken.")
        print("  Likely culprits: ATR_at_entry not captured at signal time,")
        print("  trigger compared against wrong reference price, or state")
        print("  machine blocking ADD1_LONG transition.")
    return verdict


# ----------------------------- BUG 3 ---------------------------------

def diagnose_w1_single_trade_dependence(run_dir: Path) -> str:
    """
    Check whether W1's positive net return is dominated by one trade.

    Method:
        Take the 9 trades from trade_log_best.csv. Assign each to W1/W2/W3
        by entry_time:
            W1: listing -> 2025-06-30
            W2: 2025-07-01 -> 2026-01-31
            W3: 2026-02-01 -> end
        For each window, compute:
            window_top_contribution = max(net_pnl) / sum(positive net_pnl)

    Pass criterion (REFUTING the bug):
        window_top_contribution < 0.60 for every window with at least 2
        profitable trades. (The same threshold the backtest itself uses
        for the full-period SINGLE_TRADE_DOMINATED check.)

    If any window passes 0.60, the Revision 4 sub-window check is being
    bypassed by single-trade dominance INSIDE a window. The full-period
    flag was designed to catch this; it should be replicated per-window.
    """
    print("\n" + "=" * 70)
    print("BUG 3: W1's positive return comes from one trade, bypassing Revision 4")
    print("=" * 70)

    tl = pd.read_csv(run_dir / "trade_log_best.csv")
    tl["entry_time"] = pd.to_datetime(tl["entry_time"], utc=True)

    def window_of(ts: pd.Timestamp) -> str:
        if ts < pd.Timestamp("2025-07-01", tz="UTC"):
            return "W1"
        if ts < pd.Timestamp("2026-02-01", tz="UTC"):
            return "W2"
        return "W3"

    tl["window"] = tl["entry_time"].apply(window_of)

    summary_rows = []
    for w in ["W1", "W2", "W3"]:
        sub = tl[tl["window"] == w]
        if sub.empty:
            summary_rows.append({"window": w, "trades": 0, "net_total": 0.0,
                                "top_net": 0.0, "top_contrib_of_positives": None})
            continue
        positive_sum = sub.loc[sub["net_pnl"] > 0, "net_pnl"].sum()
        top_net = sub["net_pnl"].max()
        contrib = top_net / positive_sum if positive_sum > 0 else None
        summary_rows.append({
            "window": w,
            "trades": len(sub),
            "net_total": round(sub["net_pnl"].sum(), 2),
            "top_net": round(top_net, 2),
            "top_contrib_of_positives": round(contrib, 3) if contrib is not None else None,
        })

    print(pd.DataFrame(summary_rows).to_string(index=False))

    # Bug confirmed if any window's net total is positive AND that comes
    # from a single trade dominating its window, unless the rerun report
    # explicitly replicated the per-window dominance check and therefore
    # no longer lets that window count as genuinely positive.
    confirmed = False
    for row in summary_rows:
        if (row["net_total"] > 0 and row["top_contrib_of_positives"] is not None
                and row["top_contrib_of_positives"] > 0.60):
            confirmed = True

    if confirmed:
        phase1_path = run_dir / "phase1_baseline.csv"
        report_path = run_dir / "backtest_report.md"
        if phase1_path.exists() and report_path.exists():
            phase1 = pd.read_csv(phase1_path)
            report_text = report_path.read_text()
            required_cols = {
                "w1_top_trade_contribution_pct",
                "w2_top_trade_contribution_pct",
                "w3_top_trade_contribution_pct",
                "w1_genuinely_positive",
                "w2_genuinely_positive",
                "w3_genuinely_positive",
            }
            if required_cols.issubset(set(phase1.columns)) and "Top Trade Contribution" in report_text:
                best_row = phase1.iloc[0]
                captured = True
                for label in ("w1", "w2", "w3"):
                    top = float(best_row.get(f"{label}_top_trade_contribution_pct", 0.0))
                    genuine = str(best_row.get(f"{label}_genuinely_positive", "False")).lower() == "true"
                    if top > 0.60 and genuine:
                        captured = False
                if captured:
                    confirmed = False

    verdict = "CONFIRMED" if confirmed else "REFUTED"
    print(f"\n  VERDICT: {verdict}")
    if confirmed:
        print("  ACTION: Revision 4's positive_only_in_w3 flag is too coarse.")
        print("  It checks 'positive in multiple windows' but not 'positive")
        print("  for non-single-trade reasons.' Add per-window")
        print("  top_trade_contribution test mirroring the full-period rule.")
    return verdict


# ----------------------------- main ---------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, type=Path,
                   help="path to reports/research/hype/pyramiding_backtest_<ts>")
    p.add_argument("--data-dir", required=True, type=Path,
                   help="path to data/hyperliquid containing HYPE_1d.csv and HYPE_funding.csv")
    args = p.parse_args()

    for name, path in [("run dir", args.run_dir), ("data dir", args.data_dir)]:
        if not path.exists():
            print(f"ERROR: {name} not found: {path}", file=sys.stderr)
            return 2

    verdicts = {
        "BUG 1 (funding attribution)":   diagnose_funding_attribution(args.run_dir, args.data_dir),
        "BUG 2 (Add1 never fires)":      diagnose_add1_never_fires(args.run_dir, args.data_dir),
        "BUG 3 (W1 single-trade bypass)": diagnose_w1_single_trade_dependence(args.run_dir),
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for k, v in verdicts.items():
        print(f"  {k:<40} {v}")

    any_confirmed = any(v == "CONFIRMED" for v in verdicts.values())
    print()
    if any_confirmed:
        print("At least one bug is CONFIRMED. The IMPLEMENT_STARTER_ONLY")
        print("recommendation rests on a broken execution path. Fix the")
        print("confirmed bug(s), then rerun the full backtest pipeline.")
        return 1
    report_path = args.run_dir / "backtest_report.md"
    recommendation = "unknown"
    if report_path.exists():
        lines = report_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "## Recommendation" and i + 2 < len(lines):
                recommendation = lines[i + 2].strip("` ")
                break
    print("No bugs confirmed by independent recomputation.")
    print(f"The current report recommendation is: {recommendation}")
    print("Interpret it with the effective-sample-size caveat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
