# Bundle 2 Final Validation Report

Date: 2026-06-01

Status: PASS_WITH_TESTNET_LIQUIDITY_EXCEPTION

## Summary

Bundle 2 target-runner logic is code-complete and safety-validated. The live
RUNNER_LONG transition after target-runner reduce fill was not fully validated
because Hyperliquid testnet did not provide immediately matchable liquidity for
the reduce-only IOC SELL.

This is not being claimed as a full live RUNNER_LONG validation.

## Validated

- Starter IOC filled 0.005 BTC using live mark/oracle pricing.
- STARTER_LONG transition worked.
- original_base_qty persisted as 0.005.
- Target-runner reduce quantity calculated correctly as 0.0025 BTC.
- Target-runner reduce-only SELL was submitted safely with IOC.
- IOC unfilled/rejected did not transition to RUNNER_LONG.
- target_price_tp_triggered remained false without a confirmed fill.
- runner_mode_active remained false without a confirmed fill.
- No stale pending IOC order was left behind.
- No unresolved open orders remained.
- HALTED recovery guard allowed only the known validation recovery state.
- Exchange/local state remained safe after rejected IOC attempts.
- Test suite passed: 516 tests before final report; 516+ after follow-up tests.

## Not Validated

- Actual live RUNNER_LONG transition after a filled target-runner reduce.

The RUNNER_LONG transition remains unfilled live due to testnet liquidity.

## Testnet Liquidity Exception

Hyperliquid testnet returned:

```text
Order could not immediately match against any resting orders. asset=3
```

The target-runner IOC behavior was safe under this condition. The probe did not
promote local state to RUNNER_LONG, did not set target_price_tp_triggered, did
not activate runner_mode_active, and did not leave a pending IOC order.

## Safety Invariants

- No false runner transition: validated.
- No premature target_price_tp_triggered: validated.
- No stale pending IOC order: validated.
- No unresolved open orders: validated.
- Exchange/local state remained safe: validated.

## Final Bundle 2 State Before Cleanup

- Exchange BTC qty: 0.005.
- Local state: HALTED.
- halt_reason: exit_order_canceled_position_still_open.
- open_orders_count: 0.
- pending_order: null.

## Bundle 3 Gate

Bundle 3 must not start until the remaining 0.005 BTC testnet exposure is
cleared, exchange/local state is reconciled, tests pass, and the working tree is
clean except intentionally deferred files.

## Exposure Cleanup

The remaining 0.005 BTC testnet exposure was cleared before Bundle 3.

Cleanup command:

```text
python scripts/flatten_hl_position.py --config config/btc_testnet_realistic_probe.yaml --coin BTC --recover-from-target-runner-validation-halt --time-in-force ioc --exit-aggressiveness-bps 100 --max-oracle-deviation-bps 100 --confirm --one-cycle
```

Cleanup result:

- order_status: filled.
- reduce_only: true.
- time_in_force: ioc.
- filled_qty: 0.005 BTC.
- avg_fill_px: 72219.0.
- post_exchange_qty: 0.0.
- post_open_orders_count: 0.
- final local state: EXITED.
- pending_order: null.

Bundle 3 remains gated on tests passing after this report update.
