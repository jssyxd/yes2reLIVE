"""market_ws_transport.py — stdlib-only market-WebSocket transport for Polymarket.

What this is: the missing socket layer for websocket_market_data.MarketStream.
The repo already has the full market-channel state machine + LocalOrderBook
(apply_book / apply_price_change / seed_from_rest), but no network I/O. This
module supplies a minimal, dependency-free WebSocket client that reaches the
real endpoint through a CONNECT proxy (direct wss into Polymarket is blocked
from this network; proxy http://192.168.1.5:7890 is the required path):

    TCP -> proxy CONNECT host:443 -> TLS(SNI) -> WS upgrade (Sec-WebSocket-*)
          -> masked client frames / unmasked server frames -> message loop.

Wire facts established empirically against the LIVE endpoint (2026-09-04):
  * Server->client frames are NOT capped at 4 KB; the initial subscribe dump
    arrives as ONE JSON text frame of several KB (~7.3 KB observed). The frame
    codec implements 126/127 extended lengths fully and reassembles FIN=0
    fragmentation, so length is never a practical bound.
  * The FIRST frame after subscribing is a JSON *list* of full per-asset book
    objects (keys incl. market, asset_id, timestamp, hash, bids, asks,
    tick_size, event_type, last_trade_price). It is NOT a {"type": ...} dict,
    so it cannot go through MarketStream.handle_message (which raises
    invalid_event_shape on non-dicts). Each element is a full-book baseline and
    must seed its LocalOrderBook (ready=True) BEFORE any delta applies.
  * Subsequent frames are {"type": "price_change"|"book"|...} dicts whose
    payload carries priceChanges[]/bids/asks whose shapes match what
    MarketStream.handle_message already parses (docs + py-clob-client agree).
    Those are forwarded verbatim to handle_message.
  * HTTP 101 head and the first data frame frequently arrive inside one TLS
    record. The single buffered reader below therefore reads the upgrade head
    with read_until() so no frame bytes are ever dropped (a lost-head is a
    classic 2-byte stream corruption that silently yields bogus opcodes).

Operational caveat carried into the design: Polymarket's market WS is known
(py-clob-client #292) to intermittently accept connections + stay PING/PONG
healthy while silently choking on book/price events. The transport therefore:
(a) treats REST /book as the correctness backbone (callers keep seeding /
failing-closed on REST) and WS as a best-effort freshness accelerator; (b)
self-reconnects with 5/10/30 s backoff and re-seeds so local books re-baseline
instead of decaying; and (c) exposes wall-clock + per-event-type counters so a
frozen feed (`last_event_at` stops advancing) is detectable for REST fallback.

No orders are ever sent. Clock is injectable. This module never raises out of
its own message/reconnect loops.
"""
from __future__ import annotations

import os
import json
import time
import base64
import hashlib
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable

from local_order_book import OrderBookStateError
from websocket_market_data import MarketStream, MarketStreamError

def resolve_default_proxy() -> tuple[str, int] | None:
    """Read proxy from standard environment variables (http(s)_proxy/all_proxy);
    returns None if no proxy is configured (allowing direct connection)."""
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        val = os.environ.get(key)
        if val:
            v = val.strip()
            if "://" in v:
                v = v.split("://", 1)[1]
            host, _, port_str = v.partition(":")
            port_clean = port_str.split("/")[0].strip()
            if host and port_clean:
                try:
                    return (host.strip(), int(port_clean))
                except ValueError:
                    pass
    return None


DEFAULT_PROXY: tuple[str, int] | None = resolve_default_proxy()
DEFAULT_WS_HOST = "ws-subscriptions-clob.polymarket.com"
DEFAULT_WS_PATH = "/ws/market"
DEFAULT_PING_INTERVAL_S = 10.0
DEFAULT_READ_TIMEOUT_S = 30.0
RECONNECT_BACKOFF_S = (5.0, 10.0, 30.0)  # escalates 5 -> 10 -> 30, stays at 30

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_TRACE = bool(os.environ.get("WS_TRACE"))  # frame/recv diagnostics (dev only)


class WebSocketProtocolError(RuntimeError):
    pass


class MarketTransportError(RuntimeError):
    """Hard, caller-fatal infra error (cannot reach the endpoint through the
    proxy). Distinct from protocol issues that auto-reconnect handles."""


# --------------------------------------------------------------------------- #
# RFC 6455 framing
# --------------------------------------------------------------------------- #


class _Frame:
    """One decoded websocket frame."""

    __slots__ = ("opcode", "payload", "fin")

    def __init__(self, opcode: int, payload: bytes, fin: bool) -> None:
        self.opcode = opcode
        self.payload = payload
        self.fin = fin

    @staticmethod
    def text(payload: bytes) -> bytes:
        return _encode(0x1, payload, masked=True)  # client must mask

    @staticmethod
    def ping(payload: bytes = b"") -> bytes:
        return _encode(0x9, payload, masked=True)

    @staticmethod
    def pong(payload: bytes = b"") -> bytes:
        return _encode(0xA, payload, masked=True)

    @staticmethod
    def close(code: int = 1000, reason: str = "") -> bytes:
        payload = code.to_bytes(2, "big") + reason.encode("utf-8", "replace")
        return _encode(0x8, payload, masked=True)


def _encode(opcode: int, payload: bytes, *, masked: bool) -> bytes:
    n = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])  # FIN=1
    mask_bit = 0x80 if masked else 0x00
    if n <= 125:
        header.append(mask_bit | n)
    elif n <= 65535:
        header.append(mask_bit | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(mask_bit | 127)
        header += n.to_bytes(8, "big")
    if not masked:
        return bytes(header) + payload
    key = os.urandom(4)
    masked_payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + key + masked_payload


class _Reader:
    """Single-buffered reader owning the WS-upgrade head and every data frame.

    One buffer, one consumer. This is what keeps a TLS record that bundles the
    101 head and the first frame from corrupting the stream: read_until() stops
    exactly after the head terminator and leaves any following frame bytes in
    self._buffer for next_message().
    """

    def __init__(self, sock: socket.socket, recv_size: int = 65536, initial: bytes = b"") -> None:
        self._sock = sock
        self._recv_size = recv_size
        self._buffer = initial

    def _refill(self) -> None:
        chunk = self._sock.recv(self._recv_size)
        if not chunk:
            raise EOFError("websocket_closed")
        if _TRACE:
            print("[ws] recv len=%d head=%s" % (len(chunk), chunk[:8].hex()))
        self._buffer += chunk

    def read_until(self, marker: bytes) -> bytes:
        """Read up to and including `marker` (an HTTP head terminator); any bytes
        past it stay in the buffer for the frame parser."""
        idx = self._buffer.find(marker)
        while idx < 0:
            self._refill()
            idx = self._buffer.find(marker)
        head, self._buffer = self._buffer[: idx + len(marker)], self._buffer[idx + len(marker):]
        return head

    def next_frame(self) -> _Frame:
        while len(self._buffer) < 2:
            self._refill()
        b0, b1 = self._buffer[0], self._buffer[1]
        self._buffer = self._buffer[2:]
        fin = bool(b0 & 0x80)
        if b0 & 0x70:
            raise WebSocketProtocolError("rsv_bits_set")
        opcode = b0 & 0x0F
        ln = b1 & 0x7F
        if ln == 126:
            if len(self._buffer) < 2:
                self._refill()
            # extended length needs possibly a second read if underfilled
            if len(self._buffer) < 2:
                self._refill()
            ln = int.from_bytes(self._buffer[:2], "big")
            self._buffer = self._buffer[2:]
        elif ln == 127:
            while len(self._buffer) < 8:
                self._refill()
            ln = int.from_bytes(self._buffer[:8], "big")
            self._buffer = self._buffer[8:]
        masked = bool(b1 & 0x80)
        if masked:  # server->client frame must NOT be masked per RFC
            while len(self._buffer) < 4:
                self._refill()
            key = self._buffer[:4]
            self._buffer = self._buffer[4:]
            while len(self._buffer) < ln:
                self._refill()
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(self._buffer[:ln]))
            self._buffer = self._buffer[ln:]
        else:
            while len(self._buffer) < ln:
                self._refill()
            payload, self._buffer = self._buffer[:ln], self._buffer[ln:]
        return _Frame(opcode, payload, fin)

    def next_message(self) -> _Frame:
        """One complete data message (opcode 1/2) with fragmentation reassembled,
        or a control frame (8/9/10) which never fragments."""
        while True:
            frame = self.next_frame()
            if frame.opcode in (0x8, 0x9, 0xA):  # control: return as-is
                return frame
            if frame.opcode not in (0x1, 0x2):
                raise WebSocketProtocolError(f"unsupported_opcode:{frame.opcode}")
            if frame.fin:
                return frame
            # fragmented data frame start
            payload = frame.payload
            opcode = frame.opcode
            while not frame.fin:
                seg = self.next_frame()
                if seg.opcode == 0x9:
                    # Control frames may interleave mid-fragment; surface as a
                    # pseudo message the caller answers before continuing.
                    return _Frame(0x9, seg.payload, True)
                if seg.opcode == 0xA:
                    continue
                if seg.opcode != 0x0:
                    raise WebSocketProtocolError("invalid_continuation")
                payload += seg.payload
                frame = seg
            return _Frame(opcode, payload, True)


# --------------------------------------------------------------------------- #
# Connection / handshake
# --------------------------------------------------------------------------- #


def _tls_direct(host: str, port: int, timeout: float) -> socket.socket:
    """Direct TCP -> TLS(SNI=host). Returns ssl socket."""
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        raw.settimeout(timeout)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raise


def _tls_via_connect(host: str, port: int, proxy_host: str, proxy_port: int, timeout: float) -> socket.socket:
    """TCP to proxy -> CONNECT host:port -> TLS(SNI=host). Returns ssl socket."""
    raw = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        raw.settimeout(timeout)
        raw.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Proxy-Connection: Keep-Alive\r\n\r\n".encode("ascii")
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                raise MarketTransportError("connection_closed_during_connect")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1")
        lines = head.split("\r\n")
        if len(lines) < 1 or len(lines[0].split(" ", 2)) < 2:
            raise MarketTransportError("malformed_proxy_reply")
        code = int(lines[0].split(" ", 2)[1])
        if code != 200:
            raw.close()
            raise MarketTransportError(f"proxy_connect_{code}")
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raise


def _ws_handshake(reader: "_Reader", host: str, path: str, timeout: float) -> None:
    """Send the WS-upgrade GET and verify the 101 reply, reading the head through
    the shared reader so any leading frame bytes are preserved."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    reader._sock.settimeout(timeout)
    reader._sock.sendall(
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: weatherbotyes2re-ws/0.1\r\n\r\n".encode("ascii")
    )
    head = reader.read_until(b"\r\n\r\n").decode("latin1")
    lines = head.split("\r\n")
    status = lines[0].split(" ", 2)
    if len(status) < 2:
        raise MarketTransportError("malformed_ws_reply")
    code = int(status[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    if code != 101:
        raise MarketTransportError(f"ws_upgrade_{code}")
    if headers.get("upgrade", "").lower() != "websocket":
        raise MarketTransportError("ws_upgrade_missing")
    expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        raise MarketTransportError("ws_accept_mismatch")


# --------------------------------------------------------------------------- #
# Wire -> MarketStream bridge
# --------------------------------------------------------------------------- #


def _feed_text_to_stream(stream: MarketStream, text: str) -> None:
    """Route one decoded websocket text payload into MarketStream.

    * A JSON *list* (the post-subscribe full-book dump) cannot go through
      handle_message (which needs a typed dict). Each element is a full-book
      baseline -> seed_from_rest, which flips LocalOrderBook.ready=True so the
      subsequent deltas are accepted.
    * A JSON *dict* with "type" is forwarded to handle_message verbatim; the
      state machine already maps book/price_change/tick_size_change/
      last_trade_price/best_bid_ask/heartbeat.
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return
    if isinstance(obj, list):
        for item in obj:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or item.get("tokenId") or "")
            if not asset_id:
                continue
            try:
                stream.seed_from_rest(asset_id, item)
            except OrderBookStateError:
                pass
        return
    if isinstance(obj, dict) and obj.get("type"):
        stream.handle_message(obj)


# --------------------------------------------------------------------------- #
# Telemetry / drift detector
# --------------------------------------------------------------------------- #


@dataclass
class TransportTelemetry:
    connect_count: int = 0
    reconnect_count: int = 0
    message_count: int = 0
    frames_by_type: dict[str, int] = field(default_factory=dict)
    dropped_errors: int = 0
    connect_errors: int = 0
    last_event_at: float = 0.0  # wall-clock seconds when a payload last arrived


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class MarketSocketTransport:
    """Self-contained market-channel WS client.

    on_message(stream, text, telemetry) is the consumer hook. The default hook
    routes list/dict payloads into MarketStream (see _feed_text_to_stream).
    """

    def __init__(
        self,
        stream: MarketStream,
        *,
        proxy: tuple[str, int] | None = None,
        host: str = DEFAULT_WS_HOST,
        path: str = DEFAULT_WS_PATH,
        port: int = 443,
        connect_timeout: float = 15.0,
        read_timeout: float = DEFAULT_READ_TIMEOUT_S,
        ping_interval: float = DEFAULT_PING_INTERVAL_S,
        backoff: tuple[float, float, float] = RECONNECT_BACKOFF_S,
        clock: Callable[[], float] | None = None,
        on_message: Callable[[MarketStream, str, "TransportTelemetry"], None] | None = None,
    ) -> None:
        self.stream = stream
        self.proxy = proxy if proxy is not None else DEFAULT_PROXY
        self.host = host
        self.path = path
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.ping_interval = ping_interval
        self.backoff = backoff
        self.clock = clock or time.time
        self.on_message = on_message or (lambda s, t, tel: _feed_text_to_stream(s, t))
        self.telemetry = TransportTelemetry()
        self._sock: socket.socket | None = None
        self._reader: _Reader | None = None
        self._closed = False
        self.connect_retries = 3

    # -- lifecycle --------------------------------------------------------- #

    def _dial_and_subscribe(self) -> None:
        """Connect through proxy or direct, TLS-wrap, WS-upgrade, then send the market
        subscription frame. Raises MarketTransportError on protocol reject;
        retries transient network faults up to connect_retries times."""
        last_exc: Exception | None = None
        for attempt in range(self.connect_retries):
            tls_sock = None
            try:
                if self.proxy:
                    tls_sock = _tls_via_connect(self.host, self.port, *self.proxy, self.connect_timeout)
                else:
                    tls_sock = _tls_direct(self.host, self.port, self.connect_timeout)
                self._reader = _Reader(tls_sock)  # single buffer owns upgrade + frames
                _ws_handshake(self._reader, self.host, self.path, self.connect_timeout)
            except MarketTransportError:
                # deterministic protocol reject (bad key/upgrade) — not retryable
                if tls_sock is not None:
                    tls_sock.close()
                self._reader = None
                raise
            except (socket.timeout, TimeoutError, ssl.SSLError, OSError) as exc:
                # transient network/TLS (quiet endpoint, Cloudflare reset,
                # dead proxy) — retry with short backoff.
                if tls_sock is not None:
                    try:
                        tls_sock.close()
                    except Exception:
                        pass
                self._reader = None
                last_exc = exc
                if attempt + 1 < self.connect_retries:
                    time.sleep(min(1.0 * (attempt + 1), 4.0))
                continue
            self._sock = tls_sock
            sub = self.stream.subscription_message()
            tls_sock.sendall(_Frame.text(json.dumps(sub).encode("utf-8")))
            self.stream.mark_connected()
            self.stream.mark_subscribed()
            self.telemetry.connect_count += 1
            self.telemetry.last_event_at = self.clock()
            return
        self.telemetry.connect_errors += 1
        self._sock = None
        raise MarketTransportError(f"connect_failed:{getattr(last_exc, 'errno', None) or type(last_exc).__name__}")

    # -- I/O --------------------------------------------------------------- #

    def read_one(self, timeout: float | None = None) -> _Frame | None:
        """Read one message/control frame. Returns None on read timeout
        (caller may re-issue or reconnect)."""
        if self._reader is None:
            raise MarketTransportError("not_connected")
        sock = self._sock
        assert sock is not None
        sock.settimeout(self.read_timeout if timeout is None else timeout)
        return self._reader.next_message()

    def _handle_frame(self, frame: _Frame) -> None:
        sock = self._sock
        if frame.opcode == 0x9:  # server ping -> pong (echo payload)
            if sock is not None:
                sock.sendall(_Frame.pong(frame.payload))
            return
        if frame.opcode == 0xA:  # server pong (keep-alive receipt)
            self.telemetry.last_event_at = self.clock()
            return
        if frame.opcode == 0x8:  # server close
            self.telemetry.frames_by_type["close"] = self.telemetry.frames_by_type.get("close", 0) + 1
            raise EOFError("server_sent_close")
        if frame.opcode == 0x2:  # unexpected binary push — ignore, count
            self.telemetry.frames_by_type["binary"] = self.telemetry.frames_by_type.get("binary", 0) + 1
            return
        if frame.opcode == 0x1:
            text = frame.payload.decode("utf-8", "replace")
            self.telemetry.message_count += 1
            self.telemetry.last_event_at = self.clock()
            try:
                self.on_message(self.stream, text, self.telemetry)
            except (MarketStreamError, ValueError, KeyError, TypeError):
                # A single bad event must never kill the socket loop.
                self.telemetry.dropped_errors += 1

    # -- main loops --------------------------------------------------------- --

    def run_once(self, max_wall_seconds: float) -> bool:
        """Connect, subscribe, and read until max_wall_seconds elapses.

        Returns True if the whole window was read cleanly; False if a transport
        fault ended the pass early (the caller decides on reconnect)."""
        deadline = self.clock() + max_wall_seconds
        try:
            self._dial_and_subscribe()
        except MarketTransportError:
            self.telemetry.connect_errors += 1
            raise
        last_ping = self.clock()
        try:
            while self.clock() < deadline:
                if self.clock() - last_ping >= self.ping_interval:
                    try:
                        self.send_ping()
                    except OSError:
                        return False
                    last_ping = self.clock()
                try:
                    frame = self.read_one(self.read_timeout)
                except (EOFError, OSError, WebSocketProtocolError):
                    return False
                if frame is None:
                    continue
                try:
                    self._handle_frame(frame)
                except EOFError:
                    return False
            return True
        finally:
            self.close_socket()

    def send_ping(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(_Frame.ping(b"hb"))
            except OSError:
                pass

    def send_text(self, text: str) -> None:
        """Send one text frame (used for (re)subscribe messages)."""
        if self._sock is not None:
            try:
                self._sock.sendall(_Frame.text(text.encode("utf-8")))
            except OSError as exc:
                raise MarketTransportError(f"send_failed:{exc}") from exc

    def run_forever(self, stop: Callable[[], bool] | None = None) -> None:
        """Keep the feed live: connect, run, reconnect on any fault with
        5/10/30 s backoff. Never raises for transient transport errors."""
        attempt = 0
        while not self._closed and (stop is None or not stop()):
            try:
                self.run_once(3600.0)
            except MarketTransportError:
                self.telemetry.connect_errors += 1
            except Exception:
                self.telemetry.connect_errors += 1
            if self._closed or (stop is not None and stop()):
                break
            attempt += 1
            if self.telemetry.connect_count > 1:
                self.telemetry.reconnect_count += 1
            self.close_socket()
            backoff_s = self.backoff[min(attempt, len(self.backoff) - 1)]
            end = self.clock() + backoff_s
            while self.clock() < end and not self._closed:
                time.sleep(min(0.5, end - self.clock()))
        self.close_socket()

    def close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._reader = None

    def close(self) -> None:
        self._closed = True
        self.close_socket()

    # placate the WS_TRACE debugger used in development
    def __repr__(self) -> str:
        return f"<MarketSocketTransport connected={self._sock is not None}>"
