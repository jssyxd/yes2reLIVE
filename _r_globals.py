"""Process-local caches for the weatherbotyes2re paper runner. No sigma.

Owns long-lived per-process objects that must survive across ``run_cycle``
calls but must not be rebuilt from disk every tick:
  - the :class:`ConsensusTracker` (rolling per-bucket TWAP history)
  - the read-only CLOB client
  - last-fetch epoch stamps (cadence gating)
  - the normalized in-memory ladder book cache ``{token_id: book_dict}``
None of these are serialized; they are warm-up state only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clob_market_data import CLOBMarketData
from consensus_tracker import ConsensusTracker
import time

_TRACKER: ConsensusTracker | None = None
_CLOB: CLOBMarketData | None = None

# {token_id: ladder book dict}  (best_ask/best_bid/tick_size/asks[{price,size}])
_BOOK_CACHE: dict[str, dict[str, Any]] = {}

# WS-event wake: runner sleep is interrupted when an interesting token moves.
_WAKE_EPOCH: float = 0.0
_WATCH_TOKENS: set[str] = set()


def mark_wake(epoch: float | None = None) -> None:
    """Signal the run loop that a watched token changed (called on WS thread)."""
    global _WAKE_EPOCH
    _WAKE_EPOCH = time.time() if epoch is None else float(epoch)


def wake_epoch() -> float:
    return _WAKE_EPOCH


def watch_tokens() -> set[str]:
    return _WATCH_TOKENS


def set_watch_tokens(tokens: set[str]) -> None:
    global _WATCH_TOKENS
    _WATCH_TOKENS = set(tokens)


@dataclass
class Stamp:
    at: float = 0.0


_STAMPS: dict[str, Stamp] = {}


def stamp(name: str) -> float:
    s = _STAMPS.get(name)
    return s.at if s else 0.0


def bump(name: str, now: float) -> None:
    s = _STAMPS.get(name)
    if s is None:
        _STAMPS[name] = Stamp(at=now)
    else:
        s.at = now


def tracker() -> ConsensusTracker:
    """Process-local singleton consensus/gate history."""
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = ConsensusTracker(window_seconds=7200, min_samples=20)
    return _TRACKER


def clob(timeout_seconds: float = 8.0) -> CLOBMarketData:
    """Process-local read-only CLOB client (never signs / submits orders)."""
    global _CLOB
    if _CLOB is None:
        _CLOB = CLOBMarketData(timeout_seconds=timeout_seconds)
    return _CLOB


def book_cache() -> dict[str, dict[str, Any]]:
    return _BOOK_CACHE


# Per-cycle observability surface surfaced to write_health for the watcher.
_HEALTH_EXTRA: dict[str, Any] = {}


def set_health_extra(data: dict[str, Any]) -> None:
    _HEALTH_EXTRA.clear()
    _HEALTH_EXTRA.update(data)


def health_extra() -> dict[str, Any]:
    return dict(_HEALTH_EXTRA)
