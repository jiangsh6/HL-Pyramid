"""
HL Phase 5 — Standalone Intraday Stop Monitor.

Runs independently of the 4h decision loop.  Connects to the Hyperliquid
WebSocket, streams allMids prices, and checks configured stop levels against
the current position every N seconds.

Shutdown: SIGTERM / SIGINT triggers graceful stop.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import load_config
from src.hl.auth import get_private_key
from src.hl.ws_feed import TESTNET_WS_URL, HLWebSocketFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
_log = logging.getLogger("run_intraday_hl")

_STOP_EVENT = threading.Event()


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ANN001
        _log.info("Signal %s received — stopping intraday monitor", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main(config_path: str = "config/btc_long_thesis.yaml") -> None:
    config = load_config(config_path)

    hl_cfg = config.hl or {}
    if not hl_cfg:
        _log.critical("Config missing 'hl' block — cannot start intraday monitor")
        sys.exit(1)

    try:
        get_private_key()
    except ValueError as exc:
        _log.critical("Startup failed: %s", exc)
        sys.exit(1)

    _install_shutdown_handler(_STOP_EVENT)

    coin = hl_cfg.get("coin", "BTC")
    ws_url = TESTNET_WS_URL
    check_interval = config.data.get("intraday_check_interval_minutes", 15) * 60

    feed = HLWebSocketFeed(ws_url=ws_url, coin=coin)
    feed.start()
    _log.info("Intraday monitor running for %s (check every %ds)", coin, check_interval)

    try:
        while not _STOP_EVENT.is_set():
            price = feed.get_latest_price()
            if price is not None:
                _log.info("Intraday price %s: %.4f", coin, price)
                # Phase 6 will evaluate stop levels against price here.
            _STOP_EVENT.wait(timeout=check_interval)
    finally:
        feed.stop()
        _log.info("Intraday monitor stopped")


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config/btc_long_thesis.yaml"
    main(cfg_path)
