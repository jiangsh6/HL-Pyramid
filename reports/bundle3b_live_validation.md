# Bundle 3B Live Testnet Validation Report

Date: 2026-06-01

Status: COMPLETE

## Preflight

- Network: testnet.
- Initial exchange BTC qty: 0.0.
- Initial exchange open_orders_count: 0.
- Initial local state: EXITED.
- Initial local qty: 0.0.
- Initial pending_order: null.
- Initial halted: false.
- Initial halt_reason: null.
- Preflight tests: `pytest tests/ -q` passed.

## Hard Stop Validation

Result: PASS

Position creation:

```text
python scripts/probe_hl_starter_order.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --qty 0.005 --pricing-mode aggressive --aggressiveness-bps 10 --time-in-force ioc --confirm --one-cycle
```

- order_status: filled.
- filled_qty: 0.005.
- avg_fill_px: 72388.0.
- final_state: STARTER_LONG.
- post_exchange_qty: 0.005.
- post_open_orders_count: 0.

Hard stop command:

```text
python scripts/probe_hl_hard_stop.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --stop-loss-pct 0.001 --time-in-force ioc --exit-aggressiveness-bps 100 --max-oracle-deviation-bps 100 --confirm --one-cycle
```

- decision_action: sell_stop.
- decision_qty: 0.005.
- decision_new_state: EXITED.
- side: sell.
- reduce_only: true.
- order_status: filled.
- filled_qty: 0.005.
- avg_fill_px: 72262.5.
- post_exchange_qty: 0.0.
- post_open_orders_count: 0.
- final_state: EXITED.
- pending_order: null.
- halted: false.
- halt_reason: null.

Validated:

- SELL_STOP generation.
- Reduce-only stop exit submission.
- Fill-confirmed transition to EXITED.
- Exchange/local reconciliation to flat.
- No unresolved open orders.

Pending exit blocking and duplicate-stop prevention remain unit-tested. They
were not live-observed because the live hard-stop IOC fully filled.

## Emergency Exit Validation

Result: PASS

Full-fill path:

```text
python scripts/probe_hl_starter_order.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --qty 0.005 --pricing-mode aggressive --aggressiveness-bps 10 --time-in-force ioc --confirm --one-cycle
python scripts/flatten_hl_position.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --time-in-force ioc --exit-aggressiveness-bps 100 --max-oracle-deviation-bps 100 --confirm --one-cycle
```

- starter filled_qty: 0.005.
- emergency action: exit_all.
- reduce_only: true.
- time_in_force: ioc.
- exit order_status: filled.
- exit filled_qty: 0.005.
- avg_fill_px: 72259.9.
- post_exchange_qty: 0.0.
- post_open_orders_count: 0.
- final_state: EXITED.
- halted: false.
- halt_reason: null.

IOC no-match path:

```text
python scripts/probe_hl_starter_order.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --qty 0.005 --pricing-mode aggressive --aggressiveness-bps 10 --time-in-force ioc --confirm --one-cycle
python scripts/flatten_hl_position.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --time-in-force ioc --exit-aggressiveness-bps 0 --max-oracle-deviation-bps 0 --force-ioc-no-match-validation --confirm --one-cycle
```

The starter partially filled 0.001 BTC; no additional opening order was sent.
The no-match IOC was run against that 0.001 BTC live position.

- emergency action: exit_all.
- reduce_only: true.
- time_in_force: ioc.
- order_status: rejected.
- exchange reason: Order could not immediately match against any resting orders. asset=3.
- filled_qty: 0.0.
- post_exchange_qty: 0.001.
- post_open_orders_count: 0.
- final_state: HALTED.
- pending_order: null.
- false EXITED transition: no.

Recovery flatten:

```text
python scripts/flatten_hl_position.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --recover-from-ioc-flatten-halt --time-in-force ioc --exit-aggressiveness-bps 100 --max-oracle-deviation-bps 100 --confirm --one-cycle
```

- recovery_guard: allowed.
- order_status: filled.
- filled_qty: 0.001.
- avg_fill_px: 72208.0.
- post_exchange_qty: 0.0.
- post_open_orders_count: 0.
- final_state: EXITED.
- halted: false.
- halt_reason: null.

Partial emergency exit was not live-observed. The partial-fill path remains
covered by unit tests.

## Final State

- Final exchange BTC qty: 0.0.
- Final exchange open_orders_count: 0.
- Final local state: EXITED.
- Final local qty: 0.0.
- Final pending_order: null.
- Final halted: false.
- Final halt_reason: null.

## Tests

Final command:

```text
pytest tests/ -q
```

Result:

```text
539 passed
```

## Bundle Status

- Hard Stop Validation: PASS.
- Emergency Exit Validation: PASS.
- Bundle 3B: COMPLETE.
- Bundle 3 overall: READY.

Bundle 4 was not started.
