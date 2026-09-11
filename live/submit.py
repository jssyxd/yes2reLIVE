#!/usr/bin/env python3
"""Phase-3 controlled submit channel — the **safety machinery** every write goes through.

This module owns the gate record, the append-only audit log, the passivity/size limits and the
CLI used by the operator smoke order. Since Phase 3b-3 the **write primitives themselves live in
:mod:`live.v2_transport`** (CLOB v2 — v1 orders have been rejected since 2026-04-28), and the
functions at the bottom of this file are thin adapters that delegate to it, so there is exactly
one ``post_order``/``cancel_orders`` call site in the package (``tests_live.py`` asserts it).

Safety model (Phase 3 inverts the Phase 2 rule on purpose):

* Phase 1/2 said "no write path may be reachable". Phase 3 needs a *minimal* real write
  path, so the rule becomes: **the write path must be impossible to trigger by accident,
  impossible to exceed its limits, and auditable at every step.**
* Every refusal is written to the audit log — nothing is ever skipped silently.
* The three gates below are all required. Each one stops a different accident:

  1. ``--enable-submit`` (CLI flag)  — the *invocation* must ask for the write path. A bare
     ``smoke.py`` run (cron, shell loop, copy-pasted read-only command) can never submit.
  2. ``LIVE_SUBMIT_ENABLED=1`` (env) — separates *capability* from *authorization*. The
     variable is deliberately NOT in ``.env``, so every process that merely loads ``.env``
     (dev box, inspection runs, tests) stays read-only; only a shell that exports it
     explicitly — on the deployment host — is authorized.
  3. ``--confirm SMOKE-<utc-date>`` (human step) — a date-scoped, non-reusable phrase
     (printed by ``live/submit.py --phrase``). It forces the operator to read today's
     phrase, and it makes *stale replay* (yesterday's command line, an old script, a
     copied shell-history entry) fail.

  Dropping any single one leaves a real hole: (1) covers accidental invocation, (2)
  covers the wrong machine/session, (3) covers replay of an old command. Two would be
  insufficient; three is the minimum that closes all three.

Sentinel release (least privilege): the Phase-2 sentinels are all armed first, then only
``post_order`` and ``cancel`` are released — plus the four read-only calls
(``get_order``/``get_orders``/``get_trades``/``get_balance_allowance``) that Phase 2 never
blocked in the first place. Every RFQ / credential-admin / state-write method stays blocked,
and that is asserted (both here at runtime and in ``tests_live.py``).

Usage:
    live/submit.py --phrase                      # print today's confirm phrase (no network)
    live/submit.py --status                      # show which of the three gates are satisfied
    live/submit.py --open-orders                 # read-only: list live/open orders
    live/submit.py --cancel-order <id> --enable-submit --confirm SMOKE-YYYY-MM-DD

Exit codes: 0 ok · 2 fail-closed (network/preflight) · 3 gate refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python3.13 live/...py` — make relative imports work
    __package__ = "live"  # `python3.13 live/submit.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from live import clob_client, creds as creds_mod, order_plan
else:  # `python3.13 tests_live.py` / `import live.submit`
    from . import clob_client, creds as creds_mod, order_plan
# nb: sign_dryrun / v2_transport are imported lazily inside the functions that need them —
# the three modules reference each other, and lazy imports keep the graph acyclic.

ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = Path("data/live_events.jsonl")

#: writes that may be released — the narrowest set that can smoke-test the loop (**v2 names**:
#: ``cancel_orders([id])`` is the safe cancel; the single-id ``cancel_order`` stays blocked)
RELEASE_WRITE_METHODS = ("post_order", "cancel_orders")
#: read-only calls the smoke loop needs (Phase 2 never sentineled reads: releasing is a no-op,
#: recorded so the released set is explicit and auditable)
RELEASE_READ_METHODS = ("get_order", "get_open_orders", "get_trades", "get_balance_allowance",
                        "get_order_book", "get_tick_size")
#: everything the operator may unlock
ALLOWED_RELEASE = RELEASE_WRITE_METHODS + RELEASE_READ_METHODS

CONFIRM_PREFIX = "SMOKE"

#: params keys that must never reach the audit log
_SECRET_HINTS = ("key", "secret", "pass", "priv", "signature")

#: gate codes
#: the three gate checks that must *all* be true (a truthy ``ok`` alone is not enough)
GATE_CHECKS = ("cli_flag", "env_flag", "confirm_phrase")

GATE_OK = "ok"
GATE_FLAG = "submit_flag_missing"
GATE_ENV = "submit_env_missing"
GATE_CONFIRM_MISSING = "confirm_phrase_missing"
GATE_CONFIRM_MISMATCH = "confirm_phrase_mismatch"

#: non-marketable check codes
NM_OK = "ok"
NM_VIOLATION = "marketable_would_fill"
NM_NO_BOOK = "no_book"
NM_INVALID = "invalid_input"

#: limit check codes
LIMIT_OK = "ok"
LIMIT_INVALID = "invalid_input"
LIMIT_FIRE_BUDGET = "notional_exceeds_fire_budget"
LIMIT_CAPITAL_CAP = "capital_cap_exceeded"


class AuditError(RuntimeError):
    """The audit log could not be written — refuse to act without an audit trail."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- gates

def gates_all_passed(gates) -> bool:
    """True only when the record says ``ok`` **and** every individual gate check passed.

    Shared by both write channels (v1 ``submit_order`` and v2 ``execute_leg``) so a forged or
    truncated gate record can never unlock a write.
    """
    if not isinstance(gates, dict) or not gates.get("ok"):
        return False
    checks = gates.get("checks") or {}
    return all(checks.get(key) for key in GATE_CHECKS)


def phrase(now: datetime | None = None) -> str:
    """Today's confirm phrase (UTC-dated ⇒ not replayable tomorrow)."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{CONFIRM_PREFIX}-{stamp.strftime('%Y-%m-%d')}"


def gate_status(*, enable_submit: bool, env: dict | None, confirm: str | None,
                now: datetime | None = None) -> dict:
    """Evaluate all three gates. Pure. First failure wins (deterministic, testable)."""
    env = env or {}
    expected = phrase(now)
    checks = {
        "cli_flag": bool(enable_submit),
        "env_flag": str(env.get("LIVE_SUBMIT_ENABLED") or "").strip() == "1",
        "confirm_phrase": (confirm or "").strip() == expected,
    }
    if not checks["cli_flag"]:
        return {"ok": False, "reason": GATE_FLAG, "expected_phrase": expected, "checks": checks,
                "detail": "pass --enable-submit to unlock the write path"}
    if not checks["env_flag"]:
        return {"ok": False, "reason": GATE_ENV, "expected_phrase": expected, "checks": checks,
                "detail": "export LIVE_SUBMIT_ENABLED=1 (deliberately absent from .env)"}
    if not (confirm or "").strip():
        return {"ok": False, "reason": GATE_CONFIRM_MISSING, "expected_phrase": expected,
                "checks": checks, "detail": f"pass --confirm {expected}"}
    if not checks["confirm_phrase"]:
        return {"ok": False, "reason": GATE_CONFIRM_MISMATCH, "expected_phrase": expected,
                "checks": checks, "detail": f"confirm phrase mismatch (want {expected})"}
    return {"ok": True, "reason": GATE_OK, "expected_phrase": expected, "checks": checks,
            "detail": "all three gates satisfied"}


# --------------------------------------------------------------------------- audit log

def sanitize_params(params: dict | None) -> dict:
    """Drop anything that could carry a credential; keep the numbers we need for audit."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if any(hint in str(key).lower() for hint in _SECRET_HINTS):
            out[key] = "<redacted>"
            continue
        out[key] = value if isinstance(value, (int, float, bool, str, type(None))) else str(value)
    return out


def audit(entry: dict, *, path: Path | None = None) -> dict:
    """Append one audit line. Refusals are audited too — never skip silently.

    Raises :class:`AuditError` when the log cannot be written: acting without an audit
    trail is exactly what this phase forbids.
    """
    record = {
        "ts_utc": entry.get("ts_utc") or _now_iso(),
        "phase": "phase3",
        "actor": entry.get("actor") or "live/submit.py",
        "action": entry.get("action"),
        "reason": entry.get("reason"),
        "params": sanitize_params(entry.get("params")),
        "response_summary": entry.get("response_summary"),
        "order_id": entry.get("order_id"),
    }
    target = Path(path) if path else AUDIT_PATH
    try:
        if target.parent != Path(""):
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        raise AuditError(f"cannot append to audit log {target}: {exc}") from None
    return record


# --------------------------------------------------------------------------- sentinels

def sentinel_targets(client) -> list:
    """Every write surface reachable from the client (delegated to the v2 channel)."""
    from . import v2_transport  # local import: v2_transport imports this module
    return v2_transport.sentinel_targets(client)


def install_sentinels(client) -> dict:
    """Arm every write method (delegated: one sentinel factory, one name list)."""
    from . import v2_transport
    return v2_transport.install_sentinels(client)


def arm_controlled_sentinels(client) -> dict:
    """Arm everything, then release only ``RELEASE_WRITE_METHODS`` (delegated to v2)."""
    from . import v2_transport
    return v2_transport.arm_controlled_sentinels(client)


def _blocked(name: str):
    """The sentinel factory, owned by ``live/v2_transport.py`` (kept as an alias)."""
    from . import v2_transport
    return v2_transport._sentinel(name)


# --------------------------------------------------------------------------- pure checks

def check_non_marketable(*, side: str, price, book: Any) -> dict:
    """Refuse any order that could fill immediately.

    BUY must rest strictly below the best ask, SELL strictly above the best bid. A missing
    book or a missing opposite-side price is a refusal: without a reference price we cannot
    prove the order is passive.
    """
    deny = {"ok": False, "detail": ""}
    if side not in ("BUY", "SELL"):
        return {**deny, "reason": NM_INVALID, "detail": "side: want BUY or SELL"}
    try:
        px = Decimal(str(price).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return {**deny, "reason": NM_INVALID, "detail": "price: not a number"}
    if not px.is_finite() or px <= 0:
        return {**deny, "reason": NM_INVALID, "detail": "price: must be > 0"}
    if not isinstance(book, dict):
        return {**deny, "reason": NM_NO_BOOK, "detail": "book: missing"}
    reference_key = "best_ask" if side == "BUY" else "best_bid"
    raw = book.get(reference_key)
    if raw is None:
        return {**deny, "reason": NM_NO_BOOK, "detail": f"{reference_key}: missing"}
    try:
        reference = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return {**deny, "reason": NM_INVALID, "detail": f"{reference_key}: not a number"}
    if not reference.is_finite() or reference <= 0:
        return {**deny, "reason": NM_INVALID,
                "detail": f"{reference_key}: not finite/positive ({reference})"}
    if side == "BUY" and px >= reference:
        return {**deny, "reason": NM_VIOLATION,
                "detail": f"BUY price {px} >= best_ask {reference} — would cross and fill"}
    if side == "SELL" and px <= reference:
        return {**deny, "reason": NM_VIOLATION,
                "detail": f"SELL price {px} <= best_bid {reference} — would cross and fill"}
    return {"ok": True, "reason": NM_OK, "reference_price": str(reference),
            "detail": f"passive: {side} {px} vs {reference_key} {reference}"}


def check_limits(*, notional_usdc, fire_budget_usdc, committed_usdc, max_capital_usdc) -> dict:
    """Per-order and cumulative exposure caps, both from ``.env`` ``LIVE_*`` values."""
    def _dec(value, field):
        if value is None or isinstance(value, bool):
            raise ValueError(f"{field}: missing")
        try:
            out = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError, ValueError):
            raise ValueError(f"{field}: not a number") from None
        if not out.is_finite() or out < 0:
            raise ValueError(f"{field}: must be >= 0 and finite")
        return out

    try:
        notional = _dec(notional_usdc, "notional_usdc")
        fire_budget = _dec(fire_budget_usdc, "fire_budget_usdc")
        committed = _dec(committed_usdc, "committed_usdc")
        max_capital = _dec(max_capital_usdc, "max_capital_usdc")
    except ValueError as exc:
        return {"ok": False, "reason": LIMIT_INVALID, "detail": str(exc)}
    if notional > fire_budget:
        return {"ok": False, "reason": LIMIT_FIRE_BUDGET,
                "detail": f"notional {notional} > LIVE_FIRE_BUDGET_USDC {fire_budget}"}
    if committed + notional > max_capital:
        return {"ok": False, "reason": LIMIT_CAPITAL_CAP,
                "detail": f"committed {committed} + notional {notional} > LIVE_MAX_CAPITAL_USDC {max_capital}"}
    return {"ok": True, "reason": LIMIT_OK,
            "detail": f"notional {notional} <= {fire_budget}; {committed}+{notional} <= {max_capital}"}


# --------------------------------------------------------------------------- CLOB calls

def _summarize_post(response: Any) -> dict:
    if not isinstance(response, dict):
        return {"raw": str(response)[:200]}
    keys = ("success", "status", "orderID", "orderId", "id", "errorMsg", "error", "takingAmount", "makingAmount")
    return {key: response[key] for key in keys if key in response}


def _order_id_of(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("orderID", "orderId", "id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def _summarize_order(response: Any) -> dict:
    if not isinstance(response, dict):
        return {"raw": str(response)[:200]}
    keys = ("id", "status", "price", "original_size", "size_matched", "asset_id", "side",
            "outcome", "market", "created_at", "expiration", "associate_trades", "errorMsg")
    summary = {key: response[key] for key in keys if key in response}
    if not summary:
        summary = {"keys": sorted(response)[:12]}
    return summary


def _summarize_cancel(response: Any) -> dict:
    if not isinstance(response, dict):
        return {"raw": str(response)[:200]}
    keys = ("canceled", "cancelled", "not_canceled", "not_cancelled")
    return {key: response[key] for key in keys if key in response} or {"raw_keys": sorted(response)[:8]}


def submit_order(client, *, token_id: str, price, size, side: str = "BUY", gates: dict,
                 tick=None, neg_risk: bool | None = None, post_only: bool = True,
                 book=None, poll_attempts: int = 6, poll_sleep: float = 1.0, sleep=None,
                 audit_path=None, clamp: bool = True, take_down_unfilled: bool = True) -> dict:
    """Sign + post one passive order **through the audited v2 channel**, exactly once.

    Adapter over :func:`live.v2_transport.execute_leg`: the gates record, the intent audit, the
    pre-submit clamp, the fill reconciliation and the residual-risk reporting all live there.
    The returned dict keeps this module's historical keys (``order_id`` / ``response_summary``)
    so the smoke orchestration is unchanged.
    """
    if not gates_all_passed(gates):
        audit({"action": "deny", "reason": "gates_missing",
               "params": {"intent": "submit_order", "token_id": token_id, "price": str(price)}},
              path=audit_path)
        raise PermissionError("submit_order refused: all three gates must pass in this invocation")
    from . import v2_transport  # local import: v2_transport imports this module
    result = v2_transport.execute_leg(
        client, token_id=str(token_id), price=price, size=size, side=side, book=book, tick=tick,
        neg_risk=neg_risk, gates=gates, post_only=post_only, clamp=clamp,
        poll_attempts=poll_attempts, poll_sleep=poll_sleep, sleep=sleep, audit_path=audit_path,
        take_down_unfilled=take_down_unfilled,
    )
    summary = {"status": result.get("status"), "orderID": result.get("order_id"),
               "clamped": result.get("clamped"), "limit_price": result.get("limit_price")}
    return {"ok": bool(result.get("ok")), "order_id": result.get("order_id"),
            "response_summary": summary, "post_only": post_only,
            "filled_shares": result.get("filled_shares"), "avg_price": result.get("avg_price"),
            "cost": result.get("cost"), "unfilled": result.get("unfilled"),
            "residual_risk": bool(result.get("residual_risk")),
            "audited": True, "detail": result.get("detail", "")}


def get_order(client, order_id: str) -> dict:
    response = clob_client.get_order(client, order_id)
    return {"ok": True, "response_summary": _summarize_order(response), "raw": response}


def list_open_orders(client) -> dict:
    response = clob_client.get_open_orders(client)
    rows = [{"id": row.get("id"), "asset_id": row.get("asset_id"), "side": row.get("side"),
             "price": row.get("price"), "original_size": row.get("original_size"),
             "size_matched": row.get("size_matched"), "status": row.get("status")}
            for row in response if isinstance(row, dict)]
    return {"ok": True, "count": len(rows), "orders": rows, "response_summary": {"open_orders": len(rows)}}


def cancel_order(client, order_id: str) -> dict:
    """Cancel one order (recovery direction — deliberately not gated like ``submit_order``).

    Delegates to :func:`live.v2_transport.cancel_with_retry` (``cancel_orders([id])`` + retries +
    residual-risk reporting), which also writes the ``intent``/``cancel`` audit records.
    """
    from . import v2_transport  # local import: v2_transport imports this module
    result = v2_transport.cancel_with_retry(client, str(order_id))
    summary = result.get("response_summary") or {"attempts": result.get("attempts")}
    return {"ok": bool(result.get("ok")), "order_id": str(order_id),
            "canceled": result.get("canceled") or [], "response_summary": summary,
            "residual_risk": bool(result.get("residual_risk")),
            "audited": bool(result.get("audited", True)), "raw": result}


# --------------------------------------------------------------------------- CLI

def prepare_network(env: dict) -> None:
    """Apply ``.env`` proxy settings and force IPv4 before any client exists.

    This box has no IPv6 route and needs the proxy from ``.env``; PyPI clients read both
    from the process environment, so they must be set before the client is constructed.
    """
    from . import sign_dryrun  # lazy: sign_dryrun imports v2_transport which imports this module
    sign_dryrun._apply_proxy_env(env)
    clob_client.force_ipv4()


def _load_client(env: dict):
    prepare_network(env)
    credentials = creds_mod.validate_creds(env)
    from . import sign_dryrun  # lazy (see above)
    return sign_dryrun._build_client(credentials)


def _audit_summary(as_json: bool = False) -> int:
    """Read the audit log and report what actually happened (read-only, no network)."""
    path = AUDIT_PATH
    counts: dict[str, int] = {}
    order_ids: dict[str, set] = {"submit": set(), "cancel": set()}
    records = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"audit log unreadable ({path}): {exc}")
        return 2
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        records += 1
        action = str(record.get("action"))
        counts[action] = counts.get(action, 0) + 1
        if action in order_ids and record.get("order_id"):
            order_ids[action].add(str(record["order_id"]))
    summary = {"audit_path": str(path), "records": records, "actions": dict(sorted(counts.items())),
               "submit_order_ids": sorted(order_ids["submit"]),
               "cancel_order_ids": sorted(order_ids["cancel"]),
               "real_submits": counts.get("submit", 0), "real_cancels": counts.get("cancel", 0)}
    print(json.dumps(summary, indent=2, ensure_ascii=False) if as_json else
          f"{path}: {records} records\n  actions: {summary['actions']}\n"
          f"  submit (real orders sent): {summary['real_submits']} {summary['submit_order_ids']}\n"
          f"  cancel (real cancels):     {summary['real_cancels']} {summary['cancel_order_ids']}")
    return 0


def _print_gate_status(gates: dict, out=print) -> None:
    out("Phase-3 submit gates:")
    for name, label in (("cli_flag", "--enable-submit"), ("env_flag", "LIVE_SUBMIT_ENABLED=1"),
                        ("confirm_phrase", "--confirm <phrase>")):
        out(f"  [{'x' if gates['checks'][name] else ' '}] {label}")
    out(f"  => {'ALLOWED' if gates['ok'] else 'REFUSED: ' + gates['reason']}")
    out(f"  today's phrase: {gates['expected_phrase']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-3 controlled submit channel")
    parser.add_argument("--phrase", action="store_true", help="print today's confirm phrase (no network)")
    parser.add_argument("--status", action="store_true", help="show which gates are satisfied")
    parser.add_argument("--enable-submit", action="store_true")
    parser.add_argument("--confirm", help="must equal the phrase from --phrase")
    parser.add_argument("--open-orders", action="store_true", help="read-only: list live orders")
    parser.add_argument("--cancel-order", help="cancel one order id (requires all three gates)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--audit-summary", action="store_true",
                        help="read-only: summarise data/live_events.jsonl (what actually happened)")
    args = parser.parse_args(argv)

    if args.audit_summary:
        return _audit_summary(args.json)

    if args.phrase:
        print(phrase())
        return 0

    env = creds_mod.load_env_file()
    gates = gate_status(enable_submit=args.enable_submit, env=env, confirm=args.confirm)

    if args.status:
        _print_gate_status(gates)
        print("no network call was made; nothing was submitted")
        return 0

    if args.open_orders:
        try:
            client = _load_client(env)
            result = list_open_orders(client)
        except Exception as exc:  # noqa: BLE001 - fail closed
            detail = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
            audit({"action": "query", "reason": "open_orders_failed", "response_summary": detail})
            print(f"open-orders failed: {detail}")
            return 2
        audit({"action": "query", "reason": "open_orders", "response_summary": result["response_summary"]})
        print(json.dumps(result["orders"], indent=2, ensure_ascii=False) if args.json
              else f"open orders: {result['count']}")
        return 0

    if args.cancel_order:
        if not gates["ok"]:
            audit({"action": "gate_deny", "reason": gates["reason"], "params": {"intent": "cancel_order"},
                   "order_id": args.cancel_order})
            print(f"REFUSED: {gates['reason']} — {gates['detail']}")
            return 3
        try:
            client = _load_client(env)
            arm_controlled_sentinels(client)
            # cancel_order() writes its own intent/cancel records (single source of truth)
            result = cancel_order(client, args.cancel_order)
            print(json.dumps(result["response_summary"], indent=2, ensure_ascii=False))
            return 0 if result["ok"] else 2
        except Exception as exc:  # noqa: BLE001
            detail = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
            audit({"action": "exception", "reason": "cancel_failed", "response_summary": detail,
                   "order_id": args.cancel_order})
            print(f"cancel failed: {detail}")
            return 2

    parser.print_usage()
    print("nothing to do: pass --phrase / --status / --open-orders / --cancel-order")
    return 2


if __name__ == "__main__":
    sys.exit(main())
