"""Production-oriented, read-only CLOB market-data layer for tree2.

This module deliberately does not sign or submit orders. It provides a bounded
REST fallback/cache that can be used by paper execution and by a future market
stream adapter. Every decision carries snapshot metadata so displayed prices
cannot be confused with executable asks.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Iterable

CLOB_BASE = "https://clob.polymarket.com"
BOOK_ENDPOINT = f"{CLOB_BASE}/book"
BOOKS_ENDPOINT = f"{CLOB_BASE}/books"
FEE_ENDPOINT = f"{CLOB_BASE}/fee-rate"
BOOKS_CHUNK_SIZE = 100


@dataclass(frozen=True)
class BookSnapshot:
    token_id: str
    fetched_at_epoch: float
    timestamp: str | None
    book_hash: str | None
    asset_id: str | None
    market: str | None
    min_order_size: Decimal | None
    tick_size: Decimal | None
    neg_risk: bool | None
    asks: tuple[dict[str, Decimal], ...]
    bids: tuple[dict[str, Decimal], ...]
    source: str

    @property
    def best_ask(self) -> Decimal | None:
        return min((x["price"] for x in self.asks), default=None)

    @property
    def best_bid(self) -> Decimal | None:
        return max((x["price"] for x in self.bids), default=None)

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["min_order_size"] = str(self.min_order_size) if self.min_order_size is not None else None
        value["tick_size"] = str(self.tick_size) if self.tick_size is not None else None
        value["asks"] = [{"price": str(x["price"]), "size": str(x["size"])} for x in self.asks]
        value["bids"] = [{"price": str(x["price"]), "size": str(x["size"])} for x in self.bids]
        value["best_ask"] = str(self.best_ask) if self.best_ask is not None else None
        value["best_bid"] = str(self.best_bid) if self.best_bid is not None else None
        return value


class CLOBDataError(RuntimeError):
    pass


class CLOBMarketData:
    def __init__(self, timeout_seconds: float = 5.0, max_snapshot_age_seconds: float = 3.0):
        self.timeout_seconds = timeout_seconds
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self._books: dict[str, BookSnapshot] = {}
        self._fees: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _request_json(url: str, payload: Any | None = None, timeout: float = 5.0) -> Any:
        headers = {"User-Agent": "weatherbot-tree2/market-data"}
        data = None
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CLOBDataError(f"clob_http_{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CLOBDataError("clob_network_error") from exc
        except json.JSONDecodeError as exc:
            raise CLOBDataError("clob_invalid_json") from exc

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal | None:
        if value is None:
            return None
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise CLOBDataError(f"invalid_{field}") from exc
        if not result.is_finite() or result < 0:
            raise CLOBDataError(f"invalid_{field}")
        return result

    @classmethod
    def _parse_book(cls, token_id: str, raw: dict[str, Any], source: str) -> BookSnapshot:
        if not isinstance(raw, dict):
            raise CLOBDataError("invalid_book_shape")
        asset_id = str(raw.get("asset_id")) if raw.get("asset_id") is not None else None
        if asset_id is not None and asset_id != token_id:
            raise CLOBDataError("asset_id_mismatch")

        def levels(name: str) -> tuple[dict[str, Decimal], ...]:
            value = raw.get(name)
            if not isinstance(value, list):
                raise CLOBDataError(f"invalid_{name}")
            parsed = []
            for level in value:
                if not isinstance(level, dict):
                    continue
                price = cls._decimal(level.get("price"), f"{name}_price")
                size = cls._decimal(level.get("size"), f"{name}_size")
                if price is not None and size is not None and size > 0:
                    parsed.append({"price": price, "size": size})
            return tuple(parsed)

        return BookSnapshot(
            token_id=token_id,
            fetched_at_epoch=time.time(),
            timestamp=str(raw.get("timestamp")) if raw.get("timestamp") is not None else None,
            book_hash=str(raw.get("hash")) if raw.get("hash") is not None else None,
            asset_id=asset_id,
            market=str(raw.get("market")) if raw.get("market") is not None else None,
            min_order_size=cls._decimal(raw.get("min_order_size"), "min_order_size"),
            tick_size=cls._decimal(raw.get("tick_size"), "tick_size"),
            neg_risk=(bool(raw["neg_risk"]) if "neg_risk" in raw and raw["neg_risk"] is not None else None),
            asks=levels("asks"),
            bids=levels("bids"),
            source=source,
        )

    def snapshot_from_raw(self, token_id: str, raw: dict[str, Any], source: str = "replay") -> BookSnapshot:
        snapshot = self._parse_book(str(token_id), raw, source)
        self._books[str(token_id)] = snapshot
        return snapshot

    def fetch_books(self, token_ids: Iterable[str]) -> dict[str, BookSnapshot]:
        ids = list(dict.fromkeys(str(x) for x in token_ids if str(x)))
        if not ids:
            return {}
        # The public /books endpoint accepts a batched list, but very large
        # batches (thousands of tokens) fail or time out behind proxies and
        # then fall into an unbounded per-token fallback. Fetch in bounded
        # chunks and only single-fetch the few tokens missing from each chunk,
        # so a partial snapshot fails closed instead of blocking the observer.
        parsed: dict[str, BookSnapshot] = {}
        chunks = [ids[i : i + BOOKS_CHUNK_SIZE] for i in range(0, len(ids), BOOKS_CHUNK_SIZE)]
        workers = min(10, len(chunks))
        if workers <= 1:
            for chunk in chunks:
                parsed.update(self._fetch_book_chunk(chunk))
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for chunk_parsed in executor.map(self._fetch_book_chunk, chunks):
                    parsed.update(chunk_parsed)
        self._books.update(parsed)
        return parsed

    def _fetch_book_chunk(self, chunk: list[str]) -> dict[str, BookSnapshot]:
        """Fetch one ≤100-token chunk; single-fetch the few tokens missing from it.

        Fail-closed per chunk (a slow/timed-out chunk degrades only itself) and
        safe to run concurrently with other chunks — this method touches no
        shared mutable state (caller merges results)."""
        parsed: dict[str, BookSnapshot] = {}
        try:
            raw = self._request_json(
                BOOKS_ENDPOINT,
                payload=[{"token_id": token} for token in chunk],
                timeout=self.timeout_seconds,
            )
            items = raw if isinstance(raw, list) else raw.get("books", []) if isinstance(raw, dict) else []
            for item in items:
                if isinstance(item, dict) and item.get("asset_id") is not None:
                    token = str(item["asset_id"])
                    if token in chunk:
                        parsed[token] = self._parse_book(token, item, "rest_batch")
        except CLOBDataError:
            pass
        missing = [token for token in chunk if token not in parsed]
        for token in missing:
            try:
                raw = self._request_json(
                    f"{BOOK_ENDPOINT}?{urllib.parse.urlencode({'token_id': token})}",
                    timeout=self.timeout_seconds,
                )
                parsed[token] = self._parse_book(token, raw, "rest_single")
            except CLOBDataError:
                continue
        return parsed

    def get_cached(self, token_id: str, max_age_seconds: float | None = None) -> BookSnapshot | None:
        snapshot = self._books.get(str(token_id))
        if snapshot is None:
            return None
        limit = self.max_snapshot_age_seconds if max_age_seconds is None else max_age_seconds
        if time.time() - snapshot.fetched_at_epoch > limit:
            return None
        return snapshot

    def fetch_fee_rate(self, token_id: str) -> dict[str, Any]:
        token = str(token_id)
        cached = self._fees.get(token)
        if cached and time.time() - cached[0] <= 30:
            return dict(cached[1])
        raw = self._request_json(
            f"{FEE_ENDPOINT}?{urllib.parse.urlencode({'token_id': token})}",
            timeout=self.timeout_seconds,
        )
        if not isinstance(raw, dict) or raw.get("base_fee") is None:
            raise CLOBDataError("invalid_fee_rate")
        self._fees[token] = (time.time(), dict(raw))
        return dict(raw)

    def executable_summary(self, token_id: str, max_age_seconds: float | None = None, max_price: Decimal = Decimal("0.98")) -> dict[str, Any]:
        snapshot = self.get_cached(token_id, max_age_seconds)
        if snapshot is None:
            return {"status": "STALE_OR_MISSING_BOOK", "token_id": str(token_id)}
        eligible_asks = [x for x in snapshot.asks if Decimal("0.05") <= x["price"] <= max_price]
        if not eligible_asks:
            return {
                "status": "EMPTY_ASK" if not snapshot.asks else "ASK_OUTSIDE_LIMIT",
                "token_id": snapshot.token_id,
                "book_timestamp": snapshot.timestamp,
                "book_hash": snapshot.book_hash,
                "best_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
                "best_ask": None,
            }
        return {
            "status": "EXECUTABLE_ASK_PRESENT",
            "token_id": snapshot.token_id,
            "book_timestamp": snapshot.timestamp,
            "book_hash": snapshot.book_hash,
            "book_age_seconds": round(time.time() - snapshot.fetched_at_epoch, 3),
            "best_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
            "best_ask": str(snapshot.best_ask) if snapshot.best_ask is not None else None,
            "ask_depth_shares": str(sum(x["size"] for x in eligible_asks)),
            "ask_levels": [{"price": str(x["price"]), "size": str(x["size"])} for x in eligible_asks],
        }
