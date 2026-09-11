"""ws_bridge.py — keep a Polymarket market-WebSocket alive on a daemon thread.

The transport (market_ws_transport.py) feeds wire frames into a MarketStream
(websocket_market_data.py) which owns per-token LocalOrderBook instances.
_r_cycle.py pumps fresh snapshots from those local books into the shared
process ladder cache each cycle, so paper FAK sees sub-second book updates
instead of REST-polled ones. REST /books remains the correctness backbone:
WS is a best-effort freshness accelerator (see market_ws_transport header).

stdlib only.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable

from market_ws_transport import MarketSocketTransport
from websocket_market_data import MarketStream


class WSBridge:
    """Owns one MarketStream + one transport thread (self-reconnecting)."""

    def __init__(self) -> None:
        self.stream: MarketStream | None = None
        self.transport: MarketSocketTransport | None = None
        self._thread: threading.Thread | None = None
        self.started_at: float | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, token_ids: Iterable[str]) -> bool:
        with self._lock:
            if self._thread is not None:
                return self.running
            ids = [str(t) for t in token_ids if str(t)]
            if not ids:
                return False
            try:
                self.stream = MarketStream(ids)
                self.transport = MarketSocketTransport(self.stream)
                self._thread = threading.Thread(
                    target=self._run_forever, name="ws-market", daemon=True
                )
                self._thread.start()
                self.started_at = time.time()
            except Exception:
                self.stream = None
                self.transport = None
                self._thread = None
                raise
            return True

    def _run_forever(self) -> None:
        try:
            assert self.transport is not None
            self.transport.run_forever()
        except Exception:
            pass  # daemon; never crash the runner

    def ensure_tokens(self, token_ids: Iterable[str]) -> None:
        """Grow the subscription set and re-send a subscribe frame when new
        tokens appear (rules refresh / new market-local-day).

        Locked: the daemon thread may be mid-reconnect (mark_disconnected
        iterating books.values() to invalidate) — mutating the dict without
        the lock risks RuntimeError: dictionary changed size during iteration.
        """
        if self.stream is None or self.transport is None:
            return
        with self._lock:
            before = len(self.stream.books)
            self.stream.ensure_tokens(token_ids)
            if len(self.stream.books) > before:
                try:
                    sub = json.dumps(self.stream.subscription_message())
                    self.transport.send_text(sub)
                except Exception:
                    pass

    def telemetry(self) -> dict[str, Any]:
        if self.transport is None:
            return {"deployed": False, "mode": "REST_seed_only"}
        tel = self.transport.telemetry
        return {
            "deployed": True,
            "mode": "ws_seed_live",
            "connected": bool(self.transport._sock),
            "connect_count": tel.connect_count,
            "reconnect_count": tel.reconnect_count,
            "message_count": tel.message_count,
            "event_count": self.stream.event_count if self.stream else 0,
            "subscribed_tokens": len(self.stream.books) if self.stream else 0,
            "last_event_at": tel.last_event_at,
            "connect_errors": tel.connect_errors,
        }

    def stop(self) -> None:
        with self._lock:
            if self.transport is not None:
                try:
                    self.transport.close()
                except Exception:
                    pass


_SINGLETON: WSBridge | None = None


def ws_bridge() -> WSBridge:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = WSBridge()
    return _SINGLETON
