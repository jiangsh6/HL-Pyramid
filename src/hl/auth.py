"""
Private key management — Phase 4.

SECURITY INVARIANTS (must never be violated):
  - Agent private keys are NEVER logged
  - Agent private keys are NEVER included in any exception message or traceback
  - Agent private keys are NEVER written to any file
"""
from __future__ import annotations

import os

from src.core.env_loader import ensure_runtime_env_loaded
from src.core.models import BotConfig

DEFAULT_PRIVATE_KEY_ENV = {
    "testnet": "HL_TESTNET_AGENT_PRIVATE_KEY",
    "mainnet": "HL_MAINNET_AGENT_PRIVATE_KEY",
}


def _resolve_key_env_name(
    config: BotConfig | None = None,
    network: str | None = None,
) -> str:
    if network is None and config is not None:
        network = (config.hl or {}).get("network")
    env_name = None
    if config is not None:
        env_name = (config.hl or {}).get("key_env_var")
    if env_name:
        return str(env_name)
    return DEFAULT_PRIVATE_KEY_ENV.get(network or "testnet", DEFAULT_PRIVATE_KEY_ENV["testnet"])


def get_private_key(
    config: BotConfig | None = None,
    network: str | None = None,
) -> str:
    """
    Load the signing key from the configured or network-specific env variable.

    Raises ValueError if the env var is not set.
    The error message deliberately contains no key material.
    """
    ensure_runtime_env_loaded()
    env_name = _resolve_key_env_name(config=config, network=network)
    key = os.environ.get(env_name)
    if not key:
        raise ValueError(f"{env_name} env var not set — cannot sign orders")
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
