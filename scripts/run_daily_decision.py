#!/usr/bin/env python
"""
Run a single EOD decision cycle:
  1. Load config and state.json
  2. Compute indicators
  3. Run decision_engine
  4. If action: paper_broker.execute() → apply_fill()
  5. Write all 4 CSVs + state.json + daily_summary.txt

Usage:
  python scripts/run_daily_decision.py --config config/mu_long_thesis.yaml --data-csv path/to/ohlcv.csv

The --data-csv argument is required in Phase 3; yfinance wiring is Phase 4.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Allow running this script directly without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.core.config_loader import config_snapshot_hash, load_config
from src.core.decision_engine import run as run_engine
from src.core.models import ActionType, ThesisState
from src.data.indicators import calc_indicators
from src.execution.paper_broker import execute as paper_execute
from src.reporting.daily_summary import format_summary
from src.reporting.logger import log_cycle
from src.reporting.state_writer import apply_fill, read_state, write_state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--data-csv", required=True,
                        help="Pre-fetched OHLCV CSV (yfinance fetch is Phase 4).")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg_hash = config_snapshot_hash(cfg)
    run_dir = cfg.logging["run_dir"]
    state_path = Path(run_dir) / "state.json"

    if state_path.exists():
        state = read_state(str(state_path))
    else:
        state = ThesisState(symbol=cfg.symbol["ticker"])

    df = pd.read_csv(args.data_csv, parse_dates=["date"]).set_index("date")
    ind = calc_indicators(df, cfg)

    state_before = state.state.value
    decision = run_engine(state, ind, cfg)
    fill = None

    if decision.action not in (ActionType.NO_ACTION, ActionType.HALT):
        # In paper mode: fill at next-bar open.  When the script is run EOD on
        # bar T with no T+1 yet available, fall back to bar T close as the
        # reference; backtester uses true next-bar open.
        fill_ref = float(df["open"].iloc[-1])
        fill = paper_execute(decision, fill_ref, state, cfg)
        apply_fill(state, fill)
        if decision.new_state is not None and decision.new_state != state.state:
            state.state = decision.new_state

    state.last_action = decision.action.value
    state.last_updated = datetime.now()

    equity = cfg.capital["starting_equity"]
    state_after = state.state.value

    log_cycle(
        run_dir=run_dir, state=state, decision=decision, fill=fill,
        indicators=ind, equity=equity,
        state_before=state_before, state_after=state_after,
        config_hash=cfg_hash,
    )
    write_state(state, str(state_path))

    summary = format_summary(
        state, ind, cfg,
        action_label=decision.action.value.upper(),
        reason=decision.reason, blockers=decision.blockers,
    )
    print(summary)
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "daily_summary.txt").write_text(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
