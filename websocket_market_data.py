"""Polymarket market-stream adapter (public WS). Paper path may REST-seed LocalOrderBook.

Never submits orders. Transport can be injected for tests.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from local_order_book import LocalOrderBook, LocalBookSnapshot, OrderBookStateError
from _r_globals import mark_wake, watch_tokens

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketStreamError(RuntimeError):
    pass


class MarketStream:
    def __init__(self, token_ids: Iterable[str], *, clock=None) -> None:
        ids = tuple(dict.fromkeys(str(x) for x in token_ids if str(x)))
        if not ids:
            raise ValueError("at_least_one_token_required")
        self.token_ids = ids
        self.books = {token: LocalOrderBook(token, clock=clock) for token in ids}
        self.connected = False
        self.subscribed = False
        self.last_error: str | None = None
        self.event_count = 0

    def ensure_tokens(self, token_ids: Iterable[str]) -> None:
        for tid in token_ids:
            t = str(tid)
            if t and t not in self.books:
                self.books[t] = LocalOrderBook(t)
        self.token_ids = tuple(self.books.keys())

    def subscription_message(self) -> dict[str, Any]:
        return {"type": "market", "assets_ids": list(self.token_ids), "custom_feature_enabled": True}

    def mark_connected(self) -> dict[str, Any]:
        self.connected = True
        self.subscribed = False
        self.last_error = None
        return self.subscription_message()

    def mark_disconnected(self, reason: str = "disconnected") -> None:
        self.connected = False
        self.subscribed = False
        self.last_error = reason
        for book in list(self.books.values()):  # copy: caller thread may grow dict
            book.invalidate()

    def mark_subscribed(self) -> None:
        if not self.connected:
            raise MarketStreamError("connection_required")
        self.subscribed = True

    def seed_from_rest(self, token_id: str, raw_book: dict[str, Any]) -> LocalBookSnapshot | None:
        """Apply a REST /book payload as baseline so FAK can use in-memory L2."""
        tid = str(token_id)
        if tid not in self.books:
            self.books[tid] = LocalOrderBook(tid)
        payload = dict(raw_book)
        payload.setdefault("asset_id", tid)
        payload.setdefault("tokenId", tid)
        try:
            return self.books[tid].apply_book(payload)
        except OrderBookStateError:
            return None

    def handle_message(self, message: str | bytes | dict[str, Any]) -> LocalBookSnapshot | tuple[LocalBookSnapshot, ...] | None:
        if isinstance(message, (str, bytes)):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError as exc:
                raise MarketStreamError("invalid_json") from exc
        else:
            payload = message
        if not isinstance(payload, dict):
            raise MarketStreamError("invalid_event_shape")
        event_type = str(payload.get("type", "")).lower()
        body = payload.get("payload", payload)
        if event_type in {"subscribe", "subscribed", "ack"}:
            self.mark_subscribed()
            return None
        if not isinstance(body, dict):
            raise MarketStreamError("invalid_event_payload")
        if event_type == "book":
            return self._apply_book(body)
        if event_type == "price_change":
            return self._apply_price_changes(body)
        if event_type == "tick_size_change":
            token = str(body.get("tokenId") or "")
            return self._book(token).apply_tick_size_change(body)
        if event_type == "last_trade_price":
            token = str(body.get("tokenId") or "")
            book = self._book(token)
            book.last_trade_price = book._optional_decimal(body.get("price"), "last_trade_price")
            return book.snapshot()
        if event_type in {"best_bid_ask", "new_market", "market_resolved", "heartbeat", "ping", "pong"}:
            return None
        raise MarketStreamError(f"unsupported_event:{event_type}")

    def _book(self, token: str) -> LocalOrderBook:
        if token not in self.books:
            raise OrderBookStateError("unknown_token_id")
        return self.books[token]

    def _apply_book(self, body: dict[str, Any]) -> LocalBookSnapshot:
        token = str(body.get("tokenId") or body.get("asset_id") or "")
        result = self._book(token).apply_book(body)
        self.event_count += 1
        if token in watch_tokens():
            mark_wake()
        return result

    def _apply_price_changes(self, body: dict[str, Any]) -> tuple[LocalBookSnapshot, ...]:
        changes = body.get("priceChanges", body.get("price_changes"))
        if not isinstance(changes, list):
            raise MarketStreamError("price_changes_required")
        results = []
        watched = watch_tokens()
        for change in changes:
            if not isinstance(change, dict):
                raise MarketStreamError("invalid_price_change")
            if body.get("timestamp") is not None:
                change = {**change, "timestamp": body["timestamp"]}
            token = str(change.get("tokenId") or "")
            results.append(self._book(token).apply_price_change(change))
            if token in watched:
                mark_wake()
        self.event_count += 1
        return tuple(results)

    def snapshot(self, token_id: str, *, max_age_seconds: float, now: float | None = None) -> LocalBookSnapshot | None:
        book = self._book(str(token_id))
        return book.snapshot() if book.is_fresh(max_age_seconds, now=now) else None
