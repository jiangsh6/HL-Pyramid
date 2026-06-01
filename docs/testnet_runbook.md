# Testnet Runbook

All commands below are testnet-only unless explicitly stated otherwise. Do not
start recurring loops unless supervised.

## Required Environment

```bash
export HL_TESTNET_ACCOUNT_ADDRESS=0x...
export HL_TESTNET_AGENT_PRIVATE_KEY=0x...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```

Never print private keys. Use the diagnostic scripts; they mask addresses and do
not print key material.

## 1. Telegram Health Check

```bash
python scripts/test_telegram_notification.py --config config/btc_long_thesis.yaml
```

Expected Telegram event:

- `telegram_health_check`

## 2. Read-Only Account Diagnostic

```bash
python scripts/diagnose_hl_account.py --config config/btc_long_thesis.yaml
```

Confirm:

- `network=testnet`
- account address is masked
- collateral mode is expected
- open orders are understood before any run

## 3. Dry-Run One-Cycle

```bash
python scripts/run_live_hl.py --config config/btc_testnet_dryrun.yaml --dry-run --one-cycle
```

Expected:

- no order is submitted
- state/logs update only through the dry-run path
- Telegram startup/shutdown and decision/safety alerts if enabled

## 4. Testnet One-Cycle Without Dry-Run

Only run after the read-only diagnostic is clean.

```bash
python scripts/run_live_hl.py --config config/btc_long_thesis.yaml --one-cycle
```

Expected:

- one decision cycle only
- no recurring loop
- no duplicate order if a pending/open order exists
- all order state transitions remain fill-confirmed

## 5. Testnet Recurring Loop

Start only when supervised:

```bash
python scripts/run_live_hl.py --config config/btc_long_thesis.yaml
```

Stop with `Ctrl-C`. The script installs a shutdown handler, stops the feed, and
emits `bot_shutdown` if Telegram is enabled.

## 6. Inspect State And Logs

Default testnet run directory:

```bash
ls reports/btc_hl_testnet
cat reports/btc_hl_testnet/state.json
tail -n 20 reports/btc_hl_testnet/orders.csv
tail -n 20 reports/btc_hl_testnet/positions.csv
tail -n 20 reports/btc_hl_testnet/risk.csv
tail -n 20 reports/btc_hl_testnet/decisions.csv
```

## 7. Reset Only When Exchange Is Flat

```bash
python scripts/reset_state.py --config config/btc_long_thesis.yaml --confirm
```

The reset script checks exchange state and refuses unsafe resets.

## 8. Emergency / Operator Recovery Commands

Cancel one exact order:

```bash
python scripts/cancel_hl_order.py \
  --config config/btc_long_thesis.yaml \
  --coin BTC \
  --oid <ORDER_ID> \
  --confirm \
  --one-cycle
```

Flatten an existing position using reduce-only exit:

```bash
python scripts/flatten_hl_position.py \
  --config config/btc_long_thesis.yaml \
  --coin BTC \
  --confirm \
  --one-cycle
```

Clean a dust position only when the explicit dust policy allows it:

```bash
python scripts/cleanup_hl_dust_position.py \
  --config config/btc_long_thesis.yaml \
  --coin BTC \
  --confirm \
  --one-cycle
```

## 9. Expected Testnet Telegram Alerts

- `bot_startup`
- `bot_shutdown`
- `exchange_connectivity_lost` / `exchange_connectivity_restored`
- `reconciliation_failed` / `reconciliation_recovered`
- `position_opened`
- `order` status alerts
- `position_closed`
- `pyramid_level_added` if add-on fills
- `runner_activated` if target runner fills
- `weekly_summary` if enabled
