"""Long-horizon market consensus tracker for reversal filter.

Tracks per-bucket YES mid (or best ask) over a rolling window so we only
fire when the broken bucket was the event's dominant consensus for 1–2h,
not a bucket the market already abandoned.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Deque, Iterable

ZERO = Decimal("0")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dec(v: Any, default: str = "0") -> Decimal:
    try:
        if v is None:
            return Decimal(default)
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


@dataclass
class PriceSample:
    ts_utc: datetime
    mid: Decimal
    best_ask: Decimal | None = None
    best_bid: Decimal | None = None
    ask_depth: Decimal = ZERO  # top-N size sum if known


@dataclass
class BucketSeries:
    samples: Deque[PriceSample] = field(default_factory=lambda: deque(maxlen=7200))

    def add(self, sample: PriceSample) -> None:
        self.samples.append(sample)

    def prune(self, now_utc: datetime, window_seconds: int) -> None:
        cutoff = now_utc.timestamp() - window_seconds
        while self.samples and self.samples[0].ts_utc.timestamp() < cutoff:
            self.samples.popleft()

    def twap_mid(self, now_utc: datetime, window_seconds: int) -> Decimal | None:
        self.prune(now_utc, window_seconds)
        if len(self.samples) < 2:
            if self.samples:
                return self.samples[-1].mid
            return None
        # time-weighted average of mid
        total_w = ZERO
        acc = ZERO
        pts = list(self.samples)
        for i in range(len(pts) - 1):
            dt = Decimal(str(max(0.0, (pts[i + 1].ts_utc - pts[i].ts_utc).total_seconds())))
            if dt <= 0:
                continue
            acc += pts[i].mid * dt
            total_w += dt
        # tail to now
        tail = Decimal(str(max(0.0, (now_utc - pts[-1].ts_utc).total_seconds())))
        if tail > 0:
            acc += pts[-1].mid * tail
            total_w += tail
        if total_w <= 0:
            return pts[-1].mid
        return acc / total_w

    def mean_ask_depth(self, now_utc: datetime, window_seconds: int) -> Decimal:
        self.prune(now_utc, window_seconds)
        if not self.samples:
            return ZERO
        return sum((s.ask_depth for s in self.samples), ZERO) / Decimal(len(self.samples))


class ConsensusTracker:
    """city|date|direction -> bucket_id -> BucketSeries"""

    def __init__(self, window_seconds: int = 7200, min_samples: int = 30) -> None:
        self.window_seconds = int(window_seconds)
        self.min_samples = int(min_samples)
        self._series: dict[str, dict[str, BucketSeries]] = defaultdict(dict)

    def session_key(self, city_id: str, market_local_date: str, direction: str) -> str:
        return f"{city_id}|{market_local_date}|{direction}"

    def record(
        self,
        city_id: str,
        market_local_date: str,
        direction: str,
        bucket_id: str,
        *,
        mid: Any = None,
        best_ask: Any = None,
        best_bid: Any = None,
        ask_depth: Any = None,
        now_utc: datetime | None = None,
    ) -> None:
        now = now_utc or datetime.now(timezone.utc)
        ask = _dec(best_ask) if best_ask is not None else None
        bid = _dec(best_bid) if best_bid is not None else None
        if mid is not None:
            m = _dec(mid)
        elif ask is not None and bid is not None and bid > 0:
            m = (ask + bid) / 2
        elif ask is not None:
            m = ask
        else:
            return
        key = self.session_key(city_id, market_local_date, direction)
        series = self._series[key].setdefault(str(bucket_id), BucketSeries())
        series.add(
            PriceSample(
                ts_utc=now,
                mid=m,
                best_ask=ask,
                best_bid=bid,
                ask_depth=_dec(ask_depth),
            )
        )

    def record_books(
        self,
        city_id: str,
        market_local_date: str,
        direction: str,
        buckets: Iterable[dict[str, Any]],
        books_by_token: dict[str, Any] | None,
        now_utc: datetime | None = None,
    ) -> None:
        """Sample YES-side books for each bucket (prefer yes_token_id)."""
        if not books_by_token:
            return
        now = now_utc or datetime.now(timezone.utc)
        for b in buckets:
            bid = str(b.get("bucket_id") or b.get("id") or "")
            yes_tok = str(b.get("yes_token_id") or b.get("_yes_token_id") or "")
            if not yes_tok:
                continue
            book = books_by_token.get(yes_tok)
            if book is None:
                continue
            if isinstance(book, dict):
                ask = book.get("best_ask")
                bidp = book.get("best_bid")
                depth = ZERO
                for row in (book.get("asks") or [])[:3]:
                    if isinstance(row, dict):
                        depth += _dec(row.get("size"))
                    elif isinstance(row, (list, tuple)) and len(row) >= 2:
                        depth += _dec(row[1])
            else:
                ask = getattr(book, "best_ask", None)
                bidp = getattr(book, "best_bid", None)
                depth = ZERO
            self.record(
                city_id,
                market_local_date,
                direction,
                bid,
                best_ask=ask,
                best_bid=bidp,
                ask_depth=depth,
                now_utc=now,
            )

    def rank_buckets(
        self,
        city_id: str,
        market_local_date: str,
        direction: str,
        now_utc: datetime | None = None,
        window_seconds: int | None = None,
    ) -> list[tuple[str, Decimal, Decimal]]:
        """Return [(bucket_id, twap_mid, mean_depth)] sorted by twap desc."""
        now = now_utc or datetime.now(timezone.utc)
        win = int(window_seconds or self.window_seconds)
        key = self.session_key(city_id, market_local_date, direction)
        rows: list[tuple[str, Decimal, Decimal]] = []
        for bid, series in self._series.get(key, {}).items():
            series.prune(now, win)
            tw = series.twap_mid(now, win)
            if tw is None:
                continue
            depth = series.mean_ask_depth(now, win)
            rows.append((bid, tw, depth))
        rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return rows

    def is_long_horizon_consensus(
        self,
        city_id: str,
        market_local_date: str,
        direction: str,
        bucket_id: str,
        *,
        now_utc: datetime | None = None,
        window_seconds: int | None = None,
        min_lead: Decimal = Decimal("0.05"),
        require_rank1: bool = True,
        min_samples: int | None = None,
    ) -> dict[str, Any]:
        """True if bucket_id was rank-1 (or clear leader) over the window."""
        now = now_utc or datetime.now(timezone.utc)
        win = int(window_seconds or self.window_seconds)
        need = int(min_samples if min_samples is not None else self.min_samples)
        ranks = self.rank_buckets(city_id, market_local_date, direction, now, win)
        if not ranks:
            return {
                "ok": False,
                "reason": "no_price_history",
                "rank": None,
                "twap": None,
                "lead": None,
                "samples_ok": False,
            }
        key = self.session_key(city_id, market_local_date, direction)
        series = self._series.get(key, {}).get(str(bucket_id))
        n_samples = len(series.samples) if series else 0
        samples_ok = n_samples >= need
        ranked_ids = [r[0] for r in ranks]
        try:
            rank_idx = ranked_ids.index(str(bucket_id))
        except ValueError:
            return {
                "ok": False,
                "reason": "bucket_not_in_history",
                "rank": None,
                "twap": None,
                "lead": None,
                "samples_ok": samples_ok,
                "n_samples": n_samples,
            }
        twap = ranks[rank_idx][1]
        lead = ZERO
        if rank_idx == 0 and len(ranks) > 1:
            lead = ranks[0][1] - ranks[1][1]
        elif rank_idx > 0:
            lead = ranks[rank_idx][1] - ranks[0][1]  # negative

        ok = True
        reason = "ok"
        if require_rank1 and rank_idx != 0:
            ok = False
            reason = "not_rank1"
        elif rank_idx == 0 and lead < min_lead and len(ranks) > 1:
            # still rank1 but lead too thin — optional soft fail
            ok = True
            reason = "rank1_thin_lead"
        if not samples_ok:
            ok = False
            reason = "insufficient_samples" if reason == "ok" else reason + "+insufficient_samples"

        return {
            "ok": ok,
            "reason": reason,
            "rank": rank_idx + 1,
            "twap": str(twap),
            "lead": str(lead),
            "samples_ok": samples_ok,
            "n_samples": n_samples,
            "window_seconds": win,
            "top": [{"bucket_id": a, "twap": str(b), "depth": str(c)} for a, b, c in ranks[:3]],
            "checked_at_utc": _iso(now),
        }


# process-local default tracker (runner can inject its own)
DEFAULT_TRACKER = ConsensusTracker()
