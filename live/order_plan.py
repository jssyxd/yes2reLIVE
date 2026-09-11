#!/usr/bin/env python3
"""Pure order-plan math: leg intent + book + budget + caps → signable parameters.

stdlib only, no I/O, no third-party import (reads ``config/yes2re_reversal.json``
read-only for the strategy price caps). Fail-closed: any missing/illegal input
returns ``ok: false`` with a machine-readable ``reason``.

Semantics mirror the paper engine's fire path (``re_execution.py``): never pay
above ``price_cap``, align down to the market tick, never spend more than the
budget, and never send an order below the market's minimum size.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "yes2re_reversal.json"

#: decision codes
OK = "ok"
INVALID_INPUT = "invalid_input"
NO_BOOK = "no_book"
PRICE_ABOVE_CAP = "price_above_cap"
BELOW_MIN_ORDER_SIZE = "below_min_order_size"
INSUFFICIENT_BUDGET = "insufficient_budget"

DEFAULT_TICK = Decimal("0.01")
#: absurd-budget guard: above this a value is a typo/config error, never a real order
MAX_BUDGET_USDC = Decimal("1e12")
BUY_DIRECTIONS = ("buy_yes", "buy_no")
SELL_DIRECTION = "sell"
DIRECTIONS = BUY_DIRECTIONS + (SELL_DIRECTION,)
CAP_KEYS = ("no_max_ask", "yes_max_ask")


class CapsError(ValueError):
    """Strategy price caps are missing/malformed — the caller must fail closed."""


def _dec(value, field: str, *, allow_zero: bool = False) -> Decimal:
    """Parse a decimal input. Raises ValueError for anything not a positive number."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}: missing or non-numeric")
    if isinstance(value, Decimal):
        out = value
    else:
        try:
            out = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError, ValueError):
            raise ValueError(f"{field}: not a number") from None
    if not out.is_finite():
        raise ValueError(f"{field}: not finite")
    if out < 0 or (out == 0 and not allow_zero):
        raise ValueError(f"{field}: must be > 0")
    return out


def align_down(value: Decimal, tick: Decimal) -> Decimal:
    """Round ``value`` down to the nearest multiple of ``tick`` (never pay up)."""
    if tick <= 0:
        raise ValueError("tick: must be > 0")
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _book_price(book: Any, side: str) -> Decimal | None:
    """Best ask (BUY) / best bid (SELL) from a book mapping or None."""
    if not isinstance(book, dict):
        return None
    key = "best_ask" if side == "BUY" else "best_bid"
    raw = book.get(key)
    if raw is None:
        levels = book.get("asks" if side == "BUY" else "bids") or []
        if levels:
            row = levels[0]
            raw = row.get("price") if isinstance(row, dict) else row
    if raw is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def load_caps(config_path: str | Path | None = None) -> dict[str, Decimal]:
    """Read the strategy price caps from config (read-only; never written back).

    Fail closed: a missing/invalid cap raises :class:`CapsError` instead of silently
    defaulting to "no limit" — a cap that cannot be read must never become a looser cap.
    """
    path = Path(config_path) if config_path else CONFIG_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    strategy = raw.get("strategy") or {}
    caps: dict[str, Decimal] = {}
    for key in CAP_KEYS:
        value = strategy.get(key)
        if value is None or str(value).strip() == "":
            raise CapsError(f"strategy.{key} missing from {path}")
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise CapsError(f"strategy.{key} is not a number") from None
        if not parsed.is_finite() or not 0 < parsed <= 1:
            raise CapsError(f"strategy.{key} out of range (0, 1]")
        caps[key] = parsed
    return caps


def cap_for(direction: str, caps: dict, override=None) -> Decimal | None:
    """``buy_no`` → no_max_ask, ``buy_yes`` → yes_max_ask, ``sell`` → none."""
    if override is not None:
        try:
            return _dec(override, "price_cap")
        except ValueError:
            return None
    if direction == SELL_DIRECTION:
        return None
    key = "yes_max_ask" if direction == "buy_yes" else "no_max_ask"
    value = (caps or {}).get(key)
    return value if isinstance(value, Decimal) else None


def plan_order(
    *,
    direction: str,
    token_id: str | None,
    book: Any,
    budget_usdc,
    price_cap,
    min_order_size=None,
    tick_size=None,
    size_decimals: int = 2,
) -> dict:
    """Fold one leg intent into signable order parameters.

    Args:
        direction: ``buy_yes`` / ``buy_no`` (priced off the best ask, capped) or
            ``sell`` (priced off the best bid; the cap does not bind — no floor yet).
        token_id: CLOB ERC-1155 token id of the asset being bought/sold.
        book: ``{"best_ask", "best_bid", "tick_size", "min_order_size", ...}``
            (ask/bid level rows are accepted as a fallback).
        budget_usdc: notional to deploy (BUY: USDC to spend; SELL: notional to unwind).
        price_cap: hard limit on the price we are willing to pay (BUY legs).
        min_order_size: market minimum order size in shares (``orderMinSize``).
        tick_size: market tick; falls back to the book's ``tick_size``.
        size_decimals: share decimals to round down to (2 default, 6 for fine ticks).

    Returns:
        ``{"ok", "reason", "detail", "side", "price", "size", "max_cost_usdc", "tick"}``
        — numeric fields are ``None`` on deny.
    """
    deny = {
        "ok": False,
        "detail": "",
        "side": None,
        "price": None,
        "size": None,
        "max_cost_usdc": None,
        "tick": None,
    }

    if not isinstance(direction, str) or direction not in DIRECTIONS:
        return {**deny, "reason": INVALID_INPUT, "detail": f"direction: want one of {', '.join(DIRECTIONS)}"}
    if not isinstance(token_id, str) or not token_id.strip():
        return {**deny, "reason": INVALID_INPUT, "detail": "token_id: missing"}
    if not isinstance(size_decimals, int) or isinstance(size_decimals, bool) or not 0 <= size_decimals <= 6:
        return {**deny, "reason": INVALID_INPUT, "detail": "size_decimals: want int in 0..6"}

    side = "SELL" if direction == SELL_DIRECTION else "BUY"
    deny["side"] = side

    if side == "BUY":
        try:
            cap = _dec(price_cap, "price_cap")
        except ValueError as exc:
            return {**deny, "reason": INVALID_INPUT, "detail": str(exc)}
        if cap > 1:
            return {**deny, "reason": INVALID_INPUT, "detail": "price_cap: must be <= 1"}
    else:
        cap = None

    best = _book_price(book, side)
    if best is None:
        return {**deny, "reason": NO_BOOK, "detail": f"no resting {'ask' if side == 'BUY' else 'bid'}"}
    if cap is not None and best > cap:
        return {**deny, "reason": PRICE_ABOVE_CAP,
                "detail": f"best_ask {best} > cap {cap}"}

    book_tick = book.get("tick_size") if isinstance(book, dict) else None
    book_min = book.get("min_order_size") if isinstance(book, dict) else None
    raw_min = min_order_size if min_order_size is not None else book_min
    if raw_min is None:
        return {**deny, "reason": INVALID_INPUT, "detail": "min_order_size: missing"}
    try:
        budget = _dec(budget_usdc, "budget_usdc")
        raw_tick = tick_size if tick_size is not None else book_tick
        tick = _dec(raw_tick, "tick_size") if raw_tick is not None else DEFAULT_TICK
        minimum = _dec(raw_min, "min_order_size", allow_zero=True)
    except ValueError as exc:
        return {**deny, "reason": INVALID_INPUT, "detail": str(exc)}
    if budget > MAX_BUDGET_USDC:
        return {**deny, "reason": INVALID_INPUT,
                "detail": f"budget_usdc {budget} exceeds {MAX_BUDGET_USDC}"}

    price = align_down(min(best, cap) if cap is not None else best, tick)
    if price <= 0:
        return {**deny, "reason": INVALID_INPUT, "detail": f"price aligned to 0 at tick {tick}"}
    if cap is not None and price > cap:
        return {**deny, "reason": PRICE_ABOVE_CAP, "detail": f"aligned price {price} > cap {cap}"}

    quantum = Decimal(1).scaleb(-size_decimals)
    try:
        size = (budget / price).quantize(quantum, rounding=ROUND_DOWN)
    except InvalidOperation:  # quantize overflows the context precision for absurd budgets
        return {**deny, "reason": INVALID_INPUT,
                "detail": f"budget {budget}: too large for tick quantum {quantum}"}
    if size <= 0:
        return {**deny, "reason": INSUFFICIENT_BUDGET,
                "detail": f"budget {budget} < one tick-rounded share at {price}"}
    if minimum > 0 and size < minimum:
        return {**deny, "reason": BELOW_MIN_ORDER_SIZE,
                "detail": f"size {size} < min_order_size {minimum}"}

    try:
        max_cost = (size * price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    except InvalidOperation:
        return {**deny, "reason": INVALID_INPUT, "detail": "size x price: too large to quantize"}

    return {
        "ok": True,
        "reason": OK,
        "detail": "",
        "side": side,
        "price": price,
        "size": size,
        "max_cost_usdc": max_cost,
        "tick": tick,
        "budget_usdc": budget,
        "price_cap": cap,
        "min_order_size": minimum,
        "token_id": token_id,
        "direction": direction,
    }


def _demo() -> None:
    book = {"best_ask": "0.523", "best_bid": "0.517", "tick_size": "0.01", "min_order_size": "5"}
    planned = plan_order(direction="buy_yes", token_id="T", book=book, budget_usdc="3", price_cap="0.9")
    assert planned["ok"] and planned["price"] == Decimal("0.52") and planned["size"] == Decimal("5.76"), planned
    assert planned["max_cost_usdc"] <= Decimal("3"), planned
    assert plan_order(direction="buy_yes", token_id="T", book=book,
                      budget_usdc="2", price_cap="0.9")["reason"] == BELOW_MIN_ORDER_SIZE
    assert plan_order(direction="buy_yes", token_id="T", book={**book, "best_ask": "0.95"},
                      budget_usdc="2", price_cap="0.9")["reason"] == PRICE_ABOVE_CAP
    assert plan_order(direction="buy_yes", token_id="T", book={**book, "best_ask": "0.9"},
                      budget_usdc="1", price_cap="0.9")["reason"] == BELOW_MIN_ORDER_SIZE
    assert plan_order(direction="buy_yes", token_id="T", book={},
                      budget_usdc="2", price_cap="0.9")["reason"] == NO_BOOK
    assert plan_order(direction="buy_no", token_id=None, book=book,
                      budget_usdc="2", price_cap="1.0")["reason"] == INVALID_INPUT
    assert plan_order(direction="sell", token_id="T", book=book,
                      budget_usdc="3", price_cap=None)["price"] == Decimal("0.51")
    assert plan_order(direction="buy_yes", token_id="T", book=book, budget_usdc="0",
                      price_cap="0.9")["reason"] == INVALID_INPUT
    assert plan_order(direction="buy_yes", token_id="T", book={**book, "tick_size": "0.001"},
                      budget_usdc="3", price_cap="0.9")["price"] == Decimal("0.523")
    print("order_plan demo OK")


if __name__ == "__main__":
    _demo()
