# Pyramid Runner Validation Plan

Objective:

```text
RUNNER_LONG -> controlled add probe -> PYRAMID_LONG -> reconciliation clean
```

Use existing scripts. Do not add strategy hacks or bypass lifecycle code.

## Preconditions

```bash
pytest tests/ -q
python scripts/diagnose_hl_account.py --config config/btc_testnet_realistic_probe.yaml
```

Proceed only if:

- network is testnet
- no unexpected open orders
- local state and exchange position are aligned
- no pending order exists

## Create A Runner-Sized Position

Use the Bundle 2 target-runner flow. If the starter order rests, cancel or
reconcile it before continuing.

```bash
python scripts/probe_hl_starter_order.py \
  --config config/btc_testnet_realistic_probe.yaml \
  --coin BTC \
  --qty 0.005 \
  --pricing-mode aggressive \
  --aggressiveness-bps 10 \
  --time-in-force ioc \
  --confirm \
  --one-cycle
```

Then reduce to runner:

```bash
python scripts/probe_hl_target_runner.py \
  --config config/btc_testnet_realistic_probe.yaml \
  --coin BTC \
  --runner-pct 0.5 \
  --pricing-mode aggressive \
  --aggressiveness-bps 10 \
  --time-in-force ioc \
  --confirm \
  --one-cycle
```

Confirm:

- state is `RUNNER_LONG`
- `runner_mode_active=true`
- `target_price_tp_triggered=true`
- exchange qty equals local qty

## Controlled Pyramid Add Probe

```bash
python scripts/probe_hl_add_order.py \
  --config config/btc_testnet_realistic_probe.yaml \
  --coin BTC \
  --qty 0.005 \
  --confirm \
  --one-cycle
```

The script verifies:

- local state is long
- exchange position exists
- exchange qty matches local qty
- no pending order exists
- no open orders exist
- probe qty is above the configured min-size multiplier
- reconciliation does not halt

Expected fill behavior:

- if unfilled: `pending_order` is recorded and no add state mutation occurs
- if filled: `addon_lot` is created through `apply_fill`
- `add_count` increments only after fill
- state becomes `PYRAMID_LONG`
- `RUNNER_LONG` is explicitly eligible for add checks, but only through the same
  risk budget, sizing, and pending-order blockers used by other long states

## Post-Add Validation

```bash
python scripts/diagnose_hl_account.py --config config/btc_testnet_realistic_probe.yaml
cat reports/btc_hl_testnet_realistic_probe/state.json
tail -n 20 reports/btc_hl_testnet_realistic_probe/orders.csv
tail -n 20 reports/btc_hl_testnet_realistic_probe/positions.csv
tail -n 20 reports/btc_hl_testnet_realistic_probe/risk.csv
tail -n 20 reports/btc_hl_testnet_realistic_probe/decisions.csv
```

Confirm:

- add order did not bypass risk or pending-order checks
- `addon_lots` contains the filled add
- base lot remains intact
- exchange qty equals local qty
- no pending order remains after a fill
- Telegram emits `pyramid_level_added`

## Cleanup

Reduce latest add-on first:

```bash
python scripts/probe_hl_reduce_latest_addon.py \
  --config config/btc_testnet_realistic_probe.yaml \
  --coin BTC \
  --confirm \
  --one-cycle
```

Then flatten remaining base/runner only if no pending order exists:

```bash
python scripts/flatten_hl_position.py \
  --config config/btc_testnet_realistic_probe.yaml \
  --coin BTC \
  --confirm \
  --one-cycle
```
