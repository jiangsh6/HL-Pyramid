"""
Private key management — Phase 4.

SECURITY INVARIANTS (must never be violated):
  - HL_PRIVATE_KEY is NEVER logged
  - HL_PRIVATE_KEY is NEVER included in any exception message or traceback
  - HL_PRIVATE_KEY is NEVER written to any file
"""
from __future__ import annotations

import os


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
