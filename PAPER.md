# Paper run

No live keys required. Simulated METAR + L2 books + FAK matcher.

```bash
python3 tests_reversal.py
python3 paper_reversal_sim.py --scenarios-only
python3 paper_reversal_sim.py --seconds 300 --tick 2 --budget 20
```

## Sandbox result (2026-09-01)

Scenarios: 6/6 pass

- one_bucket_fill: NO and YES both filled against L2
- two_bucket_yes_skipped: only NO leg
- stale_obs_no_fire
- morning_skip (11:00 local high)
- cap_abort_no_chase (ask 0.80 > 0.62 cap)
- no_double_fire

5-minute loop: 150 ticks, ARM then FIRE at +1 bucket.

Fills on $20 budget after scramble:

- buy_no_broken: 18.0 shares, cost ~7.68, leftover 4.58 (depth walked away)
- buy_yes_new: 12.50 shares, cost ~4.01, leftover 0
- last intent: abort_timeout (8s budget)

That **is** the fill test: `paper_match_fak` walks ask levels up to cap and mutates size. If you later plug live CLOB snapshots into the same function, you get realistic partial fills without sending live orders.

## How to test fills later against real books

1. Dump a Polymarket book JSON for the two tokens at trigger time.
2. `books_by_token[token] = dumped`
3. Call `plan_fire_cycle` + `paper_match_fak` with `mode=paper`.
4. Compare filled vs leftover. If leftover is always huge, cap is too tight or depth is gone — you were late.
