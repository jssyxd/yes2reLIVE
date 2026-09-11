"""Deterministic local order-book state for Polymarket market-stream events."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


class OrderBookStateError(ValueError):
    pass


@dataclass(frozen=True)
class LocalBookSnapshot:
    token_id: str
    market: str | None
    bids: tuple[dict[str, Decimal], ...]
    asks: tuple[dict[str, Decimal], ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    book_hash: str | None
    exchange_timestamp: str | None
    received_at_epoch: float
    version: int
    ready: bool
    tick_size: Decimal | None
    min_order_size: Decimal | None
    neg_risk: bool | None
    last_trade_price: Decimal | None

    def is_fresh(self, max_age_seconds: float, *, now: float | None = None) -> bool:
        import time
        if not self.ready or self.received_at_epoch <= 0:
            return False
        return (time.time() if now is None else now) - self.received_at_epoch <= max_age_seconds


class LocalOrderBook:
    def __init__(self, token_id: str, *, clock=None) -> None:
        self.token_id = str(token_id)
        self.clock = clock or __import__("time").time
        self.market: str | None = None
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.book_hash: str | None = None
        self.exchange_timestamp: str | None = None
        self.received_at_epoch = 0.0
        self.version = 0
        self.ready = False
        self.tick_size: Decimal | None = None
        self.min_order_size: Decimal | None = None
        self.neg_risk: bool | None = None
        self.last_trade_price: Decimal | None = None

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise OrderBookStateError(f"invalid_{field}") from exc
        if not result.is_finite() or result < 0:
            raise OrderBookStateError(f"invalid_{field}")
        return result

    def _levels(self, value: Any, field: str) -> dict[Decimal, Decimal]:
        if not isinstance(value, list):
            raise OrderBookStateError(f"invalid_{field}")
        result: dict[Decimal, Decimal] = {}
        for level in value:
            if not isinstance(level, dict):
                raise OrderBookStateError(f"invalid_{field}_level")
            price = self._decimal(level.get("price"), f"{field}_price")
            size = self._decimal(level.get("size"), f"{field}_size")
            if price <= 0:
                raise OrderBookStateError(f"invalid_{field}_price")
            if size > 0:
                result[price] = size
        return result

    def apply_book(self, payload: dict[str, Any]) -> LocalBookSnapshot:
        token = str(payload.get("tokenId") or payload.get("asset_id") or "")
        if token and token != self.token_id:
            raise OrderBookStateError("token_id_mismatch")
        self._bids = self._levels(payload.get("bids") or [], "bids")
        self._asks = self._levels(payload.get("asks") or [], "asks")
        self.market = str(payload["market"]) if payload.get("market") is not None else self.market
        self.book_hash = str(payload["hash"]) if payload.get("hash") is not None else None
        self.exchange_timestamp = str(payload["timestamp"]) if payload.get("timestamp") is not None else None
        self.tick_size = self._optional_decimal(payload.get("tickSize", payload.get("tick_size")), "tick_size")
        self.min_order_size = self._optional_decimal(payload.get("minOrderSize", payload.get("min_order_size")), "min_order_size")
        self.neg_risk = payload.get("negRisk", payload.get("neg_risk"))
        if self.neg_risk is not None and not isinstance(self.neg_risk, bool):
            raise OrderBookStateError("invalid_neg_risk")
        self.version += 1
        self.received_at_epoch = float(self.clock())
        self.ready = True
        return self.snapshot()

    def apply_price_change(self, payload: dict[str, Any]) -> LocalBookSnapshot:
        token = str(payload.get("tokenId") or "")
        if token and token != self.token_id:
            raise OrderBookStateError("token_id_mismatch")
        if not self.ready:
            raise OrderBookStateError("book_baseline_required")
        price = self._decimal(payload.get("price"), "price")
        size = self._decimal(payload.get("size"), "size")
        side = str(payload.get("side", "")).upper()
        if side in {"SELL", "ASK"}:
            levels = self._asks
        elif side in {"BUY", "BID"}:
            levels = self._bids
        else:
            raise OrderBookStateError("invalid_side")
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        if payload.get("hash") is not None:
            self.book_hash = str(payload["hash"])
        if payload.get("timestamp") is not None:
            self.exchange_timestamp = str(payload["timestamp"])
        self.version += 1
        self.received_at_epoch = float(self.clock())
        return self.snapshot()

    def apply_tick_size_change(self, payload: dict[str, Any]) -> LocalBookSnapshot:
        new_tick = self._optional_decimal(payload.get("newTickSize"), "tick_size")
        if new_tick is None or new_tick <= 0:
            raise OrderBookStateError("invalid_tick_size")
        self.tick_size = new_tick
        self.version += 1
        self.received_at_epoch = float(self.clock())
        return self.snapshot()

    @staticmethod
    def _optional_decimal(value: Any, field: str) -> Decimal | None:
        if value is None:
            return None
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise OrderBookStateError(f"invalid_{field}") from exc
        if not result.is_finite() or result < 0:
            raise OrderBookStateError(f"invalid_{field}")
        return result

    def invalidate(self) -> None:
        self.ready = False
        self.version += 1

    def snapshot(self) -> LocalBookSnapshot:
        return LocalBookSnapshot(
            token_id=self.token_id, market=self.market,
            bids=tuple({"price": p, "size": s} for p, s in sorted(self._bids.items())),
            asks=tuple({"price": p, "size": s} for p, s in sorted(self._asks.items())),
            best_bid=max(self._bids, default=None), best_ask=min(self._asks, default=None),
            book_hash=self.book_hash, exchange_timestamp=self.exchange_timestamp,
            received_at_epoch=self.received_at_epoch, version=self.version, ready=self.ready,
            tick_size=self.tick_size, min_order_size=self.min_order_size,
            neg_risk=self.neg_risk, last_trade_price=self.last_trade_price,
        )

    def is_fresh(self, max_age_seconds: float, *, now: float | None = None) -> bool:
        if not self.ready or self.received_at_epoch <= 0:
            return False
        return (self.clock() if now is None else now) - self.received_at_epoch <= max_age_seconds
