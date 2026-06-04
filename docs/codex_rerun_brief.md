# HYPE Pyramiding Backtest — v1.1 Run Rejected, Three Bugs To Fix

The v1.1 run at `reports/research/hype/pyramiding_backtest_20260604T032442Z`
produced a clean-looking `IMPLEMENT_STARTER_ONLY` recommendation. Independent
recomputation against the raw run outputs shows the recommendation rests on
three execution bugs. None of the phase-by-phase conclusions can be trusted
until these are fixed and the full pipeline is rerun.

This document specifies the bugs, the evidence, the required fix, and the
acceptance criteria. It does not specify implementation details — locate the
code paths yourself and report what you changed.

A diagnostic script is provided: `scripts/diagnose_hype_backtest.py`. Run it
before AND after your fixes. Before-fix output must show `CONFIRMED` for the
listed bugs. After-fix output must show `REFUTED` for all three.

---

## Bug 1 — Funding attribution is inconsistent across phases

### Evidence from the run outputs

```
config_id                           phase   trades   funding_pnl
p1_1d_local_stop1.5                 1       9        -208.47
p2_1d_local_a10.5                   2       9        -93.62
p3_1d_local_0.5-0.3-0.2_a22.0       3       11       -119.96
p5_1d_local_cost                    5       23       -45.57
```

All configurations trade the same asset (HYPE) over the same date range. The
total time-in-market across the run is bounded by the underlying price history;
it does not change because the strategy adds adds or partial TPs.

If funding were correctly attributed as `Sigma(notional_at_funding_timestamp ×
funding_rate_at_timestamp)` over the actual hours each lot is open, the
numbers above are not self-consistent:

- Phase 5 has 2.5x the trades of Phase 1 but only ~22% of the funding cost.
- Phase 2 has the same trade count as Phase 1 but 45% of the funding cost,
  even though Phase 2 adds notional via Add1 (the `add1_hit_rate` says it did
  not, which is the subject of Bug 2 — but the funding number should still be
  no smaller than starter-only).
- Phase 1 — the simplest strategy with no adds, no partial TP, no runner —
  paying the LARGEST funding cost is structurally backwards.

### Required behaviour

For every trade, funding PnL must be the exact sum over all hourly funding
timestamps t between the trade's first fill and its final fill of:

```
funding_t = -1 * (sum of open lot notionals at t) * funding_rate_t
```

For longs: positive funding_rate increases cost (negative PnL).

Notional at funding timestamp t must reflect all open lots at t, including
adds that have already filled and excluding portions already closed by
partial TP. Use the actual notional at t, not entry-time notional, and not
trade-average notional.

### Acceptance

`scripts/diagnose_hype_backtest.py` independently recomputes funding for the
Phase 1 best config from `trade_log_best.csv` + `HYPE_funding.csv`. After the
fix:

```
|recomputed - logged| / |logged| < 0.05
```

Also: across the full ranked configs, funding cost must be monotonically
non-decreasing in `Sigma(notional × hours_held)` across configs. If two configs
have similar exposure-hours but very different funding, that is a regression.

---

## Bug 2 — Add1 never fires across any Phase 2 configuration

### Evidence from the run outputs

```
config_id              add1_hit_rate    net_return_pct    trade_count
p2_1d_local_a10.5      0.0              7.854339          9
p2_1d_local_a11.0      0.0              7.854339          9
p2_1d_local_a11.5      0.0              7.854339          9
p2_1d_local_a12.0      0.0              7.854339          9
```

Four different Add1 trigger thresholds (0.5, 1.0, 1.5, 2.0 ATR above starter
entry) produce identical net returns, identical trade counts, and zero add
hits. This is not the data telling us "adds add no value." This is "the
add-trigger code never evaluated true."

The diagnostic script replays the 9 Phase 1 trades against daily HYPE bars
and counts how many bars satisfied each Add1 condition independently. If the
script reports any non-zero count for any trigger while the Phase 2 CSV
records `add1_hit_rate = 0.0`, the add logic is broken.

### Likely causes

In rough order of probability:

1. ATR used for the trigger threshold is the live ATR at evaluation time, not
   the ATR captured at the starter fill. The plan explicitly requires
   `ATR14_at_starter`. If you are using a moving ATR, on a vertical move ATR
   expands and the threshold runs away from price.
2. State machine guard blocks the `STARTER_LONG -> ADD1_LONG` transition
   because of an unrelated condition (e.g. `trade unrealized PnL > 0` check
   evaluated against stale state).
3. Trigger comparison uses signal-bar close but fill happens at next bar open
   — and the next bar open is below the trigger, so the order is rejected
   silently instead of filled at next open.

Investigate which of these is the actual cause and report it.

### Required behaviour

`add1_hit_rate` for at least one Phase 2 trigger must be greater than zero on
this dataset, and the number of fired adds must match what the diagnostic
script counts (within 1 fill of tolerance, to account for the same-bar
ordering rule between stop and add).

### Acceptance

The diagnostic script's BUG 2 verdict must be `REFUTED` after the fix.
Additionally: at least one Phase 2 config must show `add1_hit_rate > 0`. The
Phase 2 conclusions (`add1 helps / does not help`) are only valid after this.

---

## Bug 3 — Per-window single-trade dominance bypasses Revision 4

### Evidence from the run outputs

`backtest_report.md` reports `positive_only_in_w3 = False`, satisfying the
Revision 4 condition for not downgrading the recommendation. The sub-window
returns are:

```
W1 net: +9.26%    W2 net: -2.13%    W3 net: +3.41%
```

W1 is positive, so the flag does not trigger. But examining
`trade_log_best.csv`, all of W1's net PnL comes from one trade:

```
trade_id  entry_time   exit_time    bars_held    gross_pnl    exit_reason
2         2025-04-29   2025-09-26   151          +1067.82     MA100_BREAK
```

This is the same failure mode the full-period `SINGLE_TRADE_DOMINATED` check
was designed to catch. The check is in place at the full-period level but
not replicated per-window, so Revision 4's "positive in multiple windows"
test passes on a technicality.

### Required behaviour

Replicate the `top_trade_contribution_pct > 0.60` check inside each sub-window
W1/W2/W3 independently. A window only counts as "genuinely positive" if:

```
window_net_return > 0
AND window_top_trade_contribution <= 0.60
```

`top_trade_contribution` per window is computed as
`max(net_pnl_in_window) / sum(positive net_pnl in window)`, mirroring the
full-period definition.

The `positive_only_in_w3` downgrade should then trigger if at most one
sub-window passes BOTH conditions. The Revision 4 spec stays unchanged in
intent; this fix matches the implementation to the intent.

### Acceptance

The diagnostic script's BUG 3 verdict must be `REFUTED` after the fix.
`backtest_report.md` must include per-window `top_trade_contribution_pct`
values in the sub-window section, not just `w*_net_return_pct`.

The recommendation field must be re-derived from the corrected
`positive_only_in_w3` and may legitimately move to `NEEDS_MORE_DATA` if the
fix flips the flag. That is an acceptable outcome — it is the honest one.

---

## Order of work

1. Implement Bug 1 fix (funding attribution). Verify by recomputing one
   trade's funding by hand against the funding CSV; cross-check with the
   diagnostic script.
2. Implement Bug 2 fix (Add1 trigger). Add a debug log line at every
   evaluated add candidate showing `bar_time, close, trigger_threshold,
   atr_at_starter, condition_met` for the best config. Run once, eyeball.
3. Implement Bug 3 fix (per-window dominance). Trivially small change in
   the report generator.
4. Rerun the full pipeline:
   ```
   python scripts/run_hype_pyramiding_backtest.py --symbol HYPE --all-phases
   ```
5. Run `pytest tests/ -q`. All existing tests must still pass.
6. Run `scripts/diagnose_hype_backtest.py` against the new run dir. All three
   verdicts must be `REFUTED`.
7. Update `backtest_report.md`. Re-derive the recommendation honestly. Do
   not pre-commit to `IMPLEMENT_STARTER_ONLY` — the corrected numbers may
   support a different choice within the allowed v1.1 set.

---

## What success looks like

After the rerun, the report should let us answer three questions cleanly:

- Does the MA100 regime filter have edge in more than one independent trend?
- Does Add1 add value, subtract value, or no value, when it actually fires?
- After correctly-attributed funding, what survives?

Right now we cannot answer any of these because the underlying execution
disagrees with what the result fields claim. That is the only thing the
rerun needs to fix. The recommendation will follow from honest numbers.
