#!/usr/bin/env python3
"""Phase-3 smoke order — prove the whole loop on real money, with the smallest possible order.

What it does, in order (every step audited to ``data/live_events.jsonl``):

1. **triple gate** (``--enable-submit`` + ``LIVE_SUBMIT_ENABLED=1`` + ``--confirm <phrase>``)
2. find one active weather bucket with a real two-sided book (Gamma hint → CLOB book decides)
3. build a **deliberately non-marketable** 5 USDC limit BUY: price =
   ``min(best_bid, best_ask − 2·tick)`` (aligned down, ≥ 1 tick) — it cannot fill
4. preflight: ``risk_gate.evaluate`` → ``order_plan.plan_order`` → per-order + cumulative
   limits → non-marketable re-check (all four must pass, in that order)
5. submit once (``post_order`` with ``post_only=True``)
6. poll ``get_order`` until the order is confirmed ``live``/``unmatched``/``open``
7. ``cancel`` it, then confirm ``cancelled`` via ``get_order``
8. reconcile (``live/reconcile.collect``) and require 0 open orders
9. write the report to ``data/live_smoke.json``

Any failure: cancel what we can (by id, else by matching token+price from the open-order
list), then report the residual risk explicitly — never a silent "probably fine".

``--dry-plan`` builds and prints the plan from a synthetic (or ``--book <file>``) book with
**no network call, no client and no write path** — safe to run anywhere, any time.

Exit codes: 0 ok · 2 fail-closed · 3 gate refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

if __package__ in (None, ""):  # `python3.13 live/...py` — make relative imports work
    __package__ = "live"  # `python3.13 live/smoke.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from live import clob_client, creds as creds_mod, order_plan, reconcile, risk_gate, sign_dryrun, submit
else:  # `python3.13 tests_live.py` / `import live.smoke`
    from . import clob_client, creds as creds_mod, order_plan, reconcile, risk_gate, sign_dryrun, submit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path("data/live_smoke.json")
DEFAULT_BUDGET_USDC = "5"
TICK_GAP = 2                      # place this many ticks below the best bid
POLL_ATTEMPTS = 6
POLL_SLEEP_SECONDS = 1.0
#: after a *confirmed* cancel the CLOB open-order list can lag a few seconds; re-read it a few
#: times before crying "residual risk" (real money is not at stake, but false alarms are)
OPEN_ORDERS_ATTEMPTS = 3
OPEN_ORDERS_SLEEP_SECONDS = 5.0
CONFIRMED_STATUSES = ("live", "unmatched", "open", "delayed")
FILLED_STATUSES = ("matched", "filled", "partially_filled", "partial")
CANCELLED_STATUSES = ("cancelled", "canceled")

#: synthetic book for ``--dry-plan`` (no network, no real token)
DRY_BOOK = {
    "best_bid": "0.50", "best_ask": "0.523", "tick_size": "0.01", "min_order_size": "5",
    "neg_risk": False,
    "bids": [{"price": "0.50", "size": "120"}],
    "asks": [{"price": "0.523", "size": "80"}],
}
DRY_TOKEN_ID = "9" * 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dec(value, field: str):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}: missing")
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        raise ValueError(f"{field}: not a number") from None
    if not out.is_finite() or out <= 0:
        raise ValueError(f"{field}: must be > 0 and finite")
    return out


def smoke_price(book) -> dict:
    """Pick a price that cannot fill: below the best bid, and 2 ticks under the best ask.

    Pure. Requires both sides of the book — without a bid we cannot prove the order is
    passive, so we refuse instead of guessing.
    """
    if not isinstance(book, dict):
        return {"ok": False, "reason": "no_book", "detail": "book: missing", "price": None}
    try:
        tick = _dec(book.get("tick_size") or "0.01", "tick_size")
    except ValueError as exc:
        return {"ok": False, "reason": "invalid_input", "detail": str(exc), "price": None}
    try:
        best_bid = _dec(book.get("best_bid"), "best_bid")
        best_ask = _dec(book.get("best_ask"), "best_ask")
    except ValueError as exc:
        return {"ok": False, "reason": "no_book",
                "detail": f"{exc} — cannot prove the order would rest passively", "price": None}
    if best_bid >= best_ask:
        return {"ok": False, "reason": "invalid_book",
                "detail": f"crossed/empty book: bid {best_bid} >= ask {best_ask}", "price": None}
    raw = min(best_bid, best_ask - tick * TICK_GAP)
    price = order_plan.align_down(raw, tick)
    if price < tick:
        return {"ok": False, "reason": "price_below_tick",
                "detail": f"candidate price {raw} aligned to {price} < tick {tick}", "price": None}
    if price >= best_ask:
        return {"ok": False, "reason": "would_cross",
                "detail": f"price {price} >= best_ask {best_ask}", "price": None}
    return {"ok": True, "reason": "ok", "price": price, "tick": tick,
            "detail": f"passive: {price} < min(bid {best_bid}, ask {best_ask} - {TICK_GAP}tick)",
            "best_bid": str(best_bid), "best_ask": str(best_ask)}


def _override_price(book, raw) -> dict:
    """Operator-supplied price, aligned to tick. Passivity is checked later against the
    *real* book (``submit.check_non_marketable``), which is the authoritative gate."""
    raw_tick = book.get("tick_size") if isinstance(book, dict) else None
    try:
        tick = _dec(raw_tick, "tick_size") if raw_tick else Decimal("0.01")
        price = order_plan.align_down(_dec(raw, "price"), tick)
    except ValueError as exc:
        return {"ok": False, "reason": "invalid_input", "detail": str(exc), "price": None}
    if price < tick:
        return {"ok": False, "reason": "price_below_tick",
                "detail": f"override {raw} aligned to {price} < tick {tick}", "price": None}
    return {"ok": True, "reason": "ok", "price": price, "tick": tick,
            "detail": f"operator override {raw} -> {price} (passivity re-checked before submit)"}


def build_smoke_plan(*, book, budget_usdc=DEFAULT_BUDGET_USDC, token_id=None,
                     direction="buy_yes", caps=None, price_override=None) -> dict:
    """Pure plan construction: smoke price + ``order_plan`` size/min-size math."""
    caps = caps if caps is not None else order_plan.load_caps()
    price_choice = smoke_price(book) if price_override is None else _override_price(book, price_override)
    if not price_choice["ok"]:
        return {"ok": False, "reason": price_choice["reason"], "detail": price_choice["detail"],
                "price_choice": price_choice, "plan": None}
    plan = order_plan.plan_order(
        direction=direction,
        token_id=token_id,
        # feed the passive price in as the reference so order_plan's tick alignment is a
        # no-op and its size / min_order_size / budget math applies unchanged
        book={**book, "best_ask": str(price_choice["price"])},
        budget_usdc=budget_usdc,
        price_cap=order_plan.cap_for(direction, caps),
    )
    return {"ok": plan["ok"], "reason": plan["reason"], "detail": plan["detail"],
            "price_choice": {key: (str(value) if isinstance(value, Decimal) else value)
                             for key, value in price_choice.items()},
            "plan": plan}


# --------------------------------------------------------------------------- preflight

def _limits_from_env(env: dict) -> dict:
    return {name: env.get(key) or None for name, key in reconcile.LIMIT_KEYS.items()}


# --------------------------------------------------------------------------- order helpers

def verify_order(client, order_id: str, *, attempts: int = POLL_ATTEMPTS,
                 sleep_seconds: float = POLL_SLEEP_SECONDS, sleep=None,
                 statuses=CONFIRMED_STATUSES) -> dict:
    """Poll ``get_order`` until the order shows up in a resting state (read-only)."""
    sleep = sleep if sleep is not None else _sleep
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = submit.get_order(client, order_id)
        except Exception as exc:  # noqa: BLE001 - read failure: fail closed, caller recovers
            return {"ok": False, "reason": "get_order_failed", "attempts": attempt,
                    "detail": f"{type(exc).__name__}: {exc}", "last": last}
        last = result["response_summary"]
        status = str(last.get("status") or "").lower()
        matched = str(last.get("size_matched") or "0")
        if status in FILLED_STATUSES or _is_filled(matched):
            # a passive smoke order must never fill: surface it immediately, do not retry
            return {"ok": False, "reason": "order_partially_filled", "attempts": attempt,
                    "status": status, "size_matched": matched, "last": last}
        if status in statuses:
            return {"ok": True, "reason": "ok", "attempts": attempt, "status": status,
                    "size_matched": matched, "last": last}
        if sleep_seconds:
            sleep(sleep_seconds)
    return {"ok": False, "reason": "order_not_confirmed", "attempts": attempts, "last": last}


def _is_filled(size_matched) -> bool:
    if size_matched is None or str(size_matched).strip().lower() in ("", "none", "null"):
        return False
    try:
        return Decimal(str(size_matched).strip()) > 0
    except (InvalidOperation, AttributeError, ValueError):
        return True  # unparseable ⇒ assume the worst


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def await_no_open_orders(client, order_id, *, cancel_confirmed: bool,
                         attempts: int = OPEN_ORDERS_ATTEMPTS,
                         sleep_seconds: float = OPEN_ORDERS_SLEEP_SECONDS,
                         sleep=None) -> dict:
    """Read the live open-order list until our (cancelled) order is gone.

    The CLOB list endpoint is cached and can lag a confirmed cancel by a few seconds, so a single
    read is not evidence. Distinguishes:

    * the list clears (possibly only after a retry) ⇒ ``ok``; a lag that needed a retry is
      recorded as ``cancel_confirmed_but_list_lag`` for humans (never as residual risk);
    * the list only ever shows **our** cancelled id and never clears ⇒
      ``cancel_confirmed_but_list_still_shows_order`` (residual risk, needs a human look);
    * the list shows **another** order id ⇒ ``other_orders_remain`` (residual risk);
    * not confirmed cancelled and orders remain ⇒ ``open_orders_remain`` (residual risk).
    """
    sleep = sleep or _sleep
    attempts = max(1, int(attempts))
    seen: list[list[str]] = []
    for attempt in range(1, attempts + 1):
        try:
            listed = submit.list_open_orders(client)
        except Exception as exc:  # noqa: BLE001 - cannot confirm ⇒ fail closed
            return {"ok": False, "status": "list_failed", "attempts": attempt,
                    "remaining_ids": seen[-1] if seen else [],
                    "detail": f"{type(exc).__name__}: {exc}", "lag_observed": False, "note": ""}
        ids = [str(row.get("id")) for row in (listed.get("orders") or [])]
        seen.append(ids)
        if not ids:
            lagged = attempt > 1
            return {"ok": True, "status": "clear", "attempts": attempt, "remaining_ids": [],
                    "lag_observed": lagged,
                    "note": ("cancel_confirmed_but_list_lag"
                             if (lagged and cancel_confirmed) else "")}
        if order_id and str(order_id) not in ids:
            return {"ok": False, "status": "other_orders_remain", "attempts": attempt,
                    "remaining_ids": ids, "lag_observed": False, "note": "",
                    "detail": f"open orders not ours: {ids}"}
        if attempt < attempts and sleep_seconds:
            sleep(sleep_seconds)
    remaining = seen[-1] if seen else []
    if cancel_confirmed and order_id and remaining and all(str(i) == str(order_id) for i in remaining):
        return {"ok": False, "status": "cancel_confirmed_but_list_still_shows_order",
                "attempts": attempts, "remaining_ids": remaining, "lag_observed": True,
                "note": "cancel_confirmed_but_list_lag",
                "detail": f"cancel of {order_id} was confirmed but the open-order list still "
                          f"shows it after {attempts} read(s) — confirm by hand"}
    return {"ok": False, "status": "open_orders_remain", "attempts": attempts,
            "remaining_ids": remaining, "lag_observed": len(remaining) > 0,
            "detail": f"open orders remain after {attempts} read(s): {remaining}"}


def rescue_cancel(client, *, order_id, token_id=None, price=None) -> dict:
    """Best-effort cleanup after a failure: cancel by id, else cancel matching orders.

    Only orders matching the token (and price, when known) are touched — this must never
    become a "cancel everything" button.
    """
    tried: list[str] = []
    if not order_id and token_id is None:
        # no order id and no token ⇒ we cannot tell *our* order from a real position's:
        # refuse instead of scanning (and cancelling) the whole account.
        return {"ok": False, "reason": "no_scope: refusing a blind cancel scan", "tried": tried}
    if order_id:
        try:
            result = submit.cancel_order(client, order_id)
            tried.append(order_id)
            if result.get("ok"):
                return {"ok": True, "canceled": [order_id], "tried": tried, "strategy": "by_order_id"}
        except Exception:  # noqa: BLE001 - fall through to the matching scan
            pass
    try:
        listed = submit.list_open_orders(client)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"list_failed:{type(exc).__name__}", "tried": tried}
    matches = []
    for row in listed.get("orders") or []:
        if token_id and str(row.get("asset_id")) != str(token_id):
            continue
        if price is not None:
            # numeric compare: the book may report "0.5" where we hold "0.50"
            try:
                if Decimal(str(row.get("price"))) != Decimal(str(price)):
                    continue
            except (InvalidOperation, AttributeError, ValueError):
                continue  # unparseable price ⇒ not provably ours ⇒ skip
        matches.append(row["id"])
    canceled, failed = [], []
    for match in matches:
        try:
            outcome = submit.cancel_order(client, match)
        except Exception as exc:  # noqa: BLE001
            failed.append({"order_id": match, "error": f"{type(exc).__name__}: {exc}"})
            continue
        (canceled if outcome.get("ok") else failed).append(match if outcome.get("ok") else
                                                           {"order_id": match,
                                                            "summary": outcome.get("response_summary")})
    return {"ok": not failed, "canceled": canceled, "failed": failed, "tried": tried,
            "matched_open_orders": matches, "strategy": "by_matching_token_and_price"}


# --------------------------------------------------------------------------- orchestration

def _report_skeleton(env: dict, *, budget: str, scenario: bool) -> dict:
    return {
        "ok": False, "reason": None, "ts_utc": _now_iso(), "phase": "phase3-smoke",
        "scenario": scenario, "mode": str(env.get("YES2RE_MODE") or "").strip() or None,
        "budget_usdc": budget, "gates": None, "sentinel": None, "market": None, "book": None,
        "plan": None, "risk_gate": None, "limits_check": None, "non_marketable": None,
        "selection": None, "bucket_attempts": [], "bucket_rejected": [],
        "open_orders_check": None,
        "order": {"order_id": None, "submitted": False, "confirmed": None, "cancelled": None},
        "reconcile": None, "residual_risk": False, "steps": [],
    }


# --------------------------------------------------------------------------- bucket selection

#: qualification codes for a candidate bucket (a dead/thin bucket must never be picked)
BUCKET_OK = "ok"
NO_BID = "no_bid"
NO_ASK = "no_ask"
ASK_AT_EXTREME = "ask_at_extreme"
ASK_SIZE_BELOW_MIN = "ask_size_below_min"
ASK_ABOVE_CAP = "ask_above_cap"
NO_PASSIVE_PRICE = "no_passive_price"
INVALID_BOOK = "invalid_book"
NO_TRADEABLE_BUCKET = "no_tradeable_bucket"

NEAR_MID = Decimal("0.5")
MAX_CANDIDATE_BOOKS = 8          # bound the read-only /book fetches per session
MAX_SELECTION_SESSIONS = 6


def _price_or_none(value) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def assess_book(book, *, cap=None, tick=None, min_order_size=None) -> dict:
    """Trader view of one book: two-sided, near-price, tradable, and cap-compatible.

    Pure. A bucket qualifies only when **both** sides quote: ``best_bid > 0`` and
    ``0 < best_ask < 1``, with at least ``min_order_size`` shares resting near the ask, and a
    passive price we can actually place (``min(best_bid, best_ask - 2*tick) >= tick``).
    Everything else is a recorded refusal — a dead bucket must never be silently picked.
    """
    out = {"ok": False, "reason": INVALID_BOOK, "detail": "", "best_bid": None, "best_ask": None,
           "tick": None, "min_order_size": None, "ask_size_near": None,
           "ask_distance": None, "passive_price": None, "cap": None}
    if not isinstance(book, dict):
        return {**out, "detail": "book: missing"}
    raw_tick = tick if tick is not None else book.get("tick_size")
    raw_min = min_order_size if min_order_size is not None else book.get("min_order_size")
    try:
        tick_value = _dec(raw_tick, "tick_size") if raw_tick else Decimal("0.01")
        minimum = _dec(raw_min, "min_order_size")
    except ValueError as exc:
        return {**out, "detail": str(exc)}
    out.update({"tick": str(tick_value), "min_order_size": str(minimum)})

    bid = _price_or_none(book.get("best_bid"))
    ask = _price_or_none(book.get("best_ask"))
    out.update({"best_bid": None if bid is None else str(bid),
                "best_ask": None if ask is None else str(ask)})
    if bid is None:
        return {**out, "reason": NO_BID,
                "detail": f"best_bid={book.get('best_bid')!r} — one-sided/dead bucket"}
    if ask is None:
        return {**out, "reason": NO_ASK,
                "detail": f"best_ask={book.get('best_ask')!r} — no resting ask"}
    one = Decimal(1)
    if not (Decimal(0) < ask < one):
        return {**out, "reason": ASK_AT_EXTREME,
                "detail": f"best_ask {ask} at an extreme (0,1) bound — dead bucket"}
    if ask > one - tick_value * TICK_GAP:
        # ask pinned against 1: the bucket is effectively decided and no passive BUY can rest
        # meaningfully below it (mirror of the can't-rest-below-tiny-ask case below)
        return {**out, "reason": ASK_AT_EXTREME,
                "detail": f"best_ask {ask} within {TICK_GAP} ticks of 1 — dead bucket"}
    if bid >= ask:
        return {**out, "reason": INVALID_BOOK, "detail": f"crossed book: bid {bid} >= ask {ask}"}

    near_limit = ask + tick_value * TICK_GAP
    ask_size_near = Decimal(0)
    for row in book.get("asks") or []:
        try:
            price, size = Decimal(str(row.get("price"))), Decimal(str(row.get("size")))
        except (InvalidOperation, AttributeError, ValueError, TypeError):
            continue
        if price <= near_limit:
            ask_size_near += size
    out["ask_size_near"] = str(ask_size_near)
    if ask_size_near < minimum:
        return {**out, "reason": ASK_SIZE_BELOW_MIN,
                "detail": f"near-ask size {ask_size_near} < min_order_size {minimum}"}
    if cap is not None:
        out["cap"] = str(cap)
        if ask > Decimal(str(cap)):
            return {**out, "reason": ASK_ABOVE_CAP, "detail": f"best_ask {ask} > cap {cap}"}

    choice = smoke_price(book)
    if not choice["ok"]:
        return {**out, "reason": NO_PASSIVE_PRICE, "detail": choice["detail"]}
    out.update({"ok": True, "reason": BUCKET_OK, "passive_price": str(choice["price"]),
                "ask_distance": str(abs(ask - NEAR_MID)),
                "detail": f"two-sided: bid {bid} / ask {ask}, near-ask size {ask_size_near}, "
                          f"passive {choice['price']}"})
    return out


def choose_bucket(candidates: list) -> dict | None:
    """Pick the qualifying bucket whose ``best_ask`` is closest to 0.5; ties → higher volume."""
    qualified = [row for row in candidates if (row.get("assessment") or {}).get("ok")]
    if not qualified:
        return None
    return min(qualified, key=lambda row: (row["assessment"]["ask_distance"],
                                           -float(row.get("volume") or 0)))


def _gamma_candidates(client, session: dict, *, cap, timeout: int) -> list[dict]:
    """Accepting markets of one Gamma event, ordered by 'most likely live & near mid'."""
    record = sign_dryrun._city_table().get(session["city"]) or {}
    slug = str(record.get("market_city_slug") or session["city"])
    event = clob_client.http_json(
        sign_dryrun.GAMMA_EVENT_ENDPOINT + sign_dryrun._event_slug(slug, session["local_date"],
                                                                   session["direction"]),
        timeout=timeout,
    )
    rows = []
    for market in event.get("markets") or []:
        if not isinstance(market, dict) or not market.get("acceptingOrders"):
            continue
        try:
            tokens = json.loads(market.get("clobTokenIds") or "[]")
        except ValueError:
            tokens = []
        if not tokens:
            continue
        rows.append({"market": market, "token_id": str(tokens[0]),
                     "volume": market.get("volumeNum"),
                     "hint_bid": market.get("bestBid"), "hint_ask": market.get("bestAsk")})

    def _rank(row: dict):
        has_bid = 0 if _price_or_none(row["hint_bid"]) else 1     # prefer quoted two-sided
        distance = abs((_price_or_none(row["hint_ask"]) or NEAR_MID) - NEAR_MID)
        return (has_bid, distance, -float(row["volume"] or 0))

    return sorted(rows, key=_rank)


def select_tradeable_bucket(client, *, city=None, local_date=None, direction=None, token_id=None,
                            cap=None, timeout: int = 25, limit=None,
                            max_candidates: int = MAX_CANDIDATE_BOOKS,
                            state_path=None) -> dict:
    """Find a *tradable* bucket: two-sided, near-price, ask-side size >= min_order_size.

    Read-only (Gamma + CLOB GET). Every candidate it looks at is recorded with its verdict, so
    a refusal is auditable — never a silent skip. With no qualifying bucket the reason is
    ``no_tradeable_bucket`` plus the rejected list.
    """
    attempts: list[dict] = []
    rejected: list[dict] = []

    if token_id:                                   # operator pinned a token: assess, don't choose
        book = sign_dryrun._book_from_summary(client.get_order_book(token_id))
        assessment = assess_book(book, cap=cap)
        entry = {"key": f"token:{token_id}", "bucket": None, "token_id": str(token_id),
                 "status": assessment["reason"], "detail": assessment["detail"],
                 "best_bid": assessment["best_bid"], "best_ask": assessment["best_ask"]}
        attempts.append(entry)
        if not assessment["ok"]:
            rejected.append(entry)
            return {"ok": False, "reason": NO_TRADEABLE_BUCKET, "detail": assessment["detail"],
                    "attempts": attempts, "rejected": rejected, "selection": None}
        return {"ok": True, "reason": BUCKET_OK,
                "market": {"city": city, "local_date": local_date, "direction": direction,
                           "bucket": None, "title": "explicit --token-id", "volume": None,
                           "token_id": str(token_id)},
                "book": book, "attempts": attempts, "rejected": rejected,
                "selection": {"rule": "explicit --token-id", "assessment": assessment}}

    sessions = sign_dryrun.candidate_sessions(state_path, limit=limit or sign_dryrun.MAX_DISCOVERY_ATTEMPTS)
    if direction:
        sessions = [{**row, "direction": direction} for row in sessions]
    if city:
        sessions = [row for row in sessions if row["city"] == city]
    if local_date:
        sessions = [row for row in sessions if row["local_date"] == local_date]

    for session in sessions[:MAX_SELECTION_SESSIONS]:
        key = f"{session['city']}|{session['local_date']}|{session['direction']}"
        try:
            rows = _gamma_candidates(client, session, cap=cap, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - try the next session
            attempts.append({"key": key, "status": f"gamma_error:{type(exc).__name__}"})
            continue
        if not rows:
            attempts.append({"key": key, "status": "no_accepting_markets"})
            continue

        session_candidates: list[dict] = []
        for row in rows[:max_candidates]:
            entry = {"key": key, "bucket": row["market"].get("groupItemTitle"),
                     "token_id": row["token_id"], "volume": row["volume"],
                     "best_bid": None, "best_ask": None, "ask_size_near": None,
                     "detail": "", "status": None}
            try:
                book = sign_dryrun._book_from_summary(client.get_order_book(row["token_id"]))
            except Exception as exc:  # noqa: BLE001 - record and move on
                attempts.append({**entry, "status": f"book_error:{type(exc).__name__}"})
                rejected.append({**entry, "status": f"book_error:{type(exc).__name__}"})
                continue
            assessment = assess_book(book, cap=cap)
            entry.update({"best_bid": assessment["best_bid"], "best_ask": assessment["best_ask"],
                          "ask_size_near": assessment["ask_size_near"],
                          "detail": assessment["detail"], "status": assessment["reason"]})
            attempts.append(entry)
            if not assessment["ok"]:
                rejected.append(entry)
                continue
            session_candidates.append({
                "volume": row["volume"], "assessment": assessment, "book": book,
                "market": {"city": session["city"], "local_date": session["local_date"],
                           "direction": session["direction"],
                           "bucket": row["market"].get("groupItemTitle"),
                           "title": row["market"].get("question"),
                           "event_slug": None, "volume": row["volume"],
                           "token_id": row["token_id"],
                           "no_token_id": None},
            })
        chosen = choose_bucket(session_candidates)
        if chosen is not None:
            return {"ok": True, "reason": BUCKET_OK, "market": chosen["market"], "book": chosen["book"],
                    "attempts": attempts, "rejected": rejected,
                    "selection": {"rule": "min |best_ask - 0.5| among two-sided buckets (tie: volume)",
                                  "assessed": len(session_candidates), "key": key,
                                  "assessment": {k: v for k, v in chosen["assessment"].items() if k != "cap"}}}

    return {"ok": False, "reason": NO_TRADEABLE_BUCKET,
            "detail": f"no tradable bucket among {len(attempts)} candidate(s)",
            "attempts": attempts, "rejected": rejected, "selection": None}


def run_smoke(*, enable_submit: bool = False, confirm: str | None = None,
              budget_usdc: str = DEFAULT_BUDGET_USDC, env: dict | None = None,
              city=None, local_date=None, direction=None, token_id=None,
              price_override=None, timeout: int = 25, sleep=None, attempts: int = POLL_ATTEMPTS,
              reconcile_fn=None, readonly: bool = False, leg: str = "buy_yes",
              open_orders_attempts: int = OPEN_ORDERS_ATTEMPTS,
              open_orders_sleep: float = OPEN_ORDERS_SLEEP_SECONDS) -> dict:
    """Run the smoke loop. Never raises — every failure is logged and fail-closed."""
    env = env if env is not None else creds_mod.load_env_file()
    reconcile_fn = reconcile_fn or reconcile.collect
    report = _report_skeleton(env, budget=budget_usdc, scenario=False)
    report["readonly"] = readonly
    actor = "live/smoke.py"

    def step(action: str, reason=None, params=None, response_summary=None, order_id=None,
             write_audit: bool = True) -> None:
        record = {"ts_utc": _now_iso(), "actor": actor, "action": action, "reason": reason,
                  "params": params, "response_summary": response_summary, "order_id": order_id}
        report["steps"].append({key: record[key] for key in
                                ("ts_utc", "action", "reason", "order_id")})
        if write_audit:
            # submit/cancel audit themselves inside live/submit.py (single source of truth);
            # everything else is logged here
            submit.audit(record)

    gates = submit.gate_status(enable_submit=enable_submit, env=env, confirm=confirm)
    report["gates"] = gates
    if readonly:
        # read-only preview: the write path is never reached, so the gates are informational
        # (and the submit step below is skipped before any client write call would happen)
        step("readonly", "gates_skipped", params={"checks": gates["checks"]},
             response_summary={"reason": gates["reason"]})
    elif not gates["ok"]:
        step("gate_deny", gates["reason"], params={"intent": "smoke",
                                                   "checks": gates["checks"]})
        report["reason"] = gates["reason"]
        report["exit_code"] = 3
        return report

    order_id = None
    client = None
    try:
        submit.prepare_network(env)          # .env proxy + forced IPv4, before any client
        credentials = creds_mod.validate_creds(env)
        caps = order_plan.load_caps()
        cap = order_plan.cap_for(leg, caps)
        step("intent", "smoke_order", params={"budget_usdc": budget_usdc, "leg": leg,
                                              "caps": {k: str(v) for k, v in caps.items()}})

        client = sign_dryrun._build_client(credentials)
        sentinel = submit.arm_controlled_sentinels(client)
        report["sentinel"] = {key: sentinel[key] for key in
                              ("armed", "released", "still_blocked_count", "statement")}
        step("sentinel_armed", "ok", params={"released": sentinel["released"]},
             response_summary={"still_blocked": sentinel["still_blocked_count"]})

        # ``direction`` here is the *market* direction (high/low), never the leg. Selection
        # requires a two-sided, near-price bucket — dead/thin buckets are rejected loudly.
        found = select_tradeable_bucket(client, city=city, local_date=local_date,
                                        direction=direction, token_id=token_id, cap=cap,
                                        timeout=timeout)
        report["bucket_attempts"] = found["attempts"]
        report["bucket_rejected"] = found["rejected"]
        report["selection"] = found.get("selection")
        if not found["ok"]:
            report["reason"] = "bucket:no_tradeable_bucket"
            report["exit_code"] = 2
            step("deny", report["reason"], params={"candidates": len(found["attempts"])},
                 response_summary={"detail": found.get("detail"),
                                   "rejected": [{k: row.get(k) for k in
                                                 ("key", "bucket", "best_bid", "best_ask", "status")}
                                                for row in found["rejected"][:8]]})
            return report
        market, book = found["market"], found["book"]
        report["market"], report["book"] = dict(market), dict(book)
        step("discover", "ok", params={"city": market.get("city"), "direction": market.get("direction")},
             response_summary={"bucket": market.get("bucket"), "best_ask": book.get("best_ask"),
                               "best_bid": book.get("best_bid"),
                               "passive_price": ((found.get("selection") or {}).get("assessment")
                                                 or {}).get("passive_price")})

        # ① risk gate on live numbers (config fire budget — does not need the plan)
        pre = _preflight_with(reconcile_fn, env)
        report["snapshot"] = {key: pre["snapshot"].get(key) for key in
                              ("usdc_balance", "open_orders", "positions_value_usdc",
                               "positions_raw_count")} if pre.get("snapshot") else None
        report["risk_gate"] = pre.get("risk_gate")
        report["limits"] = pre.get("limits")
        step("risk", "ok" if pre.get("ok") else "deny", params={"stage": "risk_gate"},
             response_summary=pre.get("risk_gate") or {"reason": pre.get("reason")})
        if not pre.get("ok"):
            report["reason"] = f"preflight:{pre.get('reason')}"
            report["exit_code"] = 2
            return report
        if not (pre["risk_gate"] or {}).get("allow"):
            report["reason"] = f"risk_gate:{(pre['risk_gate'] or {}).get('reason')}"
            step("deny", report["reason"], response_summary=pre["risk_gate"])
            report["exit_code"] = 2
            return report

        # ② plan (passive price + order_plan size math)
        built = build_smoke_plan(book=book, budget_usdc=budget_usdc,
                                 token_id=market.get("token_id"), direction=leg, caps=caps,
                                 price_override=price_override)
        plan = built["plan"]
        report["plan"] = _plan_json(plan)
        report["price_choice"] = built["price_choice"]
        if not plan or not plan.get("ok"):
            reason = f"order_plan:{(plan or {}).get('reason') or built['reason']}"
            step("deny", reason, params={"budget_usdc": budget_usdc},
                 response_summary={"detail": (plan or {}).get("detail") or built.get("detail")})
            report["reason"] = reason
            report["exit_code"] = 2
            return report
        step("plan", "ok", params={"token_id": plan["token_id"], "price": str(plan["price"]),
                                   "size": str(plan["size"]),
                                   "max_cost_usdc": str(plan["max_cost_usdc"])})

        # ③ per-order + cumulative limits
        limits_check = submit.check_limits(
            notional_usdc=plan["max_cost_usdc"],
            fire_budget_usdc=(pre["limits"] or {}).get("fire_budget_usdc"),
            committed_usdc=pre["snapshot"].get("positions_value_usdc") or 0,
            max_capital_usdc=(pre["limits"] or {}).get("max_capital_usdc"),
        )
        report["limits_check"] = limits_check
        step("limits", limits_check["reason"], response_summary={"detail": limits_check["detail"]})
        if not limits_check["ok"]:
            report["reason"] = f"limits:{limits_check['reason']}"
            report["exit_code"] = 2
            return report

        # ④ never marketable
        passive = submit.check_non_marketable(side=plan["side"], price=plan["price"], book=book)
        report["non_marketable"] = passive
        step("non_marketable", passive["reason"], response_summary={"detail": passive["detail"]})
        if not passive["ok"]:
            report["reason"] = f"non_marketable:{passive['reason']}"
            report["exit_code"] = 2
            return report

        if readonly:
            report["ok"] = True
            report["reason"] = "readonly_stop_before_submit"
            report["exit_code"] = 0
            step("preflight_ok", "readonly_stop_before_submit",
                 params={"token_id": plan["token_id"], "price": str(plan["price"])},
                 response_summary={"would_submit": plan["max_cost_usdc"]})
            return report

        # ⑤ submit once (post_only GTC)
        try:
            result = submit.submit_order(
                client, token_id=plan["token_id"], price=plan["price"], size=plan["size"],
                side=plan["side"], tick=str(plan["tick"]), gates=gates,
                neg_risk=bool(book.get("neg_risk")), post_only=True,
                # a smoke order must REST so the place→query→cancel loop is really exercised
                take_down_unfilled=False)
        except Exception as exc:  # noqa: BLE001 - may or may not have landed ⇒ residual risk
            detail = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
            step("exception", "submit_failed", params={"token_id": plan["token_id"],
                                                       "price": str(plan["price"])},
                 response_summary=detail)
            report["residual_risk"] = True
            report["reason"] = f"submit_failed:{detail}"
            report["exit_code"] = 2
            rescue = rescue_cancel(client, order_id=None, token_id=plan["token_id"],
                                   price=str(plan["price"]))
            report["rescue"] = rescue
            step("rescue", "best_effort_cancel", response_summary=rescue)
            report["order"]["submitted"] = bool(rescue.get("canceled")) or bool(rescue.get("matched_open_orders"))
            return report

        order_id = result["order_id"]
        report["order"].update({"order_id": order_id, "submitted": True,
                                "response_summary": result["response_summary"]})
        filled = result.get("filled_shares") or Decimal(0)
        if filled > Decimal(0):
            # a passive order must never fill: surface the real size immediately
            report["order"]["unexpected_fill"] = str(filled)
            report["reason"] = f"unexpected_fill:size_matched={filled}"
            report["exit_code"] = 2
        step("submit", "ok" if order_id else "no_order_id", write_audit=False,
             params={"token_id": plan["token_id"], "price": str(plan["price"]),
                     "size": str(plan["size"]), "post_only": True},
             response_summary=result["response_summary"], order_id=order_id)
        if not order_id:
            report["residual_risk"] = True
            report["reason"] = "submit_response_without_order_id"
            report["exit_code"] = 2
            return report

        # ⑥ confirm resting
        confirmed = verify_order(client, order_id, attempts=attempts, sleep=sleep)
        report["order"]["confirmed"] = confirmed
        step("query", confirmed["reason"], response_summary=confirmed.get("last"),
             order_id=order_id)
        unexpected_fill = confirmed["reason"] == "order_partially_filled"
        if unexpected_fill:
            # the smoke order was supposed to rest, never fill — a fill is a *result*, not
            # residual risk, and it must be reported loudly (never silently hidden)
            report["order"]["unexpected_fill"] = confirmed.get("size_matched")
            report["reason"] = f"unexpected_fill:size_matched={confirmed.get('size_matched')}"
            report["exit_code"] = 2
        elif not confirmed["ok"]:
            report["reason"] = f"verify:{confirmed['reason']}"
            report["exit_code"] = 2
            report["residual_risk"] = True

        # ⑦ cancel (also clears the remainder if a fill left size resting on the book)
        rests = str(confirmed.get("status") or "").lower() in CONFIRMED_STATUSES
        if confirmed["ok"] or (unexpected_fill and rests):
            cancelled = submit.cancel_order(client, order_id)
            report["order"]["cancelled"] = cancelled["response_summary"]
            step("cancel", "ok" if cancelled["ok"] else "cancel_not_confirmed", write_audit=False,
                 response_summary=cancelled["response_summary"], order_id=order_id)
            after = verify_order(client, order_id, attempts=attempts, sleep=sleep,
                                 statuses=CANCELLED_STATUSES)
            report["order"]["status_after_cancel"] = (after.get("last") or {}).get("status")
            step("query", after["reason"], response_summary=after.get("last"), order_id=order_id)
            if not cancelled["ok"] or not after["ok"]:
                report["reason"] = report["reason"] or "cancel_not_confirmed"
                report["exit_code"] = 2
                report["residual_risk"] = True
            elif not unexpected_fill:
                report["ok"] = True
                report["exit_code"] = 0
                report["reason"] = None
            elif not report["reason"].startswith("unexpected_fill"):
                report["reason"] = report["reason"] or "unexpected_fill"

        # ⑧ no open orders may remain — a confirmed cancel can lag the list endpoint, so re-read
        #    it a bounded number of times instead of crying "residual risk" on one stale page
        cancel_confirmed = bool((report["order"].get("confirmed") or {}).get("ok")
                                and report["order"].get("status_after_cancel") in CANCELLED_STATUSES)
        check = await_no_open_orders(client, order_id, cancel_confirmed=cancel_confirmed,
                                     attempts=open_orders_attempts,
                                     sleep_seconds=open_orders_sleep, sleep=sleep)
        report["open_orders_check"] = check
        step("open_orders", check["status"],
             response_summary={k: check[k] for k in ("status", "attempts", "remaining_ids", "note")},
             order_id=order_id)
        final = reconcile_fn(env)
        report["reconcile"] = {key: final.get(key) for key in
                               ("ok", "open_orders", "usdc_balance", "positions_value_usdc")}
        step("reconcile", "ok" if final.get("open_orders") == 0 else "open_orders_remain",
             response_summary=report["reconcile"])
        if not check["ok"]:
            report["ok"] = False
            report["reason"] = check["status"]
            report["exit_code"] = 2
            report["residual_risk"] = True
        elif final.get("open_orders"):
            # the id list said "clear" but the snapshot still counts orders: trust the ids, note it
            report["open_orders_check"]["snapshot_mismatch"] = final.get("open_orders")
            if report["ok"]:
                report["ok"] = True
                step("complete", "ok", response_summary={"order_id": order_id,
                                                         "snapshot_open_orders": final.get("open_orders")})
            return report
        elif report["ok"]:
            step("complete", "ok", response_summary={"order_id": order_id})
        return report
    except Exception as exc:  # fail closed; try to clean up anything we may have placed
        detail = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
        step("exception", type(exc).__name__, response_summary=detail, order_id=order_id)
        report["reason"] = detail
        report["exit_code"] = 2
        try:
            plan = report.get("plan") or {}
            scoped = bool(order_id) or plan.get("token_id") is not None
            if readonly:
                # a read-only run never placed anything: nothing to clean up
                report["residual_risk"] = False
                step("rescue", "skipped_readonly",
                     response_summary={"reason": "read-only run: no order was ever placed"})
            elif client is None:
                report["residual_risk"] = bool(order_id)
            elif not scoped:
                # no order id and no plan scope ⇒ no submit was ever attempted
                report["residual_risk"] = False
                step("rescue", "skipped_no_scope",
                     response_summary={"reason": "no order_id and no plan scope — no blind cancel"})
            else:
                rescue = rescue_cancel(client, order_id=order_id,
                                       token_id=plan.get("token_id"), price=plan.get("price"))
                report["rescue"] = rescue
                report["residual_risk"] = not rescue.get("ok")
                step("rescue", "best_effort_cancel", response_summary=rescue, order_id=order_id)
        except Exception as rescue_exc:  # noqa: BLE001
            report["residual_risk"] = True
            step("rescue", "rescue_failed", response_summary=str(rescue_exc)[:200], order_id=order_id)
        return report


def _preflight_with(reconcile_fn, env: dict) -> dict:
    """Preflight using an injected collector (tests pass a fake, no network)."""
    snapshot = reconcile_fn(env)
    if not snapshot.get("ok"):
        return {"ok": False, "reason": f"reconcile:{snapshot.get('reason')}", "snapshot": snapshot}
    limits = _limits_from_env(env)
    gate = risk_gate.evaluate(
        usdc_balance=snapshot.get("usdc_balance"),
        open_positions=len(snapshot.get("positions") or []),
        committed_usdc=snapshot.get("positions_value_usdc") or 0,
        fire_budget_usdc=limits.get("fire_budget_usdc"),
        max_open_positions=limits.get("max_open_positions"),
        max_capital_usdc=limits.get("max_capital_usdc"),
    )
    return {"ok": True, "snapshot": snapshot, "limits": limits, "risk_gate": gate}


def _plan_json(plan: dict | None) -> dict | None:
    if not plan:
        return None
    return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in plan.items()}


# --------------------------------------------------------------------------- dry plan

def dry_plan(*, book_path=None, budget_usdc: str = DEFAULT_BUDGET_USDC,
             leg: str = "buy_yes", token_id: str | None = None) -> dict:
    """Build the smoke plan with no network, no client and no write path."""
    book = dict(DRY_BOOK)
    if book_path:
        book = json.loads(Path(book_path).read_text(encoding="utf-8"))
    caps = order_plan.load_caps()
    built = build_smoke_plan(book=book, budget_usdc=budget_usdc,
                             token_id=token_id or DRY_TOKEN_ID, direction=leg, caps=caps)
    report = {
        "ok": bool(built["ok"]), "reason": built["reason"], "detail": built["detail"],
        "ts_utc": _now_iso(), "phase": "phase3-smoke", "scenario": True, "dry_plan": True,
        "network_calls": 0, "budget_usdc": budget_usdc,
        "book": book, "price_choice": built["price_choice"], "plan": _plan_json(built["plan"]),
        "submit": {"attempted": False, "statement": "dry plan only — no client, no submit path"},
    }
    report["exit_code"] = 0 if report["ok"] else 2
    return report


def human_summary(report: dict) -> str:
    plan = report.get("plan") or {}
    order = report.get("order") or {}
    lines = [
        "=== Phase-3 smoke order ===",
        f"ok={report.get('ok')} reason={report.get('reason')} dry_plan={report.get('dry_plan', False)} "
        f"ts={report.get('ts_utc')}",
    ]
    price_choice = report.get("price_choice") or {}
    if price_choice:
        lines.append(f"price: {price_choice.get('price')} ({price_choice.get('detail')})")
    if plan:
        lines.append(f"plan: {plan.get('side')} {plan.get('direction')} price={plan.get('price')} "
                     f"size={plan.get('size')} notional={plan.get('max_cost_usdc')} USDC "
                     f"tick={plan.get('tick')} reason={plan.get('reason')}")
    selection = (report.get("selection") or {}).get("assessment") or {}
    if selection.get("ok"):
        lines.append(f"bucket: two-sided bid={selection.get('best_bid')} ask={selection.get('best_ask')} "
                     f"near-ask size={selection.get('ask_size_near')} passive={selection.get('passive_price')}")
    if report.get("bucket_rejected"):
        lines.append(f"bucket rejects: {len(report['bucket_rejected'])} "
                     + ", ".join(f"{row.get('bucket')}({row.get('status')})"
                                 for row in report["bucket_rejected"][:6]))
    if report.get("sentinel"):
        lines.append(f"sentinel: released={report['sentinel'].get('released')} "
                     f"still_blocked={report['sentinel'].get('still_blocked_count')}")
    if report.get("risk_gate"):
        lines.append(f"risk_gate: allow={report['risk_gate'].get('allow')} "
                     f"reason={report['risk_gate'].get('reason')}")
    if report.get("limits_check"):
        lines.append(f"limits: {report['limits_check'].get('reason')} — {report['limits_check'].get('detail')}")
    if report.get("non_marketable"):
        lines.append(f"non_marketable: {report['non_marketable'].get('reason')} — "
                     f"{report['non_marketable'].get('detail')}")
    if order.get("order_id"):
        lines.append(f"order: id={order['order_id']} confirmed={(order.get('confirmed') or {}).get('status')} "
                     f"after_cancel={order.get('status_after_cancel')}")
    check = report.get("open_orders_check") or {}
    if check:
        lines.append(f"open_orders check: {check.get('status')} "
                     f"(attempts={check.get('attempts')}"
                     + (f", lag={check['note']}" if check.get("note") else "")
                     + (f", remaining={check.get('remaining_ids')}" if check.get("remaining_ids") else "")
                     + ")")
    if report.get("reconcile"):
        lines.append(f"reconcile: open_orders={report['reconcile'].get('open_orders')}")
    if report.get("residual_risk"):
        lines.append("RESIDUAL RISK: an order may still be resting — check "
                     "`live/submit.py --open-orders` and cancel by hand")
    if report.get("order", {}).get("unexpected_fill"):
        lines.append(f"UNEXPECTED FILL size_matched={report['order']['unexpected_fill']} — "
                     "the passive order filled; report it, do not treat as a clean smoke run")
    elif report.get("dry_plan"):
        lines.append("submit: not attempted (dry plan — no client, no write path)")
    elif report.get("order", {}).get("submitted"):
        lines.append("submit: ATTEMPTED exactly once (post_only GTC) — see order above")
    else:
        lines.append("submit: not attempted (refused or failed before the submit step)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-3 smoke order (5 USDC passive limit BUY)")
    parser.add_argument("--enable-submit", action="store_true")
    parser.add_argument("--confirm", help="must equal `live/submit.py --phrase` today")
    parser.add_argument("--dry-plan", action="store_true",
                        help="build+print the plan from a synthetic book (no network, no submit)")
    parser.add_argument("--readonly-preflight", action="store_true",
                        help="run the live chain (discover/risk/plan/limits/passivity) and stop "
                             "before the write — read-only network, cannot submit")
    parser.add_argument("--book", help="book JSON for --dry-plan")
    parser.add_argument("--budget-usdc", default=DEFAULT_BUDGET_USDC)
    parser.add_argument("--city")
    parser.add_argument("--date")
    parser.add_argument("--direction", help="market direction for discovery: high / low")
    parser.add_argument("--leg", default="buy_yes", help="leg to plan: buy_yes (default) / buy_no")
    parser.add_argument("--token-id")
    parser.add_argument("--price")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--open-orders-attempts", type=int, default=OPEN_ORDERS_ATTEMPTS,
                        help="re-reads of the open-order list after a confirmed cancel")
    parser.add_argument("--open-orders-interval", type=float, default=OPEN_ORDERS_SLEEP_SECONDS,
                        help="seconds between those re-reads")
    args = parser.parse_args(argv)

    if args.dry_plan:
        report = dry_plan(book_path=args.book, budget_usdc=args.budget_usdc,
                          leg=args.leg)
        try:
            submit.audit({"actor": "live/smoke.py", "action": "plan", "reason": report["reason"],
                          "params": {"dry": True, "budget_usdc": args.budget_usdc},
                          "response_summary": {"size": (report.get("plan") or {}).get("size"),
                                               "price": (report.get("plan") or {}).get("price")}})
        except submit.AuditError as exc:  # a dry plan is not worth failing over a log hiccup
            print(f"WARNING: audit log unavailable: {exc}")
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else human_summary(report))
        return report["exit_code"]

    report = run_smoke(enable_submit=args.enable_submit, confirm=args.confirm,
                       budget_usdc=args.budget_usdc, city=args.city, local_date=args.date,
                       direction=args.direction, token_id=args.token_id, leg=args.leg,
                       price_override=args.price, timeout=args.timeout,
                       readonly=args.readonly_preflight,
                       open_orders_attempts=args.open_orders_attempts,
                       open_orders_sleep=args.open_orders_interval)
    try:
        out_path = Path(args.out)
        if out_path.parent != Path(""):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        wrote = f"wrote {out_path}"
    except OSError as exc:
        wrote = f"could not write {args.out}: {exc}"
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str) if args.json
          else human_summary(report) + "\n" + wrote)
    return report.get("exit_code", 2)


if __name__ == "__main__":
    sys.exit(main())
