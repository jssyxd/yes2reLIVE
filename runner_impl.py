"""Paper reversal runner main. No sigma."""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from reversal_strategy import ensure_re_state
from _r_state import load_config, load_state, save_state, log_event, STATE_VERSION
from _r_cycle import run_cycle
import _r_globals
from _r_exec import write_health
from research import common as _common_adapter


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["once", "run", "status"])
    ap.add_argument("--config", default="config/yes2re_reversal.json")
    ap.add_argument("--max-seconds", type=float, default=0, help="Stop run loop after N seconds (0 = forever)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # Optional CheckWX key from .env (AWC works keyless). Never logged.
    _env = {}
    try:
        _env = _common_adapter.load_env()
    except Exception:
        _env = {}
    cfg["_checkwx_key"] = os.environ.get("CHECKWX_API_KEY") or _env.get("CHECKWX_API_KEY")
    state = load_state(cfg["state_path"])
    if state.get("version") != STATE_VERSION:
        capital = state.get("paper_initial_capital_usdc", cfg["paper_initial_capital_usdc"])
        state = {
            "positions": {},
            "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
            "version": STATE_VERSION,
            "paper_initial_capital_usdc": capital,
        }
    # A freshly blanked state (no file on disk) carries the DEFAULTS capital
    # because load_state blanks with an empty cfg. Force the run-config value
    # only when no ledger activity exists yet (no positions, no debit): once
    # the account has traded we must never rewrite initial capital.
    if not state.get("positions") and not state.get("paper_total_debit_usdc"):
        state["paper_initial_capital_usdc"] = float(cfg["paper_initial_capital_usdc"])
    state.setdefault("paper_initial_capital_usdc", cfg["paper_initial_capital_usdc"])
    ensure_re_state(state)

    if args.command == "status":
        write_health(cfg, state)
        print(json.dumps({
            "health": cfg["health_path"],
            "armed": list(ensure_re_state(state).get("armed", {})),
            "fired": list(ensure_re_state(state).get("fired", {})),
            "open": sum(1 for p in state["positions"].values() if not p.get("settled")),
            "entries": state.get("entry_count", 0),
        }, indent=2))
        return 0

    if args.command == "once":
        run_cycle(cfg, state, datetime.now(timezone.utc))
        save_state(cfg["state_path"], state)
        write_health(cfg, state)
        print("once cycle complete")
        return 0

    print("yes2re reversal paper runner started (no live orders, no sigma)", flush=True)
    started = time.time()
    while True:
        try:
            armed = run_cycle(cfg, state, datetime.now(timezone.utc))
            save_state(cfg["state_path"], state)
            write_health(cfg, state)
        except Exception as exc:
            log_event(cfg["log_path"], {"type": "cycle_error", "error": type(exc).__name__, "detail": str(exc)[:400]})
            print(f"cycle_error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            time.sleep(cfg["scan_interval_seconds"])
            if args.max_seconds and (time.time() - started) >= args.max_seconds:
                break
            continue
        if args.max_seconds and (time.time() - started) >= args.max_seconds:
            print(f"max-seconds {args.max_seconds} reached — stopping", flush=True)
            break
        sleep_s = cfg["fast_poll_interval_seconds"] if armed else cfg["scan_interval_seconds"]
        if armed:
            # WS-event wake: when a watched (armed-session) token moves on the
            # market feed, skip the rest of the sleep and run the next cycle
            # immediately, so fire decisions ride the tick instead of the
            # fixed fast cadence (avg saving ~ fast_poll/2 + reaction time).
            _wake_before = _r_globals.wake_epoch()
            _deadline = time.time() + float(sleep_s)
            while True:
                _remaining = _deadline - time.time()
                if _remaining <= 0:
                    break
                if _r_globals.wake_epoch() > _wake_before:
                    break
                time.sleep(min(0.15, _remaining))
        else:
            time.sleep(float(sleep_s))
    write_health(cfg, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
