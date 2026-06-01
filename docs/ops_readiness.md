# HL Primiding Ops Readiness

This document is intentionally operational and conservative. It does not enable
orders by itself.

## Repository Preflight

```bash
git status --short
pytest tests/ -q
git status
git log --oneline -5
```

Expected:

- working tree clean
- full pytest suite passing
- no unreviewed local changes

## Config Summary

| Config | Network | Intended Use |
|---|---|---|
| `config/btc_testnet_dryrun.yaml` | testnet | tiny dry-run validation |
| `config/btc_long_thesis.yaml` | testnet | full testnet strategy loop |
| `config/btc_testnet_realistic_probe.yaml` | testnet | deterministic probe sizing |
| `config/btc_long_thesis_mainnet.yaml` | mainnet | tiny supervised first run only |

Do not run mainnet unless the testnet runbook is complete, Telegram alerts work,
and the operator explicitly approves the mainnet command.

Mainnet emergency recovery scripts are intentionally gated. `cancel_hl_order.py`
and `flatten_hl_position.py` require a mainnet config, `HL_ALLOW_MAINNET=true`,
`--mainnet`, `--confirm`, and `--one-cycle`; cancel also requires an exact OID,
and flatten refuses if exchange orders are open or BTC quantity is not aligned.
