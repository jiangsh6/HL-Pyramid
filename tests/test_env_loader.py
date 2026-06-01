from __future__ import annotations

import os

from src.core.env_loader import load_env_file


def test_load_env_file_parses_network_specific_hl_keys(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HL_TESTNET_AGENT_PRIVATE_KEY=0xabc123  # inline comment\n"
        "HL_TESTNET_ACCOUNT_ADDRESS=0x1111111111111111111111111111111111111111\n"
    )
    monkeypatch.delenv("HL_TESTNET_AGENT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)

    load_env_file(str(env_path))

    assert os.environ["HL_TESTNET_AGENT_PRIVATE_KEY"] == "0xabc123"
    assert (
        os.environ["HL_TESTNET_ACCOUNT_ADDRESS"]
        == "0x1111111111111111111111111111111111111111"
    )


def test_load_env_file_does_not_override_existing_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("HL_TESTNET_AGENT_PRIVATE_KEY=0xfromfile\n")
    monkeypatch.setenv("HL_TESTNET_AGENT_PRIVATE_KEY", "0xexisting")

    load_env_file(str(env_path), override=False)

    assert os.environ["HL_TESTNET_AGENT_PRIVATE_KEY"] == "0xexisting"
