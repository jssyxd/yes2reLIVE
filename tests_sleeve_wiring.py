"""Wiring test for the B2 sleeve path (_sleeve_tick / _enter_sleeve).

Regression for 2026-09-06: `rule["key"]` was never injected into rule dicts
by refresh_rules, so the first real sleeve signal crashed with
`sleeve_tick_error: KeyError: 'key'` (×53 on the B2 arm — the sleeve never
actually entered). This test exercises the live wiring with a key-carrying
rule and asserts the sleeve enters state instead of erroring.

Run: /usr/bin/python3 tests_sleeve_wiring.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, ".")
import _r_cycle  # noqa: E402
from sleeve_signal import PriceRing  # noqa: E402

FAIL = 0
PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def _book(price: str) -> dict:
    p = float(price)
    return {"best_ask": price, "best_bid": str(max(0.0, p - 0.01)),
            "asks": [{"price": price, "size": "100"}], "bids": [{"price": str(max(0.0, p - 0.01)), "size": "50"}]}


def _mk_buckets() -> list[dict]:
    out = []
    for t in range(28, 34):
        out.append({"bucket_id": f"h{t}", "lo": float(t), "hi": float(t + 1),
                    "yes_token_id": f"YES-{t}", "no_token_id": f"NO-{t}"})
    return out


def _seed_trend(ring: PriceRing, tok: str, flat: float, to: float, n_flat: int = 60, n_trend: int = 36) -> float:
    base = float(int(time.time())) - 500.0  # anchor near real now; _sleeve_tick uses time.time()
    for i in range(n_flat):
        ring.record(tok, Decimal(str(flat)), base + i * 5)
    start = base + n_flat * 5
    for i in range(n_trend):
        frac = i / max(1, n_trend - 1)
        ring.record(tok, Decimal(str(round(flat + (to - flat) * frac, 3))), start + i * 5)
    return start + n_trend * 5


def test_sleeve_tick_enters_with_key() -> None:
    """A key-carrying rule + trending books must ENTER a sleeve (no KeyError)."""
    cfg = {
        "log_path": "/tmp/test_sleeve_events.jsonl",
        "fire_budget_usdc": 20,
        "strategy": {
            "sleeve_enabled": True,
            "sleeve_notional_pct": 0.08,
            "sleeve_max_ask": 0.35,
            "sleeve_rank1_drop": 0.05,
            "sleeve_neighbour_rise": 0.04,
            "sleeve_short_window_s": 150,
            "sleeve_long_window_s": 480,
        },
    }
    state = {"paper_initial_capital_usdc": 5000.0, "paper_total_debit_usdc": 0.0,
             "entry_count": 0, "weatherbotyes2re": {"fired": {}, "sleeves": {}, "armed": {}}, "positions": {}}
    tree = state["weatherbotyes2re"]
    buckets = _mk_buckets()
    rule = {
        "key": "testville|2026-09-06|high",  # ← the injected field
        "city_id": "testville",
        "market_local_date": "2026-09-06",
        "direction": "high",
        "icao": "TEST",
        "buckets": buckets,
    }
    ring = PriceRing(window_s=1000)
    now = _seed_trend(ring, "YES-31", 0.75, 0.60)   # rank1 weakens (idx 3)
    now = max(now, _seed_trend(ring, "YES-32", 0.10, 0.30))  # neighbour rises
    _seed_trend(ring, "YES-30", 0.40, 0.40)
    _seed_trend(ring, "YES-28", 0.03, 0.03)
    _seed_trend(ring, "YES-29", 0.15, 0.15)
    _seed_trend(ring, "YES-33", 0.02, 0.02)
    books = {t: _book(p) for t, p in {
        "YES-31": "0.60", "YES-32": "0.30", "YES-30": "0.40",
        "YES-28": "0.03", "YES-29": "0.15", "YES-33": "0.02"}.items()}

    # Stub book_cache so _paper_fire's FAK ladder has a book for the sleeve token.
    _r_cycle._SLEEVE_RING = ring  # deterministic ring
    orig_cache = None
    import _r_globals
    orig = _r_globals._BOOK_CACHE
    _r_globals._BOOK_CACHE = {k: v for k, v in books.items()}
    try:
        now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        _r_cycle._sleeve_tick(cfg, state, rule, books, tree, now_utc)
    finally:
        _r_globals._BOOK_CACHE = orig

    sleeves = state["weatherbotyes2re"]["sleeves"]
    check("sleeve_entered_state", "testville|2026-09-06|high" in sleeves,
          f"sleeves={list(sleeves.keys())}")
    if "testville|2026-09-06|high" in sleeves:
        check("sleeve_status_open", sleeves["testville|2026-09-06|high"].get("status") == "open",
              str(sleeves["testville|2026-09-06|high"]))
    positions = state.get("positions", {})
    check("position_recorded", "testville|2026-09-06|high#sleeve" in positions,
          f"positions={list(positions.keys())}")


def test_sleeve_tick_no_key_no_crash() -> None:
    """A rule WITHOUT key must not raise (defensive: use .get in key paths)."""
    # _sleeve_tick reads rule["key"] only on signal; without a signal it returns
    # before touching key. Assert a flat-book (no signal) call with a keyless
    # rule is a silent no-op, not a crash.
    cfg = {"log_path": "/tmp/test_sleeve_events2.jsonl", "strategy": {"sleeve_enabled": True}}
    state = {"paper_initial_capital_usdc": 5000.0, "paper_total_debit_usdc": 0.0,
             "weatherbotyes2re": {"fired": {}, "sleeves": {}, "armed": {}}, "positions": {}}
    buckets = _mk_buckets()
    rule = {"city_id": "x", "market_local_date": "2026-09-06", "direction": "high", "buckets": buckets}  # no key
    ring = PriceRing(window_s=1000)
    for b in buckets:
        ring.record(b["yes_token_id"], Decimal("0.30"), 100.0 + time.time())
    _r_cycle._SLEEVE_RING = ring
    books = {b["yes_token_id"]: _book("0.30") for b in buckets}
    now_utc = datetime.now(timezone.utc)
    _r_cycle._sleeve_tick(cfg, state, rule, books, state["weatherbotyes2re"], now_utc)  # must not raise
    check("no_key_no_crash", True)


def main() -> None:
    test_sleeve_tick_enters_with_key()
    test_sleeve_tick_no_key_no_crash()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
