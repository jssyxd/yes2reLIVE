"""B2 — pre-breach sleeve signal detection (pure book structure, no METAR).

Strategy intent (user-confirmed, 2026-09-06): the reversal engine currently
waits for a METAR observation to *confirm* a bucket breach, then fires. On
thin daily-temperature markets that confirmation usually arrives AFTER the
book has already repriced (everyone reads the same METAR; faster pollers /
pushed feeds move first). guangzhou $0.88 was a fully-priced post-breach
fill — confirmation was not wrong, it was late.

B2 watches the LIVE WS book structure for the market *anticipating* a
breach before the obs reaches us:

  - the old rank-1 bucket's YES weakens  (short-window ask/bid trend down),
  - the neighbour bucket on the likely breach side starts (its YES ask
    strengthens / spread tightens),
  - the neighbour YES remains cheap enough (<= sleeve_max_ask) that a small
    position has room to appreciate if the breach lands.

When the structure moves far enough we enter a SMALL sleeve position on the
neighbour YES *before* any METAR confirms the breach. This is momentum/order-
flow following, NOT information advantage: the sleeve loses when the book
"rotates" without a physical breach (sentiment churn), which is why the
position is small (sleeve_notional_pct of fire budget), capped
(sleeve_max_ask), gated on an armed-or-watching session, and self-closing
(no new METAR within sleeve_timeout_s -> sleeve expires via normal settle or
is recorded as sleeve_timeout so the paper ledger shows the experiment's
true cost).

This module is pure: a ring buffer of recent best prices per token plus a
pure decision function over (rank1 bucket, neighbour bucket) book states.
No I/O; all state is injected so unit tests are deterministic.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def _d(value: Any) -> Decimal | None:
    """Best-effort Decimal parse; None on garbage (never raises)."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class PriceRing:
    """Fixed-capacity (time-decayed) series of best-ask observations.

    Stores (epoch, best_ask) per token. ``sample`` prunes entries older than
    ``window_s`` so TWAP comparisons are over a sliding real-time window
    rather than a fixed count (a quiet book should not make a stale price
    look "stable").
    """

    window_s: float = 600.0  # keep 10 minutes of ticks per token
    maxlen: int = 2000

    _series: dict[str, deque[tuple[float, Decimal]]] = field(default_factory=dict)

    def record(self, token_id: str, best_ask: Decimal | None, now: float | None = None) -> None:
        if best_ask is None:
            return
        now = time.time() if now is None else now
        dq = self._series.setdefault(token_id, deque(maxlen=self.maxlen))
        # Coalesce: consecutive identical prices within 1s add no information
        # and would bias a count-weighted TWAP toward the stagnant level.
        if dq and dq[-1][1] == best_ask and (now - dq[-1][0]) < 1.0:
            return
        dq.append((now, best_ask))

    def _fresh(self, token_id: str, now: float | None = None) -> deque[tuple[float, Decimal]]:
        now = time.time() if now is None else now
        dq = self._series.get(token_id)
        if not dq:
            return deque()
        while dq and (now - dq[0][0]) > self.window_s:
            dq.popleft()
        return dq

    def twap(self, token_id: str, window_s: float, now: float | None = None) -> Decimal | None:
        """Time-weighted average best-ask over the last ``window_s`` seconds.

        Weights = time held at each level (trapezoidal-ish): a level that
        persisted 30s counts 30x a level that flashed for 1s. Returns None
        when there is no data in the window. The retention window is
        ``self.window_s`` (pruned by ``_fresh``); the requested ``window_s``
        is applied here from the tail so different lookbacks over the same
        series stay comparable."""
        now = time.time() if now is None else now
        dq = self._fresh(token_id, now)
        if not dq:
            return None
        # Drop points older than (now - window_s) for THIS lookback.
        clip = now - window_s
        points = list(dq)
        # Keep the newest point even if it is the only one inside the window;
        # binary-search the first index with t >= clip.
        lo, hi = 0, len(points)
        while lo < hi:
            mid = (lo + hi) // 2
            if points[mid][0] >= clip:
                hi = mid
            else:
                lo = mid + 1
        points = points[lo:] if lo < len(points) else points[-1:]
        if not points:
            return None
        # Walk the clipped points computing weighted price.
        acc = Decimal("0")
        weight_sum = 0.0
        prev_t, prev_p = points[0]
        for t, p in points[1:]:
            w = t - prev_t
            if w > 0:
                acc += prev_p * Decimal(str(w))
                weight_sum += w
            prev_t, prev_p = t, p
        # Tail: last level holds until "now" (bounded by the requested window
        # so a stale last tick cannot dominate a short lookback).
        tail = min(now, prev_t + window_s) - prev_t if prev_t < now else 0.0
        if tail > 0:
            acc += prev_p * Decimal(str(tail))
            weight_sum += tail
        if weight_sum <= 0:
            return None
        return acc / Decimal(str(weight_sum))

    def last(self, token_id: str, now: float | None = None) -> Decimal | None:
        dq = self._fresh(token_id, now)
        return dq[-1][1] if dq else None

    def count(self, token_id: str, now: float | None = None) -> int:
        return len(self._fresh(token_id, now))


@dataclass
class SleeveSignal:
    """One detected pre-breach structure signal (pure data)."""

    key: str                  # session key city_id|date|direction
    city_id: str
    market_local_date: str
    direction: str            # "high" | "low"
    rank1_bucket_idx: int
    neighbour_bucket_idx: int
    neighbour_yes_token: str
    neighbour_yes_ask: Decimal
    rank1_yes_twap_short: Decimal | None   # ~2 min
    rank1_yes_twap_long: Decimal | None    # ~10 min
    neighbour_yes_twap_short: Decimal | None
    neighbour_yes_twap_long: Decimal | None
    spread_ratio: Decimal | None           # neighbour ask / rank1 ask (thin check)
    reason: str

    def as_event(self, ts_utc: str) -> dict[str, Any]:
        def s(x: Any) -> str | None:
            return str(x) if x is not None else None
        return {
            "type": "sleeve_signal",
            "key": self.key,
            "city_id": self.city_id,
            "market_local_date": self.market_local_date,
            "direction": self.direction,
            "rank1_bucket_idx": self.rank1_bucket_idx,
            "neighbour_bucket_idx": self.neighbour_bucket_idx,
            "neighbour_yes_token": self.neighbour_yes_token,
            "neighbour_yes_ask": s(self.neighbour_yes_ask),
            "rank1_yes_twap_short": s(self.rank1_yes_twap_short),
            "rank1_yes_twap_long": s(self.rank1_yes_twap_long),
            "neighbour_yes_twap_short": s(self.neighbour_yes_twap_short),
            "neighbour_yes_twap_long": s(self.neighbour_yes_twap_long),
            "spread_ratio": s(self.spread_ratio),
            "reason": self.reason,
            "ts_utc": ts_utc,
        }


def best_ask_of(book: dict[str, Any] | None) -> Decimal | None:
    """Best ask from a normalized ladder cache entry (asks sorted asc)."""
    if not book:
        return None
    ba = book.get("best_ask")
    if ba is not None:
        return _d(ba)
    asks = book.get("asks") or []
    if asks:
        return _d(asks[0].get("price"))
    return None


def _bucket_lo(b: dict[str, Any]) -> float:
    """Sort key for temperature buckets (open lower bound = -inf)."""
    lo = b.get("lo")
    return float(lo) if lo is not None else float("-inf")


def detect_sleeve_signal(
    *,
    buckets: list[dict[str, Any]],
    rank1_bucket_idx: int,
    direction: str,
    books_by_token: dict[str, Any],
    ring: PriceRing,
    now: float | None = None,
    short_window_s: float = 150.0,
    long_window_s: float = 600.0,
    rank1_weaken_drop: Decimal = Decimal("0.05"),
    neighbour_strengthen_rise: Decimal = Decimal("0.04"),
    neighbour_max_ask: Decimal = Decimal("0.35"),
    min_ticks: int = 4,
) -> tuple[int, SleeveSignal] | None:
    """Detect the pre-breach structure move on ONE session.

    Pure decision over bucket token books + the price ring. Returns a
    SleeveSignal when the rank-1 YES has weakened and the breach-side
    neighbour YES has strengthened enough; None otherwise.

    Semantics (high direction as example; low is mirrored):
      rank1  = current favourite bucket (highest YES price). Its YES price
               dropping = market unsure it will stay the max.
      neighbour = the next-higher bucket (a breach would break INTO it).
      Signal: rank1 YES TWAP(short) <= TWAP(long) - rank1_weaken_drop
              AND neighbour YES TWAP(short) >= TWAP(long) + neighbour_strengthen_rise
              AND neighbour best ask <= neighbour_max_ask (cheap enough to buy)
    The short/long TWAP *comparison per token* removes cross-market level
    noise: we only need the neighbour to be trending up relative to ITSELF
    and the rank1 to be trending down relative to ITSELF.
    """
    now = time.time() if now is None else now
    ordered = sorted(buckets, key=_bucket_lo)
    if not ordered or not (0 <= rank1_bucket_idx < len(ordered)):
        return None
    rank1 = ordered[rank1_bucket_idx]
    # neighbour on the likely breach side: high -> idx+1; low -> idx-1.
    n_idx = rank1_bucket_idx + 1 if direction == "high" else rank1_bucket_idx - 1
    if not (0 <= n_idx < len(ordered)):
        return None
    nbr = ordered[n_idx]

    rank1_yes = str(rank1.get("yes_token_id") or "")
    nbr_yes = str(nbr.get("yes_token_id") or "")
    if not rank1_yes or not nbr_yes:
        return None

    r1_short = ring.twap(rank1_yes, short_window_s, now)
    r1_long = ring.twap(rank1_yes, long_window_s, now)
    n_short = ring.twap(nbr_yes, short_window_s, now)
    n_long = ring.twap(nbr_yes, long_window_s, now)
    if r1_short is None or r1_long is None or n_short is None or n_long is None:
        return None
    if ring.count(rank1_yes, now) < min_ticks or ring.count(nbr_yes, now) < min_ticks:
        return None

    n_ask = best_ask_of(books_by_token.get(nbr_yes))
    if n_ask is None or n_ask > neighbour_max_ask:
        return None

    weakened = (r1_long - r1_short) >= rank1_weaken_drop     # short < long
    strengthening = (n_short - n_long) >= neighbour_strengthen_rise
    if not (weakened and strengthening):
        return None

    r1_ask = best_ask_of(books_by_token.get(rank1_yes))
    spread_ratio = None
    if r1_ask is not None and r1_ask > 0:
        spread_ratio = (n_ask / r1_ask).quantize(Decimal("0.01"))

    reason = "rank1_weaken_neighbour_rise"
    if spread_ratio is not None and spread_ratio > Decimal("3.0"):
        reason += "_thin_ratio"
    sig = SleeveSignal(
        key="",  # caller fills session identity
        city_id="",
        market_local_date="",
        direction=direction,
        rank1_bucket_idx=rank1_bucket_idx,
        neighbour_bucket_idx=n_idx,
        neighbour_yes_token=nbr_yes,
        neighbour_yes_ask=n_ask,
        rank1_yes_twap_short=r1_short,
        rank1_yes_twap_long=r1_long,
        neighbour_yes_twap_short=n_short,
        neighbour_yes_twap_long=n_long,
        spread_ratio=spread_ratio,
        reason=reason,
    )
    return n_idx, sig


# --------------------------------------------------------------------------- #
# Runner-facing helpers (still pure; identity is injected by the caller).
# --------------------------------------------------------------------------- #

def locate_rank1_bucket(
    buckets: list[dict[str, Any]],
    books_by_token: dict[str, Any],
    ring: PriceRing,
    now: float | None = None,
) -> int | None:
    """Index (into temperature-sorted buckets) of the current rank-1 bucket.

    Rank by the long-window YES TWAP when the ring has data; fall back to
    the instantaneous best ask. Mirrors consensus_tracker's ranking spirit
    (higher YES price = more likely the extreme bucket)."""
    now = time.time() if now is None else now
    ordered = sorted(buckets, key=_bucket_lo)
    if not ordered:
        return None
    scored: list[tuple[Decimal | None, int]] = []
    for i, b in enumerate(ordered):
        tok = str(b.get("yes_token_id") or "")
        if not tok:
            scored.append((None, i))
            continue
        tw = ring.twap(tok, 600.0, now)
        if tw is None:
            tw = best_ask_of(books_by_token.get(tok))
        scored.append((tw, i))
    scored.sort(key=lambda x: (x[0] is None, -(float(x[0]) if x[0] is not None else 0.0)))
    best = scored[0]
    return best[1] if best[0] is not None else None


def update_rings_from_books(
    ring: PriceRing,
    buckets: list[dict[str, Any]],
    books_by_token: dict[str, Any],
    now: float | None = None,
) -> None:
    """Record every bucket YES/NO best-ask into the price ring.

    Called each cycle for every rule that has books, so the ring accumulates
    the time series B2 needs even when no sleeve fires."""
    now = time.time() if now is None else now
    for b in buckets:
        for side in ("yes_token_id", "no_token_id"):
            tok = str(b.get(side) or "")
            if not tok:
                continue
            ask = best_ask_of(books_by_token.get(tok))
            if ask is not None:
                ring.record(tok, ask, now)
