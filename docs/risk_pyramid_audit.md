# Risk Budget and Pyramid Lifecycle Audit

## Risk Budget Findings

Runtime summary values are produced by `src/reporting/daily_summary.py::format_summary`:

```text
Config
  capital.starting_equity
  capital.max_total_capital_at_risk_pct
  state.base_lot / state.addon_lots
  state.trailing_stop_price
    -> src/reporting.daily_summary.format_summary
      max_risk = starting_equity * max_total_capital_at_risk_pct
      open_risk = sum(max(0, lot.entry_price - trailing_stop_price) * lot.qty)
      remaining_budget = max(0, max_risk - open_risk)
        -> Daily summary risk budget lines
```

Add sizing enforces the same budget before a new pyramid add:

```text
Config + state + current price
  -> src/core/decision_engine.py::run
    -> src/strategy/add.py::check_add_conditions
    -> src/strategy/sizing.py::calc_addon_size
      max_risk_budget = starting_equity * max_total_capital_at_risk_pct
      current_open_risk = sum((lot.entry_price - trailing_stop_price) * lot.qty)
      remaining = max(0, max_risk_budget - current_open_risk)
      remaining <= 0 => blocker thesis_risk_budget_exhausted
```

`Open Risk > Max Thesis Risk` is a warning condition, not by itself proof that a new order bypassed risk. It can occur after state reconstruction, manual probe sizing, price/stop movement, config changes, or a trailing stop that leaves existing exposure above the configured budget. The current behavior is conservative for new adds: remaining budget is clamped to zero and add-on sizing returns `thesis_risk_budget_exhausted`.

No trading logic change was made in this audit. Coverage was added to lock the observed behavior.

## Pyramid Lifecycle Findings

| Event | Trigger Condition | File | Function |
|---|---|---|---|
| Runner activation decision | Target-price TP logic computes `runner_target_qty` from `original_base_qty` and returns `SELL_TAKE_PROFIT` with `new_state=RUNNER_LONG` | `src/strategy/take_profit.py` | `check_take_profit` |
| Runner activation applied | Fill-confirmed sell leaves qty at or below runner target, then intended runner side effects are applied | `scripts/run_live_hl.py` | `run_decision_cycle` |
| Target-runner probe activation | Controlled probe applies fill and enters runner only if filled qty brings position to runner target | `scripts/probe_hl_target_runner.py` | `apply_target_runner_order_result` |
| Pyramid add decision | State is `BASE_LONG`/`PYRAMID_LONG`, add conditions pass, no protect-profit/daily-loss block, and add sizing succeeds | `src/core/decision_engine.py` | `run` |
| Pyramid add blockers | Max add count, cooldowns, price step, MA checks, event window, exposure cap, or protect-profit mode | `src/strategy/add.py` | `check_add_conditions` |
| Pyramid add risk budget | Remaining risk budget is computed before returning add qty | `src/strategy/sizing.py` | `calc_addon_size` |
| Pyramid add state mutation | Confirmed buy add fill appends `addon_lot`, increments `add_count`, updates `last_add_price` | `src/reporting/state_writer.py` | `apply_fill` |
| Max pyramid reached | `state.add_count >= add.max_add_count`; `check_add_conditions` returns `max_add_count_reached`. In the full decision engine, `_evaluate_protect_profit_mode` may also set `protect_profit_mode` at max add count before add evaluation, which suppresses further add attempts conservatively. | `src/strategy/add.py`, `src/core/decision_engine.py` | `check_add_conditions`, `_evaluate_protect_profit_mode` |
| Max pyramid alert | `NO_ACTION` decision contains `max_add_count_reached` | `scripts/run_live_hl.py` | `run_decision_cycle` |
| Reconciliation after add-ons | Exchange/local qty, pending order, and fill evidence are reconciled conservatively before decisions continue | `src/execution/reconciler.py` | `reconcile_state_with_exchange` |

## Safety Findings

- Add-ons cannot bypass max add count because `check_add_conditions` emits `max_add_count_reached` before sizing.
- Add-ons cannot bypass remaining risk budget because `decision_engine.run` calls `calc_addon_size` after add conditions pass and returns no action if sizing returns a blocker.
- Pending exit/reduce orders block normal strategy through `decision_engine._pending_order_is_exit_resolution_required`.
- Runtime state mutation remains fill-confirmed through `OrderResult` handling and `state_writer.apply_fill`.
- Reconciliation remains conservative and halts on ambiguous exchange/local mismatch.

## Coverage Gaps

Existing coverage already included:

- Add blocking when remaining risk budget is exhausted.
- Max add count blocking.
- Runner target based on `original_base_qty`.
- Fill-confirmed runner transition.
- Reconciliation and pending-order handling around add/reduce flows.

This audit added focused tests for:

- Summary reporting when open risk exceeds max risk.
- Add sizing blocker when open risk exceeds risk budget.
- Decision-engine add blocker when open risk exceeds risk budget.
- Decision-engine max add count blocker.

## Recommended Fixes

No code fix is required from this audit.

Recommended future hardening, separate from this audit:

- Consider adding an explicit runtime warning/alert when `open_risk > max_risk` so operators see over-budget exposure as a risk condition, even though adds are already blocked.
- Consider factoring the duplicated open-risk calculation into a shared helper so daily summary and sizing cannot drift.
