"""
Hyperliquid REST client — read-only info endpoint.

HyperliquidClient wraps POST /info requests with:
  - Testnet / mainnet URL switching from config
  - 3 retries with exponential back-off (0.5 s, 1.0 s)
  - 10-second timeout per request
  - No authentication (the /info endpoint is public)

Only the info (read-only) endpoint is used in Phase 1.
Order placement (POST /exchange) is Phase 4.
"""
from __future__ import annotations

import time
from typing import Any, Dict

try:
    import requests
except ImportError as exc:
    raise ImportError(
        "requests is required for Hyperliquid API access. "
        "Install it with: pip install requests"
    ) from exc

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"

_RETRYABLE = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)


class HyperliquidClient:
    """
    Thin wrapper around Hyperliquid's POST /info endpoint.

    Parameters
    ----------
    network : str
        "testnet" or "mainnet".
    timeout : float
        Per-request timeout in seconds (default 10).
    max_retries : int
        Total attempts before giving up (default 3).
    """

    def __init__(
        self,
        network: str = "testnet",
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        if network not in ("testnet", "mainnet"):
            raise ValueError(
                f"network must be 'testnet' or 'mainnet'; got '{network}'"
            )
        self.network  = network
        self.base_url = TESTNET_URL if network == "testnet" else MAINNET_URL
        self._timeout     = timeout
        self._max_retries = max_retries

    def post_info(self, payload: Dict[str, Any]) -> Any:
        """
        POST {base_url}/info with JSON payload.

        Retries on Timeout and ConnectionError with exponential back-off
        (0.5 s after attempt 1, 1.0 s after attempt 2).
        HTTP 4xx errors are raised immediately (not retried).

        Returns
        -------
        Any
            Parsed JSON response body.

        Raises
        ------
        requests.exceptions.Timeout | requests.exceptions.ConnectionError
            If all retries are exhausted.
        requests.exceptions.HTTPError
            On non-2xx HTTP status (4xx raised immediately, 5xx after retries).
        """
        last_exc: Exception = RuntimeError("post_info: no attempts made")

        for attempt in range(self._max_retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/info",
                    json=payload,
                    timeout=self._timeout,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.HTTPError:
                # 4xx: client error — don't retry
                raise

            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5 s, 1.0 s

        raise last_exc
