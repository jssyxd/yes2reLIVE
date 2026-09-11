#!/usr/bin/env python3
"""tests_ws_and_consensus.py — verification suite for:
1. Polymarket CLOB WebSocket market event parsing (event_type vs type, asset_id vs tokenId)
2. ConsensusTracker serialization, pruning, and file persistence
3. Strategy NO-leg disabling and 100% YES allocation
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from consensus_tracker import ConsensusTracker
from market_ws_transport import _feed_text_to_stream
from reversal_strategy import maybe_arm_or_fire
from websocket_market_data import MarketStream


def test_ws_payload_parsing() -> None:
    token1 = "1111111111111111111111111111111111111111111111111111111111111111"
    token2 = "2222222222222222222222222222222222222222222222222222222222222222"
    stream = MarketStream([token1, token2])
    stream.mark_connected()
    stream.mark_subscribed()

    assert stream.event_count == 0

    # 1. Test initial post-subscribe dump (list of book objects with asset_id)
    initial_dump = json.dumps([
        {
            "event_type": "book",
            "asset_id": token1,
            "market": "0xcond1",
            "timestamp": "1726000000000",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "150"}],
            "tick_size": "0.01",
            "min_order_size": "5",
        },
        {
            "event_type": "book",
            "asset_id": token2,
            "market": "0xcond2",
            "timestamp": "1726000000000",
            "bids": [{"price": "0.30", "size": "80"}],
            "asks": [{"price": "0.40", "size": "120"}],
            "tick_size": "0.01",
            "min_order_size": "5",
        }
    ])
    _feed_text_to_stream(stream, initial_dump)
    assert stream.books[token1].ready is True
    assert stream.books[token2].ready is True
    assert stream.books[token1].snapshot().best_ask == Decimal("0.55")
    assert stream.books[token1].snapshot().best_bid == Decimal("0.45")
    assert stream.event_count >= 2

    cnt_after_dump = stream.event_count

    # 2. Test Polymarket official price_change payload (with event_type and asset_id)
    price_change_payload = json.dumps({
        "event_type": "price_change",
        "market": "0xcond1",
        "timestamp": "1726000005000",
        "price_changes": [
            {
                "asset_id": token1,
                "price": "0.56",
                "size": "200",
                "side": "SELL",
                "best_bid": "0.45",
                "best_ask": "0.55"
            }
        ]
    })
    _feed_text_to_stream(stream, price_change_payload)
    assert stream.event_count == cnt_after_dump + 1
    # Check that level was added to asks
    assert Decimal("0.56") in stream.books[token1]._asks

    # 3. Test Polymarket official best_bid_ask payload
    bba_payload = json.dumps({
        "event_type": "best_bid_ask",
        "market": "0xcond1",
        "asset_id": token1,
        "best_bid": "0.46",
        "best_ask": "0.55",
        "spread": "0.09"
    })
    _feed_text_to_stream(stream, bba_payload)
    assert stream.event_count == cnt_after_dump + 2

    # 4. Test last_trade_price payload
    ltp_payload = json.dumps({
        "event_type": "last_trade_price",
        "asset_id": token1,
        "price": "0.55"
    })
    _feed_text_to_stream(stream, ltp_payload)
    assert stream.books[token1].last_trade_price == Decimal("0.55")
    assert stream.event_count == cnt_after_dump + 3

    print("PASS test_ws_payload_parsing")


def test_consensus_tracker_persistence() -> None:
    tracker = ConsensusTracker(window_seconds=7200, min_samples=3)
    now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

    city_id = "tokyo"
    date_str = "2026-09-12"
    direction = "high"
    bucket_fav = "b30"
    bucket_other = "b31"

    # Seed samples
    for i in range(10):
        t = now - timedelta(minutes=50 - i * 5)
        tracker.record(city_id, date_str, direction, bucket_fav, mid=0.60, best_ask=0.61, best_bid=0.59, ask_depth=20, now_utc=t)
        tracker.record(city_id, date_str, direction, bucket_other, mid=0.25, best_ask=0.26, best_bid=0.24, ask_depth=15, now_utc=t)

    res_before = tracker.is_long_horizon_consensus(city_id, date_str, direction, bucket_fav, now_utc=now)
    assert res_before["ok"] is True
    assert res_before["rank"] == 1

    # Save to temp file
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "yes2re_consensus.json"
        tracker.save_to_file(fpath, now_utc=now)
        assert fpath.exists()

        # Load into fresh tracker
        new_tracker = ConsensusTracker(window_seconds=7200, min_samples=3)
        ok = new_tracker.load_from_file(fpath, now_utc=now)
        assert ok is True

        res_after = new_tracker.is_long_horizon_consensus(city_id, date_str, direction, bucket_fav, now_utc=now)
        assert res_after["ok"] is True
        assert res_after["rank"] == 1
        assert res_after["twap"] == res_before["twap"]
        assert res_after["n_samples"] == res_before["n_samples"]

        # Test pruning on load: load with time far in future
        future_time = now + timedelta(hours=5)
        future_tracker = ConsensusTracker(window_seconds=7200, min_samples=3)
        future_tracker.load_from_file(fpath, now_utc=future_time)
        res_future = future_tracker.is_long_horizon_consensus(city_id, date_str, direction, bucket_fav, now_utc=future_time)
        # All samples should have been pruned
        assert res_future["ok"] is False
        assert res_future["reason"] == "no_price_history"

    print("PASS test_consensus_tracker_persistence")


def test_strategy_no_leg_disabled() -> None:
    city = {"city_id": "tokyo", "icao": "RJTT", "timezone": "Asia/Tokyo", "market_unit": "C"}
    buckets = [
        {"bucket_id": "h29", "lo": 29.0, "hi": 30.0, "no_token_id": "NO-29", "yes_token_id": "YES-29"},
        {"bucket_id": "h30", "lo": 30.0, "hi": 31.0, "no_token_id": "NO-30", "yes_token_id": "YES-30"},
        {"bucket_id": "h31", "lo": 31.0, "hi": 32.0, "no_token_id": "NO-31", "yes_token_id": "YES-31"},
    ]
    now = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)
    tracker = ConsensusTracker(window_seconds=7200, min_samples=3)
    for i in range(10):
        t = now - timedelta(minutes=40 - i * 4)
        tracker.record("tokyo", "2026-09-12", "high", "h30", mid=0.60, best_ask=0.62, best_bid=0.58, now_utc=t)

    cfg = {
        "no_leg_enabled": False,
        "no_notional_pct": 0.0,
        "yes_notional_pct": 1.0,
        "yes_leg_enabled": True,
        "no_max_ask": "1.0",
        "yes_max_ask": "0.90",
        "require_fresh_obs_seconds": 180,
        "require_consensus_filter": True,
        "consensus_min_samples": 5,
        "high_fire_local_start": 12,
        "high_fire_local_hour_end": 18,
    }

    state = {}
    books = {
        "NO-30": {"best_ask": "0.95", "tick_size": "0.01", "asks": [{"price": "0.95", "size": "100"}]},
        "YES-31": {"best_ask": "0.55", "tick_size": "0.01", "asks": [{"price": "0.55", "size": "100"}]},
    }

    # First arm with 30.2, then fire with 31.4
    actions = []
    actions.extend(maybe_arm_or_fire(
        state, city, "2026-09-12", "high", buckets,
        30.5, 30.2, now - timedelta(minutes=5), now - timedelta(minutes=5), books, cfg, tracker
    ))
    actions.extend(maybe_arm_or_fire(
        state, city, "2026-09-12", "high", buckets,
        30.5, 31.4, now, now, books, cfg, tracker
    ))

    fire = next((a for a in actions if a.get("action_type") == "re_fire"), None)
    assert fire is not None, f"Expected fire, got: {actions}"

    legs = fire.get("legs", [])
    assert len(legs) == 1, f"Expected exactly 1 leg (YES only), got: {legs}"
    assert legs[0]["leg"] == "buy_yes_new"
    assert legs[0]["outcome"] == "YES"
    assert legs[0]["notional_pct"] == "1.0"
    assert legs[0]["token_id"] == "YES-31"

    print("PASS test_strategy_no_leg_disabled")


def main() -> int:
    test_ws_payload_parsing()
    test_consensus_tracker_persistence()
    test_strategy_no_leg_disabled()
    print("All ws and consensus tests passed successfully!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
