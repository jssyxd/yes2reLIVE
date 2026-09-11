"""Unit tests for sleeve_signal (B2 pre-breach detector). Pure + deterministic.

Run: /usr/bin/python3 tests_sleeve_signal.py
"""
from __future__ import annotations

import sys
import time
from decimal import Decimal

sys.path.insert(0, ".")
from sleeve_signal import (  # noqa: E402
    PriceRing,
    SleeveSignal,
    best_ask_of,
    detect_sleeve_signal,
    locate_rank1_bucket,
    update_rings_from_books,
)

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


def mk_buckets() -> list[dict]:
    # temperature buckets high-direction: 28-29, 29-30, 30-31, 31-32, 32-33
    return [
        {"lo": 28, "hi": 29, "yes_token_id": "t_28", "no_token_id": "n_28"},
        {"lo": 29, "hi": 30, "yes_token_id": "t_29", "no_token_id": "n_29"},
        {"lo": 30, "hi": 31, "yes_token_id": "t_30", "no_token_id": "n_30"},
        {"lo": 31, "hi": 32, "yes_token_id": "t_31", "no_token_id": "n_31"},
        {"lo": 32, "hi": 33, "yes_token_id": "t_32", "no_token_id": "n_32"},
    ]


def book(price: str) -> dict:
    return {"best_ask": price, "best_bid": str(max(0.0, float(price) - 0.01)), "asks": [{"price": price, "size": "100"}], "bids": []}


def seed_ring_trend(ring: PriceRing, tok: str, start: float, end: float, n: int = 40, now0: float = 1000.0) -> None:
    """Seed a linear price trend from start->end over n ticks, 5s apart."""
    for i in range(n):
        frac = i / max(1, n - 1)
        p = start + (end - start) * frac
        ring.record(tok, Decimal(str(round(p, 3))), now0 + i * 5)


def seed_ring_flat_then_trend(ring: PriceRing, tok: str, flat: float, trend_to: float, n_flat: int = 60, n_trend: int = 36) -> float:
    """Seed `flat` for n_flat ticks (5s apart), then a linear trend to
    `trend_to` over n_trend ticks. Returns the epoch just after seeding
    (caller uses it as `now`)."""
    base = 10000.0
    for i in range(n_flat):
        ring.record(tok, Decimal(str(flat)), base + i * 5)
    start_t = base + n_flat * 5
    for i in range(n_trend):
        frac = i / max(1, n_trend - 1)
        p = flat + (trend_to - flat) * frac
        ring.record(tok, Decimal(str(round(p, 3))), start_t + i * 5)
    return start_t + n_trend * 5


def test_twap_basic() -> None:
    ring = PriceRing(window_s=1000)
    now0 = 2000.0
    # constant 0.50 for 20 ticks (5s apart) = 95s of data
    for i in range(20):
        ring.record("x", Decimal("0.50"), now0 + i * 5)
    tw = ring.twap("x", 100, now0 + 20 * 5)
    check("twap_constant", tw is not None and abs(float(tw) - 0.50) < 1e-9, f"tw={tw}")


def test_twap_weights_recent() -> None:
    ring = PriceRing(window_s=1000)
    now0 = 3000.0
    # 0.40 for the first 60s, then 0.60 for the next 60s (ticks every 5s)
    for i in range(12):
        ring.record("x", Decimal("0.40"), now0 + i * 5)
    for i in range(12):
        ring.record("x", Decimal("0.60"), now0 + 60 + i * 5)
    # TWAP over the last 150s covers both halves equally -> ~0.50
    tw150 = ring.twap("x", 150, now0 + 120)
    # TWAP over the last 60s covers only the 0.60 half -> ~0.60
    tw60 = ring.twap("x", 60, now0 + 120)
    check("twap_150_half", tw150 is not None and abs(float(tw150) - 0.50) < 0.02, f"tw150={tw150}")
    check("twap_60_recent", tw60 is not None and float(tw60) > 0.58, f"tw60={tw60}")


def test_detect_high_direction() -> None:
    buckets = mk_buckets()
    ring = PriceRing(window_s=1200)
    # Timeline: 5 min flat (old steady state) then 3 min trend (the move).
    # t_30 (rank1 YES): flat 0.80 -> drops to 0.66
    # t_31 (neighbour YES): flat 0.10 -> rises to 0.28
    now = seed_ring_flat_then_trend(ring, "t_30", 0.80, 0.66)
    now = max(now, seed_ring_flat_then_trend(ring, "t_31", 0.10, 0.28))
    seed_ring_flat_then_trend(ring, "t_28", 0.03, 0.03)
    seed_ring_flat_then_trend(ring, "t_29", 0.15, 0.15)
    seed_ring_flat_then_trend(ring, "t_32", 0.02, 0.02)

    books = {
        "t_30": book("0.66"),
        "t_31": book("0.28"),
        "t_29": book("0.15"),
        "t_28": book("0.03"),
        "t_32": book("0.02"),
    }
    # rank1 by long TWAP: t_30 still highest on the long window
    r1 = locate_rank1_bucket(buckets, books, ring, now)
    check("locate_rank1_is_t30", r1 == 2, f"r1_idx={r1}")

    res = detect_sleeve_signal(
        buckets=buckets,
        rank1_bucket_idx=2,
        direction="high",
        books_by_token=books,
        ring=ring,
        now=now,
        short_window_s=150,
        long_window_s=480,
        rank1_weaken_drop=Decimal("0.03"),
        neighbour_strengthen_rise=Decimal("0.03"),
        neighbour_max_ask=Decimal("0.35"),
    )
    check("signal_detected", res is not None)
    if res:
        n_idx, sig = res
        check("neighbour_is_t31", n_idx == 3 and sig.neighbour_yes_token == "t_31", f"n_idx={n_idx}")


def test_no_signal_when_flat() -> None:
    buckets = mk_buckets()
    ring = PriceRing(window_s=1000)
    now0 = 9000.0
    # everything flat -> no weakening/strengthening
    for tok in ("t_28", "t_29", "t_30", "t_31", "t_32"):
        seed_ring_trend(ring, tok, 0.30, 0.30, now0=now0)
    books = {t: book("0.30") for t in ("t_28", "t_29", "t_30", "t_31", "t_32")}
    now = now0 + 40 * 5
    res = detect_sleeve_signal(
        buckets=buckets,
        rank1_bucket_idx=2,
        direction="high",
        books_by_token=books,
        ring=ring,
        now=now,
        short_window_s=120,
        long_window_s=300,
    )
    check("no_signal_flat", res is None)


def test_no_signal_when_neighbour_expensive() -> None:
    buckets = mk_buckets()
    ring = PriceRing(window_s=1000)
    now0 = 13000.0
    seed_ring_trend(ring, "t_30", 0.80, 0.70, now0=now0)
    seed_ring_trend(ring, "t_31", 0.10, 0.60, now0=now0)  # rises but ends expensive
    books = {"t_30": book("0.70"), "t_31": book("0.60")}
    now = now0 + 40 * 5
    res = detect_sleeve_signal(
        buckets=buckets,
        rank1_bucket_idx=2,
        direction="high",
        books_by_token=books,
        ring=ring,
        now=now,
        short_window_s=120,
        long_window_s=300,
        neighbour_max_ask=Decimal("0.35"),
    )
    check("no_signal_expensive", res is None)


def test_update_rings_records_books() -> None:
    ring = PriceRing()
    buckets = mk_buckets()
    books = {"t_28": book("0.05"), "t_29": book("0.12")}
    update_rings_from_books(ring, buckets, books, now=50.0)
    check("ring_has_t28", ring.count("t_28", 50.0) == 1)
    check("ring_has_t29", ring.count("t_29", 50.0) == 1)
    check("ring_missing_absent", ring.count("t_30", 50.0) == 0)


def test_event_shape() -> None:
    s = SleeveSignal(
        key="tokyo|2026-09-06|high",
        city_id="tokyo",
        market_local_date="2026-09-06",
        direction="high",
        rank1_bucket_idx=1,
        neighbour_bucket_idx=2,
        neighbour_yes_token="abc",
        neighbour_yes_ask=Decimal("0.22"),
        rank1_yes_twap_short=Decimal("0.61"),
        rank1_yes_twap_long=Decimal("0.68"),
        neighbour_yes_twap_short=Decimal("0.24"),
        neighbour_yes_twap_long=Decimal("0.18"),
        spread_ratio=Decimal("0.40"),
        reason="rank1_weaken_neighbour_rise",
    )
    ev = s.as_event("2026-09-06T04:00:00Z")
    check("event_has_type", ev["type"] == "sleeve_signal")
    check("event_has_key", ev["key"] == "tokyo|2026-09-06|high")


def main() -> None:
    test_twap_basic()
    test_twap_weights_recent()
    test_detect_high_direction()
    test_no_signal_when_flat()
    test_no_signal_when_neighbour_expensive()
    test_update_rings_records_books()
    test_event_shape()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
