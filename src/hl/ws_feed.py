"""
HL Phase 5 — WebSocket price feed.

Subscribes to Hyperliquid allMids channel and buffers the latest mid price
for the configured coin.  Runs a reconnect loop with exponential backoff;
after MAX_RECONNECTS failures a None sentinel is placed on the buffer to
signal the caller that the feed is dead and a HALT may be needed.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Optional

import websocket

_log = logging.getLogger(__name__)

TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"

_BACKOFF_SECONDS = [1, 2, 4, 8, 16]


class HLWebSocketFeed:
    MAX_RECONNECTS = 5

    def __init__(self, ws_url: str, coin: str) -> None:
        self.ws_url = ws_url
        self.coin = coin
        self._price_buffer: queue.Queue[Optional[float]] = queue.Queue(maxsize=100)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reconnect_attempts = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="hl-ws-feed"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def get_latest_price(self) -> Optional[float]:
        """Drain the buffer and return the most recent price, or None if empty."""
        price: Optional[float] = None
        try:
            while True:
                price = self._price_buffer.get_nowait()
        except queue.Empty:
            pass
        return price

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
                self._reconnect_attempts = 0
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                if self._reconnect_attempts >= self.MAX_RECONNECTS:
                    _log.error("WS feed: max reconnects (%d) reached — signalling halt", self.MAX_RECONNECTS)
                    self._price_buffer.put(None)
                    return
                delay = _BACKOFF_SECONDS[min(self._reconnect_attempts, len(_BACKOFF_SECONDS) - 1)]
                _log.warning(
                    "WS feed disconnected (%s), retry %d/%d in %ds",
                    exc, self._reconnect_attempts + 1, self.MAX_RECONNECTS, delay,
                )
                self._reconnect_attempts += 1
                self._stop_event.wait(timeout=delay)

    def _connect_and_run(self) -> None:
        ws = websocket.create_connection(self.ws_url, timeout=10)
        try:
            ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
            while not self._stop_event.is_set():
                ws.settimeout(5)
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                price = self._parse_price(msg)
                if price is not None:
                    self._put_price(price)
        finally:
            ws.close()

    def _put_price(self, price: float) -> None:
        try:
            self._price_buffer.put_nowait(price)
        except queue.Full:
            try:
                self._price_buffer.get_nowait()
            except queue.Empty:
                pass
            self._price_buffer.put_nowait(price)

    def _parse_price(self, message: str) -> Optional[float]:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return None
        if data.get("channel") != "allMids":
            return None
        mids = data.get("data", {}).get("mids", {})
        raw = mids.get(self.coin)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
