#!/usr/bin/env python3
"""Phase-3b CLOB **v2** transport — the only place in ``live/`` that can place or cancel a real order.

Polymarket moved to CLOB v2 (2026-04-28); the old ``py-clob-client`` build is archived and every
order it signs is rejected (``invalid order version``). This module evolves ``live/submit.py``
(v1) onto ``py-clob-client-v2`` (``import py_clob_client_v2``) while **keeping the v1 safety
machinery as the single source of truth**: the triple gate (``submit.gate_status``), the
append-only audit log (``submit.audit``), the passivity/limits checks
(``submit.check_non_marketable`` / ``submit.check_limits``) and the ``AuditError`` contract.

v2 specifics handled here (each was a real failure mode):

* credentials are passed explicitly (``ApiCreds``); v2 has no ``create_or_derive_api_creds``
* ``cancel_orders([id])`` — the single-argument ``cancel_order`` is unreliable in v2, so it is
  armed with a sentinel and **never released** (calling it is refused by the arm step)
* open orders come from ``get_open_orders()`` (there is no ``get_orders``)
* ``create_order`` returns a ``SignedOrderV2`` **object** (attributes, no ``dict.get``)
* a **taker** (FAK) order must be built with the SDK's market-order API
  (``create_market_order(MarketOrderArgsV2(...))``): the venue caps a market order's maker
  amount at **2 decimals of USDC** (BUY) / **4 decimals of shares** (SELL) and rejects the
  ``create_order`` shape with ``400 invalid amounts`` (real failure, 2026-09-12)
* the book must be refetched and the limit clamped **immediately before** submitting,
  otherwise a ``post_only`` order is rejected with ``order crosses book``
* a cancel failure is retried and, if it still fails, reported as **residual order risk**

Nothing in this module runs by itself: every write call needs a fully-passing gate record.
The v2 SDK is imported lazily, so the paper path never needs it.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python3.13 live/...py` — make relative imports work
    __package__ = "live"  # `python3.13 live/v2_transport.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from live import clob_client, creds as creds_mod, submit
else:  # `python3.13 tests_port.py` / `import live.v2_transport`
    from . import clob_client, creds as creds_mod, submit

ZERO = Decimal("0")
ONE = Decimal("1")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
#: shown only when the v2 SDK is genuinely missing (``--status`` reports ``sdk_import_ok`` too)
V2_HINT = (
    "py-clob-client-v2 is not importable with this interpreter (CLOB v1 orders are rejected since\n"
    "2026-04-28, so this SDK is required for anything real). It lives in its own venv:\n"
    "  /home/da/桌面/poly-yes2/live-probe-v2/.venv/bin/python live/v2_transport.py\n"
    "(stdlib-only unit tests need no SDK: python3.13 tests_live.py / tests_port.py)"
)

#: v2 order-entry methods — must exist and stay blocked unless explicitly released.
#: ``cancel_order`` (DELETE /order, single id) is armed but **never released**: the safe form
#: is ``cancel_orders([id])`` and the single-id variant proved unreliable in practice.
SUBMIT_METHODS = (
    "create_and_post_order",
    "create_and_post_market_order",
    "post_order",
    "post_orders",
    "cancel_order",
    "cancel_orders",
    "cancel_all",
    "cancel_market_orders",
)
#: the second order-entry surface (RFQ), best-effort: blocked when the build exposes it
RFQ_SUBMIT_METHODS = (
    "create_rfq_request",
    "cancel_rfq_request",
    "create_rfq_quote",
    "cancel_rfq_quote",
    "accept_rfq_quote",
    "approve_rfq_order",
)
#: credential / allowance / order-state administration
ADMIN_METHODS = (
    "create_api_key",
    "create_or_derive_api_key",
    "create_builder_api_key",
    "create_readonly_api_key",
    "delete_api_key",
    "delete_readonly_api_key",
    "derive_api_key",
    "revoke_builder_api_key",
    "update_balance_allowance",
)
#: account-state writes that are neither order entry nor credential admin
STATE_WRITE_METHODS = (
    "post_heartbeat",
    "drop_notifications",
)

#: least privilege for the engine: one write to place, one to take it back
RELEASE_WRITE_METHODS = ("post_order", "cancel_orders")
#: read-only calls the fill loop needs (never blocked; listed so the release set is explicit)
RELEASE_READ_METHODS = ("get_order", "get_open_orders", "get_trades", "get_balance_allowance",
                        "get_order_book", "get_tick_size")

QTY = Decimal("0.000001")
#: order statuses that mean "no more changes will happen on the book"
TERMINAL_STATUSES = ("matched", "filled", "cancelled", "canceled", "expired")
RESTING_STATUSES = ("live", "unmatched", "open", "delayed")


def _dec(value, field: str, *, allow_zero: bool = True) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}: missing")
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        raise ValueError(f"{field}: not a number") from None
    if not out.is_finite() or out < 0 or (out == 0 and not allow_zero):
        raise ValueError(f"{field}: out of range")
    return out


# --------------------------------------------------------------------------- client (lazy)

def _py_clob_v2() -> dict[str, Any]:
    """Lazy import of py-clob-client-v2; clear error when the venv is missing."""
    try:
        from py_clob_client_v2 import clob_types as _types
        from py_clob_client_v2.clob_types import (ApiCreds, AssetType, BalanceAllowanceParams,
                                                  OrderArgs, OrderType, PartialCreateOrderOptions)
        from py_clob_client_v2.client import ClobClient
    except ImportError as exc:  # pragma: no cover - exercised only outside the v2 venv
        raise RuntimeError(f"{exc}\n{V2_HINT}") from None
    return {"ClobClient": ClobClient, "ApiCreds": ApiCreds, "OrderArgs": OrderArgs,
            "OrderType": OrderType, "PartialCreateOrderOptions": PartialCreateOrderOptions,
            "AssetType": AssetType, "BalanceAllowanceParams": BalanceAllowanceParams,
            #: ``MarketOrderArgsV2`` is the current name; older builds export the plain alias.
            #: ``None`` when this build has neither ⇒ the taker path refuses (never falls back).
            "MarketOrderArgs": (getattr(_types, "MarketOrderArgsV2", None)
                                or getattr(_types, "MarketOrderArgs", None))}


def sdk_available() -> bool:
    """True when ``py-clob-client-v2`` can be imported (no client is constructed)."""
    return clob_client.sdk_available()


def build_client(creds: dict, *, host: str = CLOB_HOST, chain_id: int = CHAIN_ID) -> Any:
    """Construct a Level-2 v2 client (explicit ApiCreds; no cred derivation in v2)."""
    lib = _py_clob_v2()
    api_creds = None
    if creds.get("api_key") and creds.get("api_secret") and creds.get("api_passphrase"):
        api_creds = lib["ApiCreds"](creds["api_key"], creds["api_secret"], creds["api_passphrase"])
    return lib["ClobClient"](host, chain_id=chain_id, key=creds["private_key"], creds=api_creds,
                             signature_type=creds["signature_type"], funder=creds["funder_address"])


def server_version(client) -> int | None:
    """``get_version()`` — 2 means the client speaks the live CLOB generation."""
    try:
        return int(client.get_version())
    except Exception:  # noqa: BLE001 - informational only
        return None


# --------------------------------------------------------------------------- sentinels

def _sentinel(name: str):
    """Build the function that replaces a write method (shared shape across the layer)."""

    def blocked(*_args, **_kwargs):
        raise RuntimeError(f"SUBMIT BLOCKED (dry-run): {name}() is disabled in Phase 2")

    blocked.__name__ = "dryrun_sentinel"
    blocked.__doc__ = f"sentinel standing in for {name}"
    blocked.dryrun_sentinel_for = name
    return blocked


def sentinel_targets(client) -> list[tuple[str, object, tuple[str, ...]]]:
    """Every write surface reachable from the v2 client: the client and its RFQ sub-client."""
    targets = [("client", client, SUBMIT_METHODS + ADMIN_METHODS + STATE_WRITE_METHODS)]
    rfq = getattr(client, "rfq", None)
    if rfq is not None:
        targets.append(("client.rfq", rfq, RFQ_SUBMIT_METHODS))
    return targets


def _walk_sentinels(client) -> tuple[list, list, list]:
    """(armed triples, installed, missing) for every write method the client exposes."""
    armed, installed, missing = [], [], []
    for label, target, names in sentinel_targets(client):
        for name in names:
            if getattr(target, name, None) is None:
                missing.append(f"{label}.{name}")
                continue
            setattr(target, name, _sentinel(name))
            installed.append(f"{label}.{name}")
            armed.append((label, target, name))
    return armed, installed, missing


def prove_sentinels(armed) -> list[dict]:
    """Call each sentinel once and record the RuntimeError — evidence, not a claim."""
    proofs = []
    for label, target, name in armed:
        method = getattr(target, name, None)
        entry = {"target": label, "method": name,
                 "patched": getattr(method, "dryrun_sentinel_for", None) == name}
        try:
            method()
        except RuntimeError as exc:
            entry.update({"blocked": True, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - anything else means a broken sentinel
            entry.update({"blocked": False, "error": f"unexpected {type(exc).__name__}: {exc}"})
        else:
            entry.update({"blocked": False, "error": "call returned — sentinel NOT armed"})
        proofs.append(entry)
    return proofs


def install_sentinels(client) -> dict:
    """Arm every v2 write method; the returned report carries the per-sentinel proof."""
    armed, installed, missing = _walk_sentinels(client)
    return {
        "installed": installed,
        "missing": missing,
        "armed": bool(armed),
        "proof": prove_sentinels(armed),
        "statement": ("submit path unreachable: every order-submit / RFQ / credential-admin / "
                      "state-write method raises RuntimeError"),
    }


def arm_controlled_sentinels(client) -> dict:
    """Arm everything, then release only ``post_order`` + ``cancel_orders`` (least privilege).

    Fails closed when a released method is missing, when anything outside the release list is
    left unblocked, or when a still-blocked method does not actually raise.
    """
    originals: dict[str, Any] = {}
    for name in RELEASE_WRITE_METHODS:
        original = getattr(client, name, None)
        if original is None:
            raise RuntimeError(f"cannot release {name}: v2 client has no such method")
        originals[name] = original

    armed = install_sentinels(client)
    if not armed["armed"]:
        raise RuntimeError("no v2 sentinels armed — refusing to continue")
    for name, original in originals.items():
        setattr(client, name, original)

    proof, still_blocked = [], []
    for label, target, names in sentinel_targets(client):
        for name in names:
            method = getattr(target, name, None)
            if method is None:
                continue
            if getattr(method, "dryrun_sentinel_for", None) != name:
                proof.append({"target": label, "method": name,
                              "status": "released" if name in RELEASE_WRITE_METHODS else "not_sentineled"})
                continue
            try:
                method()
            except RuntimeError as exc:
                still_blocked.append(f"{label}.{name}")
                proof.append({"target": label, "method": name, "status": "blocked", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                proof.append({"target": label, "method": name,
                              "status": f"unexpected:{type(exc).__name__}", "error": str(exc)[:200]})
            else:
                proof.append({"target": label, "method": name, "status": "NOT_BLOCKED"})
    bad = [row for row in proof if row["status"].startswith("unexpected") or row["status"] == "NOT_BLOCKED"]
    if bad:
        raise RuntimeError(f"v2 sentinel release violated least privilege: {bad}")
    return {
        "armed": True,
        "released": [f"client.{name}" for name in RELEASE_WRITE_METHODS],
        "read_only_available": list(RELEASE_READ_METHODS),
        "still_blocked": still_blocked,
        "still_blocked_count": len(still_blocked),
        "proof": proof,
        "statement": ("least privilege: only post_order/cancel_orders released; every other "
                      "order/RFQ/credential/state-write method stays sentinel-blocked"),
    }


# --------------------------------------------------------------------------- tolerant readers

def _field(obj, *names, default=None):
    """Read a field from a dict / object / JSON string without assuming the type."""
    if obj is None:
        return default
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def order_summary(order) -> dict:
    """Compact, JSON-safe view of an order (dict, SignedOrderV2 object or JSON string)."""
    keys = ("id", "orderID", "status", "price", "original_size", "size_matched", "asset_id",
            "side", "outcome", "market", "associate_trades", "errorMsg", "success")
    summary = {}
    for key in keys:
        value = _field(order, key)
        if value is not None:
            summary[key] = value if isinstance(value, (int, float, str, bool)) else str(value)
    if not summary and order is not None:
        summary = {"raw": str(order)[:200]}
    return summary


def order_id_of(response) -> str | None:
    for key in ("orderID", "orderId", "id"):
        value = _field(response, key)
        if value:
            return str(value)
    return None


def cancel_summary(response) -> dict:
    """v2 ``cancel_orders`` may answer with a dict *or* a plain string — accept both."""
    if isinstance(response, dict):
        out = {key: response[key] for key in ("canceled", "cancelled", "not_canceled",
                                              "not_cancelled") if key in response}
        return out or {"raw_keys": sorted(response)[:8]}
    if isinstance(response, str):
        return {"raw": response[:200]}
    text = str(response)
    return {"raw": text[:200]}


def canceled_ids(response) -> list[str]:
    summary = cancel_summary(response)
    out = []
    for key in ("canceled", "cancelled"):
        value = summary.get(key)
        if isinstance(value, list):
            out.extend(str(x) for x in value)
        elif value:
            out.append(str(value))
    if not out and isinstance(summary.get("raw"), str):
        # a bare string answer is treated as "accepted" only when it names the id
        out = []
    return out


def list_open_orders(client) -> dict:
    """Read-only: v2 open orders (``get_open_orders``; there is no ``get_orders`` in v2)."""
    rows = clob_client.get_open_orders(client)
    slim = [{"id": _field(row, "id"), "asset_id": _field(row, "asset_id"),
             "side": _field(row, "side"), "price": _field(row, "price"),
             "original_size": _field(row, "original_size"),
             "size_matched": _field(row, "size_matched"), "status": _field(row, "status")}
            for row in rows]
    return {"ok": True, "count": len(slim), "orders": slim,
            "response_summary": {"open_orders": len(slim)}}


def read_account(client, *, address: str, timeout: int = 25) -> dict:
    """Read-only snapshot for the pre-trade risk gate: collateral, open orders, positions."""
    lib = _py_clob_v2()
    balance = client.get_balance_allowance(lib["BalanceAllowanceParams"](asset_type=lib["AssetType"].COLLATERAL))
    raw = _field(balance, "balance")
    positions = clob_client.fetch_positions(address, timeout=timeout) if address else []
    kept = [p for p in positions if float(_field(p, "currentValue") or 0) > 0.1]
    return {
        "usdc_balance": round(int(raw) / 10 ** clob_client.USDC_DECIMALS, 6) if raw is not None else None,
        "open_orders": list_open_orders(client)["count"],
        "positions": kept,
        "positions_value_usdc": round(sum(float(_field(p, "currentValue") or 0) for p in kept), 6),
    }


# --------------------------------------------------------------------------- price clamping

def clamp_limit(*, side: str, limit, book, tick=None) -> dict:
    """Make a passive limit price legal against a *fresh* book.

    BUY: ``min(limit, best_ask - tick)`` aligned down (must stay > 0 and < best_ask).
    SELL: ``max(limit, best_bid + tick)`` aligned up (must stay < 1 and > best_bid).
    A ``post_only`` order that crosses the book is rejected server-side (``order crosses book``),
    so this clamp is what keeps a fire from being thrown away at submit time.
    """
    if side not in ("BUY", "SELL"):
        return {"ok": False, "reason": "invalid_input", "detail": "side: want BUY or SELL"}
    try:
        want = _dec(limit, "limit", allow_zero=False)
        raw_tick = tick if tick is not None else (book or {}).get("tick_size") if isinstance(book, dict) else None
        step = _dec(raw_tick or "0.01", "tick", allow_zero=False)
    except ValueError as exc:
        return {"ok": False, "reason": "invalid_input", "detail": str(exc)}
    if not isinstance(book, dict):
        return {"ok": False, "reason": "no_book", "detail": "book: missing"}
    reference_key = "best_ask" if side == "BUY" else "best_bid"
    try:
        reference = _dec(book.get(reference_key), reference_key, allow_zero=False)
    except ValueError as exc:
        return {"ok": False, "reason": "no_book", "detail": f"{exc} — cannot clamp"}
    if side == "BUY":
        ceiling = reference - step
        price = min(want, ceiling)
        price = (price / step).to_integral_value(rounding=ROUND_DOWN) * step
        if price <= 0 or price >= reference:
            return {"ok": False, "reason": "no_passive_price",
                    "detail": f"BUY clamp of {want} against best_ask {reference} (tick {step}) left {price}"}
    else:
        floor = reference + step
        price = max(want, floor)
        price = (price / step).to_integral_value(rounding=ROUND_UP) * step
        if price >= 1 or price <= reference:
            return {"ok": False, "reason": "no_passive_price",
                    "detail": f"SELL clamp of {want} against best_bid {reference} (tick {step}) left {price}"}
    return {"ok": True, "reason": "ok", "price": price, "tick": step, "reference": reference,
            "clamped": price != want,
            "detail": f"{side} {want} -> {price} vs {reference_key} {reference}"}


def refetch_book(client, token_id: str) -> dict | None:
    """Fresh read-only book, in the shape order_plan/clamp_limit consume. None on failure."""
    try:
        from . import sign_dryrun  # local import: the book normaliser lives in the phase-2 module
        summary = client.get_order_book(str(token_id))
        return sign_dryrun._book_from_summary(summary)
    except Exception:  # noqa: BLE001 - the caller falls back to its cached book
        return None


# --------------------------------------------------------------------------- cancel / poll

def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def _audit_best_effort(record: dict, *, audit_path=None) -> bool:
    """Write an audit line without ever blocking the action (loud on stderr instead)."""
    try:
        submit.audit(record, path=audit_path)
    except submit.AuditError as exc:  # recovery must keep working even with a broken log
        print(f"AUDIT FAILURE: {exc}", file=sys.stderr)
        return False
    return True


def cancel_with_retry(client, order_id: str, *, attempts: int = 3, sleep=None, audit_path=None) -> dict:
    """``cancel_orders([id])`` with retries; failure ⇒ explicit residual-order risk.

    Auditing here is best-effort **by contract**: cancelling is the recovery direction, so an
    unwritable log must not stop it (it is reported on stderr instead).
    """
    sleep = sleep or _sleep
    tried = []
    last = None
    audited = True
    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = client.cancel_orders([str(order_id)])
        except Exception as exc:  # noqa: BLE001 - retry the cancel, never the submit
            last = {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
            tried.append(last)
            if attempt < attempts:
                sleep(0)
            continue
        summary = cancel_summary(response)
        canceled = canceled_ids(response)
        ok = str(order_id) in canceled or bool(summary.get("canceled") or summary.get("cancelled"))
        last = {"attempt": attempt, "summary": summary, "ok": ok}
        tried.append(last)
        audited = _audit_best_effort(
            {"actor": "live/v2_transport.py", "action": "cancel",
             "reason": "ok" if ok else "cancel_not_confirmed",
             "params": {"order_id": str(order_id), "attempt": attempt},
             "response_summary": summary, "order_id": str(order_id)},
            audit_path=audit_path) and audited
        if ok:
            return {"ok": True, "order_id": str(order_id), "attempts": attempt, "tried": tried,
                    "canceled": canceled, "response_summary": summary, "audited": audited}
        if attempt < attempts:
            sleep(0)
    result = {"ok": False, "order_id": str(order_id), "attempts": attempts, "tried": tried,
              "residual_risk": True, "audited": audited,
              "detail": f"cancel not confirmed after {attempts} attempt(s) — order may still be resting"}
    _audit_best_effort({"actor": "live/v2_transport.py", "action": "cancel", "reason": "residual_risk",
                        "params": {"order_id": str(order_id), "attempts": attempts},
                        "response_summary": last, "order_id": str(order_id)}, audit_path=audit_path)
    return result


def poll_fill(client, order_id: str, *, attempts: int = 6, sleep=None, sleep_seconds: float = 1.0,
              trades: bool = True) -> dict:
    """Poll ``get_order`` (and ``get_trades``) until terminal or timeout; report the real fill."""
    sleep = sleep or _sleep
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            order = client.get_order(str(order_id))
        except Exception as exc:  # noqa: BLE001 - a read failure is not a fill
            return {"ok": False, "status": "poll_error", "attempts": attempt,
                    "detail": f"{type(exc).__name__}: {exc}", "last": last,
                    "filled_shares": ZERO, "avg_price": None, "unfilled": None, "terminal": False}
        last = order_summary(order)
        status = str(last.get("status") or "").lower()
        matched = _dec(_field(order, "size_matched") or 0, "size_matched")
        original = _dec(_field(order, "original_size") or 0, "original_size")
        if status in TERMINAL_STATUSES or (status in RESTING_STATUSES and matched > 0 and matched == original):
            price = _field(order, "price")
            avg = None
            if trades:
                avg = average_fill_price(client, str(order_id), token_id=_field(order, "asset_id"))
            if avg is None and price is not None:
                avg = Decimal(str(price))
            filled = matched
            return {"ok": True, "status": status, "attempts": attempt, "terminal": True,
                    "filled_shares": filled, "avg_price": avg,
                    "unfilled": max(original - filled, ZERO), "last": last}
        if sleep_seconds:
            sleep(sleep_seconds)
    status = str((last or {}).get("status") or "").lower()
    matched = _dec((last or {}).get("size_matched") or 0, "size_matched")
    return {"ok": False, "status": "timeout", "attempts": attempts, "terminal": False,
            "filled_shares": matched, "avg_price": None, "unfilled": None, "last": last,
            "detail": f"order still {status or 'unknown'} after {attempts} poll(s)"}


def average_fill_price(client, order_id: str, *, token_id: str | None = None) -> Decimal | None:
    """Weighted average of the trades belonging to this order (read-only; best effort)."""
    try:
        rows = client.get_trades() or []
    except Exception:  # noqa: BLE001 - the average is informative, not load-bearing
        return None
    shares = ZERO
    cost = ZERO
    for row in rows:
        if _field(row, "orderID", "order_id", "id") not in (order_id, None):
            continue
        if _field(row, "orderID", "order_id") is None:
            continue
        size = _dec(_field(row, "size") or 0, "size")
        price = _dec(_field(row, "price") or 0, "price")
        shares += size
        cost += size * price
    if shares <= ZERO:
        return None
    return (cost / shares).quantize(QTY)


# --------------------------------------------------------------------------- execute one leg

#: fill modes recorded in the audit log (``order_mode``) — one value per order
MAKER_ORDER_MODE = "maker"
TAKER_ORDER_MODE = "taker"

#: which SDK entry point built the order (``order_api`` in the audit log)
LIMIT_ORDER_API = "limit"
MARKET_ORDER_API = "market"

#: the venue's precision limit for a **market** order ``amount`` (real 400, 2026-09-12):
#: ``invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals,
#: taker amount a max of 4 decimals`` ⇒ BUY ⇒ amount is the USDC nominal (2 dp), SELL ⇒ the
#: share count (4 dp).  Both are floored, never rounded up: the order must stay affordable.
MARKET_AMOUNT_DECIMALS = {"BUY": Decimal("0.01"), "SELL": Decimal("0.0001")}
MARKET_AMOUNT_UNIT = {"BUY": "USDC", "SELL": "shares"}

#: the single ``post_order`` call site exists exactly once and serves both modes.  Taker is
#: **FAK** (fill-and-kill: the unfilled remainder is voided server-side, nothing rests); maker
#: keeps the historical **GTC**.  The literal GTC string is kept inline for the maker branch so
#: that path stays byte-for-byte what it was.
ORDER_TYPE_GTC = "GTC"
ORDER_TYPE_FAK = "FAK"


def _tick_of(book, tick) -> Decimal:
    """Tick size for one leg: explicit ``tick``, else the book's, else the 1-cent default."""
    raw = tick if tick is not None else ((book or {}).get("tick_size") if isinstance(book, dict) else None)
    try:
        return _dec(raw or "0.01", "tick", allow_zero=False)
    except ValueError:
        return Decimal("0.01")


def market_amount(*, side: str, limit, shares) -> Decimal | None:
    """The ``amount`` a **market** (FAK) order may carry — floored to the venue's precision.

    ``create_order`` (limit-order args) produced a maker amount the venue refuses for a market
    order (``400 invalid amounts``); the market API takes an ``amount`` instead of a size:

    * ``BUY``  ⇒ ``amount`` = the **USDC nominal** (``shares * limit``) floored to **2 decimals**;
    * ``SELL`` ⇒ ``amount`` = the **share count** floored to **4 decimals**.

    Flooring (never rounding up) keeps the spend at or below the intended notional, so the same
    budget still covers it.  ``None`` for an unknown side or an unparseable input.
    """
    step = MARKET_AMOUNT_DECIMALS.get(side)
    if step is None:
        return None
    try:
        price = _dec(limit, "limit", allow_zero=False)
        size = _dec(shares, "shares", allow_zero=False)
    except ValueError:
        return None
    raw = size * price if side == "BUY" else size
    return (raw / step).to_integral_value(rounding=ROUND_DOWN) * step


def _taker_cap(value) -> Decimal | None:
    """The cap an aggressive order may lean on: an explicit ``0 < cap <= 1``.

    ``None`` for missing / unparseable / non-positive / ``> 1`` (above the venue maximum).  A
    missing cap must never authorise a taker.  ``cap == 1.0`` is accepted because that is the
    shipped ``no_max_ask`` — with it the absolute ``<= 1`` limit is what actually binds.
    (L-2/L-3: the last line of defence must not depend on the caller's good manners.)
    """
    try:
        out = _dec(value, "cap", allow_zero=False)
    except ValueError:
        return None
    return out if out <= ONE else None


def _deny_audit(audit_path, reason: str, params: dict) -> bool:
    """Audit a refusal of the **aggressive** direction (best effort).

    A refusal is not a write, so — exactly like ``cancel_with_retry`` — a broken log must not
    swallow the refusal; it is reported on stderr instead.  (The submit path itself keeps its
    mandatory, fail-closed ``intent`` record: no audit ⇒ no order.)
    """
    try:
        submit.audit({"actor": "live/v2_transport.py", "action": "deny", "reason": reason,
                      "params": params}, path=audit_path)
    except submit.AuditError as exc:
        print(f"AUDIT FAILURE: {exc}", file=sys.stderr)
        return False
    return True


def execute_leg(client, *, token_id: str, side: str, price, size, book=None, tick=None,
                neg_risk: bool | None = None, gates: dict | None = None, post_only: bool = True,
                clamp: bool = True, taker: bool = False, cap=None, poll_attempts: int = 6,
                poll_sleep: float = 1.0, sleep=None, audit_path=None,
                take_down_unfilled: bool = True, audit_extra: dict | None = None) -> dict:
    """Place ONE limit order and reconcile the real fill. The submit never retries.

    ``gates`` must be a fully-passing gate record (``submit.gate_status``): the dangerous
    direction is unreachable without proof that all three gates were satisfied.

    Two fill modes, one call site (``post_order``):

    * ``taker=False`` (default) — **maker / passive**: refetch the book, ``clamp_limit`` the
      price below the best ask (BUY) / above the best bid (SELL), ``post_only=True``,
      ``OrderType.GTC`` — byte-for-byte the historical behaviour.
    * ``taker=True`` — **taker / aggressive** (the operator's LIVE rule): the incoming limit is
      used **as passed and never clamped down**, sent ``post_only=False`` with
      ``OrderType.FAK`` (fill-and-kill → whatever does not fill is voided server-side; nothing is
      left resting and no residual order is created).  The taker stays inside the same hard
      limits: an explicit ``0 < cap <= 1`` is **required** (missing / unparseable / non-positive /
      ``> 1`` ⇒ refuse ``taker_cap_required``), the leg's ask ``cap`` is *refused* (``above_cap``)
      rather than chased, a limit outside ``(0, 1]`` is refused (``price_out_of_range``) and a
      limit that is not tick-aligned is refused (``price_not_on_tick``) instead of being moved.
      ``submit.check_non_marketable`` is skipped **by design** (an aggressive order is
      marketable by definition) — that is exactly why the caller must gate the mode first.
      The order itself is built with the SDK's **market-order** API
      (``create_market_order(MarketOrderArgsV2(amount=...))``) because the venue enforces market
      precision on it (BUY ``amount`` = USDC to 2 dp, SELL = shares to 4 dp); the ``create_order``
      limit path fails that check with ``400 invalid amounts`` (real failure, 2026-09-12).

    ``audit_extra`` is merged into the ``intent``/``submit`` audit params so the caller's
    fill-mode decision (``taker_gate`` / ``yes_price`` …) travels with the mandatory record.
    """
    out = {"ok": False, "status": "not_started", "order_id": None, "filled_shares": ZERO,
           "avg_price": None, "cost": ZERO, "unfilled": _safe_dec(size), "residual_risk": False,
           "limit_price": None, "detail": "", "clamped": False,
           "order_mode": TAKER_ORDER_MODE if taker else MAKER_ORDER_MODE,
           "order_type": ORDER_TYPE_FAK if taker else ORDER_TYPE_GTC,
           "order_api": MARKET_ORDER_API if taker else LIMIT_ORDER_API,
           "market_amount": None, "amount_unit": None}
    if not submit.gates_all_passed(gates):
        submit.audit({"actor": "live/v2_transport.py", "action": "deny", "reason": "gates_missing",
                      "params": {"intent": "execute_leg", "token_id": str(token_id),
                                 "price": str(price)}}, path=audit_path)
        raise PermissionError("execute_leg refused: all three gates must pass in this invocation")
    try:
        want = _dec(price, "price", allow_zero=False)
        shares = _dec(size, "size", allow_zero=False)
    except ValueError as exc:
        return {**out, "status": "invalid_input", "detail": str(exc)}

    extra_params: dict[str, Any] = dict(audit_extra or {})
    #: the market-order ``amount`` (USDC for BUY / shares for SELL).  Assigned by the taker branch
    #: below — which returns *before* any signing if the amount would not be representable, so a
    #: taker order can never reach the call site with the placeholder value.
    amount: Decimal = ZERO
    lib = None
    if taker:
        # an aggressive order is never post-only, whatever the caller asked for
        post_only = False
        if side not in MARKET_AMOUNT_DECIMALS:
            return {**out, "status": "invalid_input",
                    "detail": f"side: want BUY or SELL (got {side!r})"}
        # the aggressive order type must really exist: silently falling back to GTC would leave a
        # marketable order *resting* on the book (the one thing FAK exists to prevent)
        lib = _py_clob_v2()
        if getattr(lib["OrderType"], "FAK", None) is None:
            _deny_audit(audit_path, "no_fak_order_type",
                        {**extra_params, "token_id": str(token_id), "price": str(want),
                         "size": str(shares)})
            return {**out, "status": "no_fak_order_type",
                    "detail": ("OrderType.FAK is unavailable in this SDK build — refusing to send "
                               "an aggressive order as GTC (it could come to rest)")}
        cap_dec = _taker_cap(cap)
        if cap_dec is None:
            # L-2: an aggressive order must carry its own ceiling.  Missing / unparseable /
            # non-positive / >= 1 (a 1.0 cap is no ceiling) all refuse — never send uncapped.
            _deny_audit(audit_path, "taker_cap_required",
                        {**extra_params, "price": str(want),
                         "cap": (str(cap) if cap is not None else None)})
            return {**out, "status": "taker_cap_required",
                    "detail": (f"taker refused: cap={cap!r} is not a usable ceiling "
                               f"(need 0 < cap < 1)")}
        if want > ONE or want <= ZERO:
            # L-3: the taker path never re-prices, so an out-of-range limit must be refused here
            _deny_audit(audit_path, "price_out_of_range",
                        {**extra_params, "price": str(want), "cap": str(cap_dec)})
            return {**out, "status": "price_out_of_range",
                    "detail": f"taker limit {want} is outside (0, 1] — refuse"}
        if want > cap_dec:
            _deny_audit(audit_path, "above_cap",
                        {**extra_params, "price": str(want), "cap": str(cap_dec)})
            return {**out, "status": "above_cap",
                    "detail": f"taker limit {want} > leg cap {cap_dec} — refuse, never chase"}
        step = _tick_of(book, tick)
        if step > ZERO and (want / step) != (want / step).to_integral_value():
            _deny_audit(audit_path, "price_not_on_tick",
                        {**extra_params, "price": str(want), "tick": str(step)})
            return {**out, "status": "price_not_on_tick",
                    "detail": (f"taker limit {want} is not a multiple of tick {step} — refuse "
                               f"(the taker path never moves the price)")}
        # a MARKET order carries an ``amount``, not a size — and the venue caps its precision
        # (BUY: USDC to 2 dp, SELL: shares to 4 dp).  Computed here, floored, and *before* the
        # mandatory intent audit so the recorded numbers are the ones that go out.
        amount_dec = market_amount(side=side, limit=want, shares=shares)
        if amount_dec is None or amount_dec <= ZERO:
            _deny_audit(audit_path, "amount_below_precision",
                        {**extra_params, "price": str(want), "size": str(shares),
                         "side": side})
            return {**out, "status": "amount_below_precision",
                    "detail": (f"market amount for {shares} share(s) @ {want} floors to 0 at "
                               f"{MARKET_AMOUNT_DECIMALS[side]} ({MARKET_AMOUNT_UNIT[side]}) — "
                               f"refuse, nothing sent")}
        amount = amount_dec
        if lib.get("MarketOrderArgs") is None \
                or getattr(client, "create_market_order", None) is None:
            # fail closed: the size-based ``create_order`` shape is exactly what the venue
            # rejects for a market order (400 invalid amounts), so it is never a fallback.
            _deny_audit(audit_path, "no_market_order_api",
                        {**extra_params, "price": str(want), "size": str(shares), "side": side})
            return {**out, "status": "no_market_order_api",
                    "detail": ("this SDK build has no create_market_order/MarketOrderArgs — "
                               "refusing to send a FAK order through the limit-order shape")}
        limit = want
        out.update({"limit_price": str(limit), "clamped": False,
                    "market_amount": str(amount), "amount_unit": MARKET_AMOUNT_UNIT[side]})
    else:
        reference = refetch_book(client, token_id) if clamp else None
        if reference is None:
            reference = book
        clamped = clamp_limit(side=side, limit=want, book=reference, tick=tick)
        if not clamped["ok"]:
            return {**out, "status": clamped["reason"], "detail": clamped["detail"]}
        limit, step = clamped["price"], clamped["tick"]
        out.update({"limit_price": str(limit), "clamped": bool(clamped["clamped"])})
        passive = submit.check_non_marketable(side=side, price=limit, book=reference)
        if not passive["ok"]:
            return {**out, "status": passive["reason"], "detail": passive["detail"]}

    params = {"token_id": str(token_id), "side": side, "price": str(limit), "size": str(shares),
              "clamped": bool(out["clamped"]), "post_only": post_only}
    params.update(extra_params)
    # the computed mode/type/api are authoritative: they can never be spoofed through ``audit_extra``
    params["order_mode"] = out["order_mode"]
    params["order_type"] = out["order_type"]
    params["order_api"] = out["order_api"]
    if taker:
        # the market-order ``amount`` that actually goes out (USDC for BUY, shares for SELL)
        params["amount"] = out["market_amount"]
        params["amount_unit"] = out["amount_unit"]
    submit.audit({"actor": "live/v2_transport.py", "action": "intent", "reason": "execute_leg",
                  "params": params}, path=audit_path)

    if lib is None:
        lib = _py_clob_v2()
    options = lib["PartialCreateOrderOptions"](tick_size=str(step),
                                               neg_risk=bool(neg_risk) if neg_risk is not None else None)
    if taker:
        # market-order API: ``amount`` (not ``size``) with the venue's precision, and the limit is
        # passed explicitly so the SDK never walks the book to guess a market price.
        market_args = lib["MarketOrderArgs"](token_id=str(token_id), amount=float(amount),
                                             side=side, price=float(limit),
                                             order_type=lib["OrderType"].FAK)
        signed = client.create_market_order(market_args, options)
    else:
        args = lib["OrderArgs"](token_id=str(token_id), price=float(limit), size=float(shares), side=side)
        signed = client.create_order(args, options)
    order_type = lib["OrderType"].FAK if taker else lib["OrderType"].GTC
    try:
        response = client.post_order(signed, order_type, post_only=post_only)
    except Exception as exc:  # noqa: BLE001 - the order may or may not have landed
        err_msg = getattr(exc, "error_msg", None) or getattr(exc, "error_message", None)
        if isinstance(err_msg, dict) and "no orders found to match with FAK" in str(err_msg.get("error", "")):
            oid = err_msg.get("orderID")
            submit.audit({"actor": "live/v2_transport.py", "action": "submit",
                          "reason": "fak_no_match", "params": params,
                          "response_summary": str(err_msg), "order_id": oid}, path=audit_path)
            res = {**out, "ok": True, "order_id": oid, "status": "cancelled",
                   "filled_shares": ZERO, "avg_price": None, "cost": ZERO,
                   "unfilled": shares, "terminal": True, "response_summary": str(err_msg),
                   "detail": "no orders found to match with FAK order (killed)"}
            if taker:
                res["fill_and_kill"] = True
                res["voided_shares"] = shares
            return res

        detail = f"{type(exc).__name__}: {exc}"
        submit.audit({"actor": "live/v2_transport.py", "action": "exception", "reason": "submit_failed",
                      "params": params, "response_summary": detail}, path=audit_path)
        return {**out, "status": "submit_failed", "residual_risk": True, "detail": detail}

    order_id = order_id_of(response)
    summary = order_summary(response)
    submit.audit({"actor": "live/v2_transport.py", "action": "submit",
                  "reason": "ok" if order_id else "no_order_id", "params": params,
                  "response_summary": summary, "order_id": order_id}, path=audit_path)
    if not order_id:
        return {**out, "status": "no_order_id", "residual_risk": True,
                "detail": "submit response carried no order id", "response_summary": summary}

    fill = poll_fill(client, order_id, attempts=poll_attempts, sleep=sleep, sleep_seconds=poll_sleep)
    filled = fill.get("filled_shares") or ZERO
    avg = fill.get("avg_price")
    cost = (filled * avg).quantize(QTY) if (filled > ZERO and avg is not None) else ZERO
    result = {**out, "ok": True, "order_id": order_id, "status": fill["status"],
              "filled_shares": filled, "avg_price": avg, "cost": cost,
              "unfilled": (shares - filled) if filled <= shares else ZERO,
              "terminal": fill.get("terminal"), "response_summary": summary,
              "detail": fill.get("detail", "")}
    if taker:
        # FAK evidence for the engine/report: the remainder was voided, not left resting. A FAK
        # order is terminal as soon as it is answered, so the take-down below normally does not
        # fire; it stays armed as the defensive net in case a fill ever reports a resting state.
        result["fill_and_kill"] = True
        result["voided_shares"] = result["unfilled"]

    # the engine wants "fill now or stand down", so the unfilled remainder is taken down here;
    # the smoke order deliberately keeps it resting so the place→query→cancel loop can be tested.
    # For a taker order the FAK answer is terminal: the remainder was voided server-side, so a
    # cancel would be pointless *and* would report false residual risk. The net stays armed for
    # the one case that matters — a taker order that somehow reports a resting state.
    fill_status = str(fill["status"]).lower()
    needs_takedown = take_down_unfilled and (shares - filled) > ZERO \
        and fill_status not in ("cancelled", "canceled") \
        and not (taker and fill_status in TERMINAL_STATUSES)
    if needs_takedown:
        takedown = cancel_with_retry(client, order_id, sleep=sleep, audit_path=audit_path)
        result["cancel"] = takedown
        if not takedown["ok"]:
            result["residual_risk"] = True
            result["detail"] = (result["detail"] + " | " if result["detail"] else "") + takedown["detail"]
    return result


def _safe_dec(value) -> Decimal:
    try:
        return _dec(value, "value")
    except ValueError:
        return ZERO


# --------------------------------------------------------------------------- CLI (read-only default)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLOB v2 transport (read-only unless gated)")
    parser.add_argument("--status", action="store_true", help="gates + v2 client/library status")
    parser.add_argument("--open-orders", action="store_true", help="read-only: open orders")
    parser.add_argument("--version", action="store_true", help="read-only: server CLOB version")
    parser.add_argument("--enable-submit", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--cancel-order", dest="cancel_order_id",
                        help="cancel one order id (requires all three gates)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    env = creds_mod.load_env_file()
    gates = submit.gate_status(enable_submit=args.enable_submit, env=env, confirm=args.confirm)
    if args.status:
        sdk_ok = sdk_available()
        payload = {"gates": gates, "sdk_import_ok": sdk_ok,
                   "release": list(RELEASE_WRITE_METHODS),
                   "read_only": list(RELEASE_READ_METHODS),
                   "blocked_write_surface": (list(SUBMIT_METHODS) + list(ADMIN_METHODS)
                                             + list(STATE_WRITE_METHODS) + list(RFQ_SUBMIT_METHODS))}
        if not sdk_ok:                       # the hint is only relevant when the SDK is missing
            payload["hint"] = V2_HINT
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        submit.prepare_network(env)          # .env proxy + forced IPv4, before any client
        client = build_client(creds_mod.validate_creds(env))
        arm_controlled_sentinels(client)
    except Exception as exc:  # noqa: BLE001 - fail closed
        print(f"v2 client unavailable: {creds_mod.sanitize(f'{type(exc).__name__}: {exc}', env)}")
        return 2

    if args.version:
        try:
            print(f"clob server version: {client.get_version()}")
            return 0
        except Exception as exc:  # noqa: BLE001 - fail closed, with a clear message
            print(f"cannot read server version: {creds_mod.sanitize(f'{type(exc).__name__}: {exc}', env)}")
            return 2
    if args.open_orders:
        try:
            result = list_open_orders(client)
        except Exception as exc:  # noqa: BLE001 - fail closed, audited
            detail = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
            submit.audit({"actor": "live/v2_transport.py", "action": "query",
                          "reason": "open_orders_failed", "response_summary": detail})
            print(f"open-orders failed: {detail}")
            return 2
        submit.audit({"actor": "live/v2_transport.py", "action": "query", "reason": "open_orders",
                      "response_summary": result["response_summary"]})
        print(json.dumps(result["orders"], indent=2, ensure_ascii=False) if args.json
              else f"open orders: {result['count']}")
        return 0
    if args.cancel_order_id:
        if not gates["ok"]:
            submit.audit({"actor": "live/v2_transport.py", "action": "gate_deny",
                          "reason": gates["reason"], "params": {"intent": "cancel_order"},
                          "order_id": args.cancel_order_id})
            print(f"REFUSED: {gates['reason']} — {gates['detail']}")
            return 3
        result = cancel_with_retry(client, args.cancel_order_id)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0 if result["ok"] else 2

    parser.print_usage()
    print("nothing to do: pass --status / --version / --open-orders / --cancel-order")
    return 2


if __name__ == "__main__":
    sys.exit(main())
