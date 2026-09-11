# Repository Guidelines

## Project Overview

`weatherbotyes2re` — the **canonical reversal-paper** stack of the broader `poly-yes2` effort. A **pure-Python 3.13, stdlib-only** paper-trading engine for Polymarket daily-weather markets.

It watches per-city "highest/lowest temperature" Polymarket buckets, merges real aviation weather (CheckWX + AviationWeather METAR/TAF), and detects when an observed running daily high/low breaks the reference extreme by **exactly one bucket**. It then *paper*-fills (simulated, never live) a capped FAK buy on that reversal.

**Safety stance (hard-enforced):** paper only. `No σ. No fade-NO / BUY-YES grid. No wallet. Paper only.` `reversal_runner.py` refuses any mode other than `paper`.

> ⚠ **Source-integrity caveat.** The committed live-run entry chain is **currently non-runnable**: `runner_impl.py` imports `_r_state`, `r_cycle`, and `_r_exec` modules that do **not** exist in the tree (only `_r_globals.py` is present). These modules are named in `CHANGELOG.md` as intended. The `reversal_runner.py` / `runner_impl.py` path fails on import today. The fully self-contained, runnable-and-testable artifacts are the pure strategy + simulation layer (`paper_reversal_sim.py` → `tests_reversal.py`). A stray `runner_impl.b64.0` is a base64 snapshot of an older single-file runner (historical artifact). Re-homing the live runner therefore requires restoring `_r_state/_r_cycle/_r_exec`.

## Architecture & Data Flow

Layered, function-heavy, transport-agnostic design. Data path is a **blocking polling loop** (`time.sleep`), not request/event-driven, with one concurrency spot (`concurrent.futures.ThreadPoolExecutor`, see below).

1. **Weather & rules ingestion**
   - `research/common.py` — dual-source METAR (`dual_source_metar`, fresher obs wins), TAF TX/TN (`checkwx_taf`, `parse_tx_tn`), `parse_metar_temp_c`, `parse_obs_time_utc`, `c_to_market_unit` (°C→city market unit), `load_env`, `load_cities`.
   - `market_adapter.py` — public **Gamma REST** adapter: regex-parses event slugs/questions/outcome ranges into bucket rules (`parse_event_rules`, maps yes/no `clobTokenIds`); `refresh_market_rules` runs parallel Gamma fetches via `ThreadPoolExecutor(max_workers=10)` under a 180s deadline.
2. **Market data (read-only)**
   - `clob_market_data.py` — CLOB REST: batched `/books` (chunk 100) + single `/book` fallback → frozen `BookSnapshot`; status classifier `executable_summary` (`STALE`/`EMPTY`/`ASK_OUTSIDE_LIMIT`/`EXECUTABLE`); `fetch_fee_rate` (30s cache).
   - `websocket_market_data.py` — public market WS (`MarketStream` → per-token `LocalOrderBook`; `seed_from_rest`). Optional; paper path can be REST-only.
   - `local_order_book.py` — deterministic in-memory L2 book (`LocalOrderBook`: `apply_book`, `apply_price_change`, `snapshot`, `is_fresh`; injectable `clock`).
   - **Normalization seam:** `adapters/polymarket/orderbook.py.from_any/from_book_snapshot/from_local_snapshot` converts every transport shape into one `execution/market.BookView`. Execution logic never branches on transport.
3. **Strategy (pure decision logic)**
   - `reversal_strategy.py` — state machine **IDLE → ARMED → FIRED → COOLDOWN**. Main hook: `maybe_arm_or_fire(state, city, market_local_date, direction, buckets, taf_extreme, observed_temp, obs_time_utc, now_utc, books_by_token, config, consensus_tracker) -> list[action dicts]`. Emits data: `re_arm` / `re_disarm` / `re_fire` / `re_skip(reason)` / `re_skip_yes`. **Never raises** — returns machine-readable `reason` codes (`duplicate_obs_time`, `stale_obs`, `consensus_filter`, `bucket_unmapped`, `no_reference_extreme`, `already_fired`).
   - `consensus_tracker.py` — long-horizon anti-whipsaw filter: rolling time-weighted mid (TWAP) per-bucket rank; gate fires to buckets that were rank-1 for 1–2h. `ConsensusTracker` dataclasses `PriceSample`/`BucketSeries` (deque maxlen 7200); keyed `city|date|direction -> bucket_id`; module singleton `DEFAULT_TRACKER`.
4. **Execution (pure, in-memory)**
   - `re_execution.py` — capped FAK ladder (`LADDER_MS=(0,1500,4000)`, `FIRE_BUDGET_MS=8000`); `plan_leg_attempts`, `plan_fire_cycle`, `size_legs`, `paper_match_fak` (walks ask levels mutating the book), `cap_price`/`best_ask`/`tick_of`.
   - `paper_capital.py` — shared paper cash ledger, pure over the state dict (`reserve` returns `None` on insufficient capital → caller must **fail closed**).
5. **State & run loop**
   - Single JSON blob persisted to `data/yes2re_state.json` each cycle; schema guarded by `STATE_VERSION`. Keys: `positions`, `weatherbotyes2re:{armed,fired,running_extremes,taf_forecasts,last_obs,last_obs_time}`, `paper_initial_capital_usdc`.
   - Cycle writes `data/yes2re_health.json` and appends JSONL to `data/yes2re_events.jsonl`. Per-cycle exceptions are caught, logged as `{"type":"cycle_error",...}`, and the loop continues — never crashes.
   - Per-fire dedup key: `city|date|direction`. Fresh-obs guard: identical `obs_time` not processed twice.

**Fire conditions** (mirrored in strategy docstring): (1) running extreme breaks reference by exactly one bucket (reference = TAF TX/TN, else 1–2h rank-1 YES TWAP mid); (2) fresh obs (age ≤ 180s); (3) local hour in window (high ≥ 14, low ≤ 10); (4) broken bucket was long-horizon rank-1 consensus. Legs: BUY NO broken bucket (cap `0.65`, 75% notional) + optional BUY YES new bucket (cap `0.48`, 25%). Jump ≥ 2 buckets → NO-only.

## Key Directories

| Path | Purpose |
|------|---------|
| repo root | All top-level strategy/execution modules + entry points |
| `research/` | Weather/TAF source helpers (`common.py`), stdlib only |
| `execution/` | Normalized contracts (`market.py`) and exec engine seam |
| `adapters/polymarket/` | Transport→`BookView` normalization (`orderbook.py`) |
| `config/` | Runtime tuning (`yes2re_reversal.json`) + static city table (`contract_cities.json`, ~592 lines) |
| `data/` | Runtime output — state/log/health (gitignored, not in tree) |

## Development Commands

No package manager, no build tooling, no Makefile, no venv — **stdlib-only**, plain `python3` with `#!/usr/bin/env python3` entry points. Run from the repo root (`weatherbotyes2re/`):

```bash
# Scenario "test" runner (fully wired, dependency-free) — exit 1 on failure
python3 tests_reversal.py

# Fire-simulation variant (also runs a live synthetic loop by default)
python3 paper_reversal_sim.py --scenarios-only

# Paper runner — CLI subcommands: once | run | status
# CAVEAT: currently unimportable — needs _r_state/_r_cycle/_r_exec restored
python3 reversal_runner.py once  --config config/yes2re_reversal.json
python3 reversal_runner.py run   --config config/yes2re_reversal.json   # loops, cycles until Ctrl-C or --max-seconds
python3 reversal_runner.py status --config config/yes2re_reversal.json

# Optional: weather API key (AWC works without it)
export CHECKWX_API_KEY=...
```

No lint, formatter, or CI script exists. Python 3.13 required (`.pyc` artifacts are `cpython-313`).

## Code Conventions & Common Patterns

- **stdlib only.** Module docstrings repeat this constraint. No `requirements.txt`/`pyproject`. Use `urllib`, `json`, `decimal`, `concurrent.futures`, `argparse`, `dataclasses`, `datetime` — not third-party libs.
- **Money = `decimal.Decimal`** everywhere in logic (module constants like `NO_MAX_ASK = "0.65"` are strings, treated as Decimal). Floats only when serializing state to JSON.
- **Pure, function-heavy core; actions as data.** Strategy never raises or mutates the caller's live state + decides solely from its inputs; it returns a list of action **dicts** with machine-readable `reason` codes. Callers act on the data, not exceptions.
- **Custom exception subclasses** for hard infra errors: `CLOBDataError`, `MarketStreamError`, `OrderBookStateError` (`OrderBookStateError(ValueError)`, others `(RuntimeError)`).
- **Fail closed.** `paper_capital.reserve` → `None` means *don't* mutate; FAK ladder aborts above-cap / on timeout; stale/empty books yield status codes, not prices.
- **State machine = one dict**, per `session_key = city|date|direction` (`armed`/`fired`/`running_extremes`). One-fire-per-session and duplicate-`obs_time` dedup are invariants.
- **Config passing, not a framework.** `config` (a parsed JSON dict) and an explicit `ConsensusTracker` instance (default `DEFAULT_TRACKER`) are threaded into pure functions. Constructor injection of `clock`/`timeout_seconds` used in adapters for testability.
- **Named private-module prefix** for the runner internals: `_r_globals.py` (`_r_state`/`_r_cycle`/`_r_exec` intended). `_r_globals` holds only process-local caches (tracker singleton, METAR/book/TAF & rules stamps).
- **Dual-rate polling:** idle cadence (~60s METAR / ~30s books, `scan_interval_seconds` 20) vs **ARM fast-poll** of only the armed ICAOs (~10s). During ARM the full universe still samples slowly to feed consensus.

## Important Files

| File | Role |
|------|------|
| `reversal_runner.py` | Thin `paper` entry point (`#!/usr/bin/env python3`) → `runner_impl.main`; blocks non-paper modes |
| `runner_impl.py` | CLI `once`/`run`/`status`; cycle loop, state save, per-cycle error logging. **Import-broken** (missing `_r_*` modules) |
| `paper_reversal_sim.py` | Standalone simulator + integration harness (7 `scenario_*` fns, `make_city`/`make_buckets`/`make_book`, `live_loop`) |
| `tests_reversal.py` | Lightweight, pytest-free test runner → `paper_reversal_sim.run_scenarios()` |
| `reversal_strategy.py` | Strategy state machine — the per-new-METAR hook `maybe_arm_or_fire` + constants (`NO_MAX_ASK 0.65`, `YES_MAX_ASK 0.48`, `REQUIRE_FRESH_OBS_SECONDS=180`, `HIGH_FIRE_LOCAL_HOUR=14`, …) |
| `consensus_tracker.py` | `ConsensusTracker`, `DEFAULT_TRACKER` long-horizon rank filter |
| `re_execution.py` | Capped FAK planner + `paper_match_fak` L2 matcher |
| `paper_capital.py` | Paper ledger over state dict |
| `clob_market_data.py` / `websocket_market_data.py` / `local_order_book.py` | Read-only CLOB REST / WS + deterministic L2 book |
| `market_adapter.py` | Gamma rules discovery (public metadata only) |
| `execution/market.py` | Normalized `BookView` contract (`OrderIntent → RiskGate → ExecutionEngine(Paper\|Live) → Fill → Position`) |
| `adapters/polymarket/orderbook.py` | The single transport-normalization seam (`from_any`) |
| `config/yes2re_reversal.json` | Runtime config (mode/intervals/caps/consensus/fire params) + paths |
| `reversal_runner.b64.0` (`runner_impl.b64.0`) | Historical base64 runner snapshot |

## Runtime/Tooling Preferences

- **Runtime:** Python 3.13, no virtualenv, no dependency install.
- **Package manager:** none.
- **Environment/secrets:** optional `CHECKWX_API_KEY`, read from env or a `.env` file (loaded by `research/common.load_env`; AviationWeather path is keyless). `.env.example` documents it — never commit real keys; `.env` is gitignored.
- **Config:** JSON in `config/`. Runtime tuning lives in the `strategy` block of `yes2re_reversal.json` (caps, notional splits, consensus window/samples/lead, fire hour windows) — module-level constants in `reversal_strategy.py` are the defaults that `config['strategy']` overrides.
- **Docs language:** README/PAPER/CHANGELOG are English; some `.env.example` comments are Chinese. Match what you are editing.
- **Network:** WSL needs `networkingMode=mirrored`; deploy to a low-latency near-CLOB VPS (US East) preferred; keep token ids + books warm before FIRE (never discover tokens on the fire path).

## Testing & QA

- **No pytest / unittest / jest** and **no conventional test suite.** The entire automated test surface is one dependency-free scenario runner:
  - `python3 tests_reversal.py` → calls `paper_reversal_sim.run_scenarios()`; prints `PASS`/`FAIL` per scenario, `SystemExit(nonzero-fail-count)` on failure.
  - `python3 paper_reversal_sim.py --scenarios-only` — same checks.
- **Test style:** pure-logic, fully synthetic and in-memory — fake books, buckets, consensus histories, timestamps via factory helpers (`make_city`, `make_buckets`, `make_book`, `seed_consensus_rank1`, `apply_scramble`), plus `run_fire_window`. No network, DB, or mock library. Transport/clock are constructor-injected to make this possible.
- **The 7 scenarios** (from `paper_reversal_sim.py`) exercise `maybe_arm_or_fire` + `plan_fire_cycle`/`paper_match_fak` end-to-end against real branch rules:
  1. `one_bucket_fill` — exact-one-bucket breach fires
  2. `two_bucket_yes_skipped` — ≥2-bucket jump → YES leg skipped (NO-only)
  3. `stale_obs_no_fire` — obs older than freshness window does not fire
  4. `morning_skip` — outside local fire-hour window, no fire
  5. `cap_abort_no_chase` — ask above cap aborts; no chase
  6. `no_double_fire` — second fire for same `city|date|direction` blocked
  7. `consensus_blocks_non_leader` — non-leader bucket is rejected by the consensus filter
- **Coverage:** none configured/measured. No CI. To "test" a fill against real books later, see `PAPER.md`: dump book JSON, seed into the matcher, call `plan_fire_cycle` + `paper_match_fak` with `mode=paper`.
- **Adding behavior** to strategy → add a `scenario_*` function + wire it into `run_scenarios()`, then run `python3 tests_reversal.py`.
