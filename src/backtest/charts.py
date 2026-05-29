"""
Backtest charts (Section 21.5).

Five charts, all via matplotlib:
  1. Price chart with entry/add/reduce/TP/stop markers
  2. Equity curve vs buy-and-hold
  3. Drawdown curve
  4. Exposure over time
  5. Position size over time

generate_charts(result, config, output_dir) saves PNG files.
If matplotlib is unavailable, raises ImportError with a clear message.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backtest.backtester import BacktestResult
    from src.core.models import BotConfig


def _require_matplotlib():
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        return matplotlib, plt, mdates
    except ImportError as exc:
        raise ImportError(
            "matplotlib is required for chart generation. "
            "Install it with: pip install matplotlib>=3.8"
        ) from exc


def generate_charts(
    result: "BacktestResult",
    config: "BotConfig",
    output_dir: str,
) -> None:
    """
    Generate and save all 5 backtest charts as PNG files.

    Parameters
    ----------
    result : BacktestResult
        Output of backtester.run_backtest().
    config : BotConfig
        Loaded bot configuration (used for equity baseline and labels).
    output_dir : str
        Directory path where PNGs are written.
    """
    matplotlib, plt, mdates = _require_matplotlib()

    records = result.records
    if not records:
        return

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    equity = config.capital["starting_equity"]
    ticker = config.symbol.get("ticker", "TICKER")

    from src.core.models import ActionType

    dates       = [r.bar_date for r in records]
    closes      = [r.adj_close for r in records]
    positions   = [r.shares for r in records]
    exposures   = [r.exposure_pct * 100 for r in records]  # percent

    # ── 1. Price chart with action markers ────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(dates, closes, color="steelblue", linewidth=1.2, label="Price (adj close)")

    # Trailing stop overlay
    stops = [r.trailing_stop_price for r in records]
    valid_stops = [(d, s) for d, s in zip(dates, stops) if s is not None]
    if valid_stops:
        sd, sv = zip(*valid_stops)
        ax.plot(sd, sv, color="orange", linewidth=0.8, linestyle="--", label="Trailing stop")

    _marker_map = {
        ActionType.BUY_STARTER:          ("^", "green",  "Entry (starter)",  80),
        ActionType.BUY_BASE:             ("^", "limegreen", "Entry (base)",   80),
        ActionType.BUY_ADDON:            ("^", "cyan",   "Add",              60),
        ActionType.SELL_REDUCE_ADDON:    ("v", "gold",   "Reduce addon",     60),
        ActionType.SELL_REDUCE_BASE:     ("v", "orange", "Reduce base",      60),
        ActionType.SELL_TAKE_PROFIT:     ("*", "purple", "Take profit",      80),
        ActionType.SELL_STOP:            ("x", "red",    "Hard stop",        100),
        ActionType.SELL_TRAILING_STOP:   ("x", "darkred","Trailing stop",    100),
        ActionType.EXIT_ALL:             ("X", "red",    "Exit all",         100),
        ActionType.SELL_EVENT_DERISKING: ("D", "brown",  "Event de-risk",    60),
    }

    plotted_labels: set = set()
    for rec in records:
        if rec.action in _marker_map and rec.fill_price is not None:
            marker, color, label, size = _marker_map[rec.action]
            lbl = label if label not in plotted_labels else None
            ax.scatter(rec.bar_date, rec.fill_price,
                       marker=marker, color=color, s=size,
                       zorder=5, label=lbl)
            plotted_labels.add(label)

    ax.set_title(f"{ticker} — Price & Trade Markers")
    ax.set_xlabel("Date")
    ax.set_ylabel("Price ($)")
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(Path(output_dir) / "01_price_markers.png", dpi=150)
    plt.close(fig)

    # ── 2. Equity curve vs buy-and-hold ───────────────────────────────────────
    running_realized = 0.0
    eq_curve = []
    bah_curve = []
    first_close = closes[0] if closes else 1.0

    for rec in records:
        if rec.fill_shares < 0 and rec.fill_price and rec.avg_entry_price:
            pnl = (rec.fill_price - rec.avg_entry_price) * abs(rec.fill_shares)
            running_realized += pnl
        unrealized = 0.0
        if rec.shares > 0 and rec.avg_entry_price:
            unrealized = (rec.adj_close - rec.avg_entry_price) * rec.shares
        eq_curve.append(equity + running_realized + unrealized)
        bah_shares = equity / first_close if first_close > 0 else 1
        bah_curve.append(bah_shares * rec.adj_close)

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(dates, eq_curve,  color="steelblue",  label="Strategy equity",      linewidth=1.2)
    ax.plot(dates, bah_curve, color="gray",        label="Buy-and-hold equity",  linewidth=1.0, linestyle="--")
    ax.axhline(equity, color="black", linewidth=0.6, linestyle=":")
    ax.set_title(f"{ticker} — Equity Curve vs Buy-and-Hold")
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio Value ($)")
    ax.legend()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(Path(output_dir) / "02_equity_curve.png", dpi=150)
    plt.close(fig)

    # ── 3. Drawdown curve ──────────────────────────────────────────────────────
    peak = eq_curve[0] if eq_curve else equity
    drawdowns = []
    for v in eq_curve:
        peak = max(peak, v)
        dd = (peak - v) / peak if peak > 0 else 0.0
        drawdowns.append(-dd * 100)   # negative percentage

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(dates, drawdowns, 0, color="red", alpha=0.3)
    ax.plot(dates, drawdowns, color="red", linewidth=0.8)
    ax.set_title(f"{ticker} — Drawdown (%)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(Path(output_dir) / "03_drawdown.png", dpi=150)
    plt.close(fig)

    # ── 4. Exposure over time ─────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(dates, exposures, 0, color="steelblue", alpha=0.35)
    ax.plot(dates, exposures, color="steelblue", linewidth=0.8)
    ax.set_title(f"{ticker} — Exposure (% of equity)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Exposure (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(Path(output_dir) / "04_exposure.png", dpi=150)
    plt.close(fig)

    # ── 5. Position size over time ─────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(dates, positions, 0, color="green", alpha=0.3)
    ax.plot(dates, positions, color="green", linewidth=0.8)
    ax.set_title(f"{ticker} — Position Size (shares)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Shares held")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(Path(output_dir) / "05_position_size.png", dpi=150)
    plt.close(fig)
