#!/usr/bin/env python3
"""Shared CLOB **v2** façade: client construction, reads, and the SDK-agnostic helpers.

``py-clob-client`` (v1) is dead: Polymarket moved to CLOB v2 on 2026-04-28 and every v1-signed
order is rejected (``invalid order version``). Everything in ``live/`` therefore speaks
``py-clob-client-v2`` now, and this module is the one place that builds a client and reads from
it (balance/allowance, open orders, one order, trades) plus the transport-independent helpers
(forced IPv4, JSON GET, data-api positions, egress identity).

**Write path**: ``post_order`` / ``cancel_orders`` are *not* called here. They live in exactly one
module — :mod:`live.v2_transport` — which owns the gates, the clamping, the fill reconciliation
and the audit trail. :func:`submit_order` / :func:`cancel_orders` below are thin **delegating**
façades, so the whole live layer can speak v2 without a second implementation to keep in sync.

The v2 SDK is imported lazily: this module (and the paper path) stays usable under a plain
stdlib interpreter, and a missing SDK raises a clear error naming the v2 venv.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "weatherbotyes2re-live/1.0 (read-only reconcile)"
#: collateral decimals — 6 dp. Today's collateral is pUSD (Polymarket USD,
#: 0xc011a7e1…), not USDC.e; same 6 decimals, so this constant is unchanged.
USDC_DECIMALS = 6

V2_VENV_HINT = (
    "py-clob-client-v2 is not importable with this interpreter. CLOB v1 orders are rejected "
    "since 2026-04-28, so the v2 SDK is the only supported one. It lives in its own venv — run with:\n"
    "  /home/da/桌面/poly-yes2/live-probe-v2/.venv/bin/python live/reconcile.py\n"
    "(stdlib-only unit tests need no SDK: python3.13 tests_live.py / tests_port.py)"
)
#: kept as an alias for older callers/log lines
VENV_HINT = V2_VENV_HINT

_IPV4_INSTALLED = False


def force_ipv4() -> bool:
    """Force every DNS lookup to ``AF_INET``.

    Why: this host (192.168.1.98) has no IPv6 route. When DNS returns an AAAA
    record, ``connect()`` fails with ``ENETUNREACH`` before it ever tries the A
    record — so pin the family instead of hoping for happy-eyeballs. Idempotent.
    """
    global _IPV4_INSTALLED
    if _IPV4_INSTALLED:
        return True
    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    _IPV4_INSTALLED = True
    return True


def http_json(url: str, *, timeout: int = 20) -> Any:
    """GET a JSON document (urllib honours ``http_proxy``/``https_proxy`` from env)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url.split('?')[0]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error for {url.split('?')[0]}: {exc.reason}") from None


def _py_clob_v2() -> dict[str, Any]:
    """Lazy import of py-clob-client-v2; raises a clear error when absent."""
    force_ipv4()
    try:
        from py_clob_client_v2.clob_types import (ApiCreds, AssetType, BalanceAllowanceParams,
                                                  OrderArgs, OrderType,
                                                  PartialCreateOrderOptions)
        from py_clob_client_v2.client import ClobClient
    except ImportError as exc:  # pragma: no cover - exercised only outside the v2 venv
        raise RuntimeError(f"{exc}\n{V2_VENV_HINT}") from None
    return {
        "ClobClient": ClobClient,
        "ApiCreds": ApiCreds,
        "AssetType": AssetType,
        "BalanceAllowanceParams": BalanceAllowanceParams,
        "OrderArgs": OrderArgs,
        "OrderType": OrderType,
        "PartialCreateOrderOptions": PartialCreateOrderOptions,
    }


#: kept for callers that still say ``_py_clob()``; it is the **v2** import now
def _py_clob() -> dict[str, Any]:  # pragma: no cover - thin alias
    return _py_clob_v2()


def sdk_available() -> bool:
    """True when the CLOB v2 SDK can be imported (no client is constructed)."""
    try:
        _py_clob_v2()
    except RuntimeError:
        return False
    return True


def build_client(creds: dict, *, host: str = CLOB_HOST, chain_id: int = CHAIN_ID) -> Any:
    """Construct a Level-2 **v2** ClobClient from validated creds.

    v2 takes the API credentials explicitly (there is no ``create_or_derive_api_creds``).
    """
    lib = _py_clob_v2()
    api_creds = None
    if creds.get("api_key") and creds.get("api_secret") and creds.get("api_passphrase"):
        api_creds = lib["ApiCreds"](
            creds["api_key"], creds["api_secret"], creds["api_passphrase"]
        )
    return lib["ClobClient"](
        host,
        chain_id=chain_id,
        key=creds["private_key"],
        creds=api_creds,
        signature_type=creds["signature_type"],
        funder=creds["funder_address"],
    )


def get_balance_allowance(client: Any) -> dict:
    """Collateral balance + per-contract allowances. Read-only."""
    lib = _py_clob_v2()
    params = lib["BalanceAllowanceParams"](asset_type=lib["AssetType"].COLLATERAL)
    return client.get_balance_allowance(params)


def get_open_orders(client: Any) -> list:
    """Currently open orders for this API key. Read-only (v2: ``get_open_orders``)."""
    return client.get_open_orders() or []


def get_order(client: Any, order_id: str) -> dict:
    """One order by id. Read-only."""
    return client.get_order(str(order_id))


def get_trades(client: Any, **kwargs) -> list:
    """Trade history for this API key. Read-only."""
    return client.get_trades(**kwargs) or []


# ------------------------------------------------------------------ write façades (delegating)
# The write *calls* (``post_order`` / ``cancel_orders``) exist in exactly one module —
# live/v2_transport.py. These two helpers keep the API surface of this façade complete
# without creating a second implementation that could drift from the audited one.

def submit_order(client: Any, **kwargs) -> dict:
    """Sign + post one passive order **through the audited channel** (live/v2_transport)."""
    from . import v2_transport  # local import: v2_transport imports this module
    kwargs.setdefault("post_only", True)
    return v2_transport.execute_leg(client, **kwargs)


def cancel_orders(client: Any, order_ids, **kwargs) -> dict:
    """Cancel **through the audited channel** (live/v2_transport, with retry + residual risk)."""
    from . import v2_transport  # local import: v2_transport imports this module
    ids = [order_ids] if isinstance(order_ids, (str, bytes)) else list(order_ids)
    if len(ids) == 1:
        return v2_transport.cancel_with_retry(client, ids[0], **kwargs)
    out = [v2_transport.cancel_with_retry(client, oid, **kwargs) for oid in ids]
    return {"ok": all(item.get("ok") for item in out), "orders": out}


def fetch_positions(address: str, *, limit: int = 500, timeout: int = 25) -> list:
    """Positions held by ``address`` from Polymarket's data-api. Read-only."""
    query = urllib.parse.urlencode({"user": address, "limit": limit, "sizeThreshold": 0.1})
    data = http_json(f"{DATA_API}/positions?{query}", timeout=timeout)
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("data-api /positions returned an unexpected shape")
    return data


def fetch_egress_info(*, timeout: int = 15) -> dict:
    """Best-effort public egress identity (ipinfo.io). Caller handles failure."""
    data = http_json("https://ipinfo.io/json", timeout=timeout)
    return {
        "ip": data.get("ip"),
        "country": data.get("country"),
        "org": data.get("org"),
    }
