"""Normalize REST/WS book snapshots into the shared :class:`BookView`.

This is the only place that knows the concrete shapes of
``clob_market_data.BookSnapshot`` (REST), ``local_order_book.LocalBookSnapshot``
(WebSocket local book) and plain dicts (tests / internal consumers). Everything
downstream (``RiskGate``, ``paper_executor``) sees only ``BookView``.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from execution.market import BookView


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    return d if d.is_finite() and d >= 0 else None


def _levels(value: Any, *, ascending: bool) -> tuple[tuple[Decimal, Decimal], ...]:
    """Parse level lists/dicts into (price, size); sort asks ascending, bids descending."""
    if not isinstance(value, (list, tuple)):
        return ()
    parsed: list[tuple[Decimal, Decimal]] = []
    for level in value:
        if not isinstance(level, dict):
            continue
        price = _dec(level.get("price"))
        size = _dec(level.get("size"))
        if price is not None and size is not None and size > 0:
            parsed.append((price, size))
    parsed.sort(key=lambda row: row[0], reverse=not ascending)
    return tuple(parsed)


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def from_any(book: Any, *, token_id: str | None = None) -> BookView | None:
    """Build a BookView from a BookSnapshot, LocalBookSnapshot, or dict."""
    if book is None:
        return None
    tid = str(token_id or _field(book, "token_id") or _field(book, "asset_id") or "")
    asks = _levels(_field(book, "asks"), ascending=True)
    bids = _levels(_field(book, "bids"), ascending=False)
    best_ask = _dec(_field(book, "best_ask")) or (asks[0][0] if asks else None)
    best_bid = _dec(_field(book, "best_bid")) or (bids[0][0] if bids else None)

    fetched = _field(book, "fetched_at_epoch")
    if fetched is None:
        fetched = _field(book, "received_at_epoch")
    age = 0.0
    if isinstance(fetched, (int, float)) and fetched > 0:
        age = max(0.0, time.time() - float(fetched))

    return BookView(
        token_id=tid,
        asks=asks,
        bids=bids,
        best_ask=best_ask,
        best_bid=best_bid,
        age_seconds=age,
        book_hash=_field(book, "book_hash") or _field(book, "hash"),
        timestamp=_field(book, "timestamp") or _field(book, "exchange_timestamp"),
        tick_size=_dec(_field(book, "tick_size") or _field(book, "tickSize")),
        min_order_size=_dec(_field(book, "min_order_size") or _field(book, "minOrderSize")),
        neg_risk=_field(book, "neg_risk") or _field(book, "negRisk"),
    )


def from_book_snapshot(snapshot: Any) -> BookView | None:
    """REST ``BookSnapshot`` -> BookView."""
    return from_any(snapshot, token_id=_field(snapshot, "token_id"))


def from_local_snapshot(snapshot: Any) -> BookView | None:
    """WS ``LocalBookSnapshot`` -> BookView."""
    return from_any(snapshot, token_id=_field(snapshot, "token_id"))
