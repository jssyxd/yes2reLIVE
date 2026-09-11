"""Normalized market-data contract consumed by RiskGate and PaperExecutor.

Both a WebSocket local book (local_order_book.LocalBookSnapshot) and a REST
snapshot (clob_market_data.BookSnapshot) are converted to this one shape by
``adapters.polymarket.orderbook``, so execution logic never branches on the
book's transport.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BookView:
    """A point-in-time executable order-book view for one token."""

    token_id: str
    asks: tuple[tuple[Decimal, Decimal], ...]   # (price, size) ascending (best ask first)
    bids: tuple[tuple[Decimal, Decimal], ...]   # (price, size) descending (best bid first)
    best_ask: Decimal | None
    best_bid: Decimal | None
    age_seconds: float
    book_hash: str | None = None
    timestamp: str | None = None
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    neg_risk: bool | None = None

    @property
    def ready(self) -> bool:
        return self.best_ask is not None and len(self.asks) > 0

    @property
    def ask_depth_shares(self) -> Decimal:
        return sum((size for _, size in self.asks), Decimal("0"))
