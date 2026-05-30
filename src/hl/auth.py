"""
Private key management — Phase 4.

SECURITY INVARIANTS (must never be violated):
  - HL_PRIVATE_KEY is NEVER logged
  - HL_PRIVATE_KEY is NEVER included in any exception message or traceback
  - HL_PRIVATE_KEY is NEVER written to any file
"""
from __future__ import annotations

import os

from src.core.models import BotConfig


def get_private_key() -> str:
    """
    Load the private key from the HL_PRIVATE_KEY environment variable.

    Raises ValueError if the env var is not set.
    The error message deliberately contains no key material.
    """
    key = os.environ.get("HL_PRIVATE_KEY")
    if not key:
        raise ValueError("HL_PRIVATE_KEY env var not set — cannot sign orders")
    return key


def build_signer(private_key: str):
    """
    Build an eth_account.Account object for signing HL L1 actions.

    Parameters
    ----------
    private_key : str
        Raw hex private key (with or without '0x' prefix).
        NEVER logged by this function.

    Returns
    -------
    eth_account.Account
        Account object used by hyperliquid-python-sdk sign_l1_action.
    """
    from eth_account import Account  # lazy import keeps eth_account optional at module load
    return Account.from_key(private_key)


def validate_mainnet_intent(
    config: BotConfig,
    cli_has_mainnet_flag: bool,
) -> None:
    """
    Enforce the four-layer mainnet gate before any network connection.

    Required layers:
      1. bot.mode == "mainnet"
      2. HL_ALLOW_MAINNET=true
      3. --mainnet CLI flag passed
      4. mainnet_confirmed: true in config
    """
    if config.bot.get("mode") != "mainnet":
        raise ValueError(
            "mainnet intent rejected: Layer 1 failed "
            "(config file must set bot.mode: mainnet)."
        )
    if os.environ.get("HL_ALLOW_MAINNET") != "true":
        raise ValueError(
            "mainnet intent rejected: Layer 2 failed "
            "(HL_ALLOW_MAINNET=true env var is required)."
        )
    if not cli_has_mainnet_flag:
        raise ValueError(
            "mainnet intent rejected: Layer 3 failed "
            "(--mainnet CLI flag is required)."
        )
    if config.bot.get("mainnet_confirmed") is not True:
        raise ValueError(
            "mainnet intent rejected: Layer 4 failed "
            "(mainnet_confirmed: true is required in config)."
        )
