# Mainnet Tiny Deployment Runbook

This is for a supervised tiny first run only. Do not use it until testnet has
completed cleanly and Telegram alerts have been verified.

## Required Environment

```bash
export HL_ALLOW_MAINNET=true
export HL_MAINNET_ACCOUNT_ADDRESS=0x...
export HL_MAINNET_AGENT_PRIVATE_KEY=0x...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```

Security rules:

- never print private keys
- never paste `.env` into logs or chat
- never run mainnet without the `--mainnet` flag

## Config To Use

```text
config/btc_long_thesis_mainnet.yaml
```

Safety defaults:

- `bot.mode: mainnet`
- `mainnet_confirmed: true`
- `hl.network: mainnet`
- `capital.starting_equity: 100`
- `capital.max_total_capital_at_risk_pct: 0.03`
- `entry.starter.exposure_pct: 0.04`
- leverage disabled
- Telegram disabled by default until explicitly configured

## Capital Ladder

Recommended progression:

```text
$100 -> $250 -> $1,000
```

Do not increase size until:

- testnet lifecycle remains clean
- Telegram alerts are reliable
- cancellation/flatten paths are verified
- no unresolved pending order exists

## First Read-Only Checks

```bash
python scripts/test_telegram_notification.py --config config/btc_long_thesis_mainnet.yaml
```

The health check does not place orders.

There is no mainnet diagnostic command in this runbook because a diagnostic hits
mainnet endpoints. Run it only under explicit operator approval.

## First Mainnet Dry-Run One-Cycle

```bash
python scripts/run_live_hl.py \
  --config config/btc_long_thesis_mainnet.yaml \
  --mainnet \
  --dry-run \
  --one-cycle
```

Expected:

- mainnet gates pass
- no order is submitted
- Telegram startup/shutdown/decision alerts if enabled

## First Tiny Mainnet One-Cycle

Only after the dry run is reviewed:

```bash
python scripts/run_live_hl.py \
  --config config/btc_long_thesis_mainnet.yaml \
  --mainnet \
  --one-cycle
```

This may place a live order if the strategy produces a valid action.

## Mainnet Recurring Loop

Start only after multiple one-cycle checks:

```bash
python scripts/run_live_hl.py \
  --config config/btc_long_thesis_mainnet.yaml \
  --mainnet
```

Stop with `Ctrl-C`. Verify `bot_shutdown` arrives if Telegram is enabled.

## Emergency Commands

Cancel exactly one order:

```bash
python scripts/cancel_hl_order.py \
  --config config/btc_long_thesis_mainnet.yaml \
  --coin BTC \
  --oid <ORDER_ID> \
  --mainnet \
  --confirm \
  --one-cycle
```

Mainnet cancel is allowed only as an emergency recovery command when all of
these are true: the config is mainnet, `HL_ALLOW_MAINNET=true`, the operator
passes `--mainnet`, `--confirm`, and `--one-cycle`, and an exact `oid` is
provided. The script cancels only that OID.

Flatten position:

```bash
python scripts/flatten_hl_position.py \
  --config config/btc_long_thesis_mainnet.yaml \
  --coin BTC \
  --mainnet \
  --confirm \
  --one-cycle
```

Mainnet flatten is allowed only as an emergency recovery command when all of
these are true: the config is mainnet, `HL_ALLOW_MAINNET=true`, the operator
passes `--mainnet`, `--confirm`, and `--one-cycle`, no exchange orders are
open, and BTC quantity is aligned. The script submits reduce-only only and uses
conservative cleanup pricing.

## Required Telegram Alerts

Before live mainnet:

- `telegram_health_check`
- `bot_startup`
- `bot_shutdown`

During live mainnet:

- `exchange_connectivity_lost`
- `exchange_connectivity_restored`
- `reconciliation_failed`
- `position_opened`
- `order` status
- `position_closed`
- `weekly_summary` if enabled

## Stop Mainnet Immediately If

- any order is open but not represented in local `pending_order`
- local qty and exchange qty differ
- Telegram alerts stop arriving
- `reconciliation_failed` appears
- state becomes `HALTED`
- a reduce-only exit rests unexpectedly
- Hyperliquid UI disagrees with local state
