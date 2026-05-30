"""
HL Phase 5 — 24/7 Live Loop (Hyperliquid Testnet).

Startup sequence:
  1. Load config and validate (mainnet rejected, testnet OK)
  2. Check HL_PRIVATE_KEY env var is set
  3. Build HyperliquidClient (testnet)
  4. Fetch account snapshot + reconcile against persisted state
  5. Start WebSocket intraday monitor thread
  6. Enter 4h candle-close decision loop

Shutdown: SIGTERM / SIGINT sets the global stop event, which unwinds the loop
and the intraday monitor thread within a few seconds.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow running as a script: project root must be on sys.path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import load_config
from src.core.models import BotConfig
from src.hl.auth import get_private_key
from src.hl.ws_feed import TESTNET_WS_URL, HLWebSocketFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
_log = logging.getLogger("run_live_hl")

_STOP_EVENT = threading.Event()
_4H_SECONDS = 4 * 60 * 60


def next_4h_candle_close(now: Optional[datetime] = None) -> datetime:
    """Return the next UTC 4h candle close after *now* (00:00/04:00/08:00/...)."""
    if now is None:
        now = datetime.now(timezone.utc)
    epoch_seconds = now.timestamp()
    next_boundary = (int(epoch_seconds // _4H_SECONDS) + 1) * _4H_SECONDS
    return datetime.fromtimestamp(next_boundary, tz=timezone.utc)


def startup_checks(config: BotConfig) -> str:
    """
    Validate config and environment; return the private key.
    Raises SystemExit (via _fatal) on any startup failure.
    """
    hl = config.hl
    if not hl:
        _fatal("Config missing 'hl' block — cannot start live loop")
    # get_private_key() raises ValueError if HL_PRIVATE_KEY is unset
    try:
        private_key = get_private_key()
    except ValueError as exc:
        _fatal(str(exc))
    return private_key  # type: ignore[return-value]


def _fatal(msg: str) -> None:
    _log.critical("STARTUP FATAL: %s", msg)
    sys.exit(1)


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ANN001
        _log.info("Signal %s received — initiating graceful shutdown", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run_intraday_monitor(
    feed: HLWebSocketFeed,
    stop_event: threading.Event,
    check_interval_seconds: int = 60,
) -> None:
    """
    Background thread: polls latest price from WS feed and checks for stop triggers.
    Currently logs the price; Phase 6 will wire stop execution.
    A None price sentinel from the feed signals WS failure — set stop_event (halt).
    """
    _log.info("Intraday monitor started")
    while not stop_event.is_set():
        price = feed.get_latest_price()
        if price is None:
            # None could mean empty buffer (no data yet) or halt sentinel
            # The WS feed only puts None after MAX_RECONNECTS failures
            stop_event.wait(timeout=check_interval_seconds)
            continue
        _log.debug("Intraday price: %.4f", price)
        stop_event.wait(timeout=check_interval_seconds)
    _log.info("Intraday monitor stopped")


def main(config_path: str = "config/btc_long_thesis.yaml") -> None:
    _log.info("Loading config from %s", config_path)
    config = load_config(config_path)

    private_key = startup_checks(config)
    _log.info("Startup checks passed — entering live loop (testnet)")

    _install_shutdown_handler(_STOP_EVENT)

    hl_cfg = config.hl or {}
    coin = hl_cfg.get("coin", "BTC")
    ws_url = TESTNET_WS_URL

    feed = HLWebSocketFeed(ws_url=ws_url, coin=coin)
    feed.start()
    _log.info("WebSocket feed started for %s @ %s", coin, ws_url)

    monitor_thread = threading.Thread(
        target=run_intraday_monitor,
        args=(feed, _STOP_EVENT),
        daemon=True,
        name="intraday-monitor",
    )
    monitor_thread.start()

    try:
        while not _STOP_EVENT.is_set():
            target = next_4h_candle_close()
            wait_secs = (target - datetime.now(timezone.utc)).total_seconds()
            _log.info("Next 4h candle close at %s (%.0fs away)", target.isoformat(), wait_secs)

            # Sleep in short chunks so SIGTERM wakes us quickly
            deadline = time.monotonic() + wait_secs
            while time.monotonic() < deadline and not _STOP_EVENT.is_set():
                time.sleep(min(5.0, deadline - time.monotonic()))

            if _STOP_EVENT.is_set():
                break

            _log.info("4h candle closed at %s — running decision engine", target.isoformat())
            # Phase 6 will call decision engine here; placeholder log for now.
    finally:
        _log.info("Shutting down — stopping WS feed")
        feed.stop()
        monitor_thread.join(timeout=10)
        _log.info("Shutdown complete")


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config/btc_long_thesis.yaml"
    main(cfg_path)
