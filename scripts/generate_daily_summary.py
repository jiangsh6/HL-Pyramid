#!/usr/bin/env python
"""Generate a daily summary from the current state.json + latest OHLCV bar."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.core.config_loader import load_config
from src.data.indicators import calc_indicators
from src.reporting.daily_summary import format_summary
from src.reporting.state_writer import read_state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--data-csv", required=True)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    state_path = Path(cfg.logging["run_dir"]) / "state.json"
    if not state_path.exists():
        print(f"No state.json found at {state_path}", file=sys.stderr)
        return 1

    state = read_state(str(state_path))
    df = pd.read_csv(args.data_csv, parse_dates=["date"]).set_index("date")
    ind = calc_indicators(df, cfg)
    print(format_summary(state, ind, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
