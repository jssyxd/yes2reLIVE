#!/usr/bin/env python3
"""Phase-2 live dry-run: build + locally sign an order, submit nothing.

Hard guarantees enforced here:

1. **Runtime sentinels**: right after the client is constructed and before any
   signing, every submit-class method (``post_order``, ``post_orders``,
   ``create_and_post_order``, ``cancel``, ``cancel_orders``, ``cancel_all``,
   ``cancel_market_orders``) is replaced by a function that raises
   ``RuntimeError("SUBMIT BLOCKED (dry-run)")``. The report carries a proof:
   each sentinel is actually called once and the RuntimeError is recorded.
2. **Signing only**: the order reaches the CLOB through ``create_order()`` —
   the local EIP-712 signing path. Nothing is ever sent.
3. **No submittable artifact**: the signature is deliberately *not* persisted
   (only presence/length/prefix), so ``data/live_order_dryrun.json`` cannot be
   turned into a live order by anyone who reads it.
4. ``--confirm-dryrun`` is mandatory; missing creds/params fail closed
   (``ok: false`` + exit 2).

Usage:
    /home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/sign_dryrun.py --scenario --confirm-dryrun
    ... live/sign_dryrun.py --confirm-dryrun --city london --date 2026-09-11 --direction high
    ... live/sign_dryrun.py --confirm-dryrun --token-id <id> --budget-usdc 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python3.13 live/...py` — make relative imports work
    __package__ = "live"  # `python3.13 live/sign_dryrun.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from live import clob_client, creds as creds_mod, order_plan, v2_transport
else:  # `python3.13 tests_live.py` / `import live.sign_dryrun`
    from . import clob_client, creds as creds_mod, order_plan, v2_transport

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path("data/live_order_dryrun.json")
STATE_PATH = ROOT / "data" / "yes2re_state.json"
CITIES_PATH = ROOT / "config" / "contract_cities.json"
GAMMA_EVENT_ENDPOINT = "https://gamma-api.polymarket.com/events/slug/"

CHAIN_ID = 137
#: the v2 write surface, owned by the single v2 channel (no duplicated lists to drift)
SUBMIT_METHODS = v2_transport.SUBMIT_METHODS
RFQ_SUBMIT_METHODS = v2_transport.RFQ_SUBMIT_METHODS
ADMIN_METHODS = v2_transport.ADMIN_METHODS
STATE_WRITE_METHODS = v2_transport.STATE_WRITE_METHODS

#: offline synthetic market for `--scenario` (no network, no real token)
SCENARIO = {
    "market": {"city": "scenario", "local_date": "2026-01-01", "direction": "high",
               "bucket": "synthetic", "title": "synthetic scenario market", "token_id": "9" * 20},
    "book": {"best_bid": "0.50", "best_ask": "0.523", "tick_size": "0.01",
             "min_order_size": "5", "neg_risk": False,
             "bids": [{"price": "0.50", "size": "120"}], "asks": [{"price": "0.523", "size": "80"}]},
    "budget_usdc": "3",
}

MAX_DISCOVERY_ATTEMPTS = 12


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_proxy_env(env: dict) -> None:
    """Expose .env proxy settings to urllib/httpx (mirrors live/reconcile.py)."""
    for key in ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = (env.get(key) or "").strip()
        if value:
            os.environ.setdefault(key, value)


# --------------------------------------------------------------------------- sentinels

#: the sentinel factory lives in the v2 channel now (same shape as before)
def _blocked(name: str):
    """Backwards-compatible alias: the sentinel factory is owned by ``live/v2_transport.py``."""
    return v2_transport._sentinel(name)


def sentinel_targets(client) -> list[tuple[str, object, tuple[str, ...]]]:
    """Every write surface reachable from the client (v2 lists; delegated)."""
    return v2_transport.sentinel_targets(client)


def install_sentinels(client) -> dict:
    """Replace every write-capable method with a blocking sentinel (delegated to v2)."""
    return v2_transport.install_sentinels(client)


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
        except Exception as exc:  # noqa: BLE001 - anything else means the sentinel is not load-bearing
            entry.update({"blocked": False, "error": f"unexpected {type(exc).__name__}: {exc}"})
        else:
            entry.update({"blocked": False, "error": "call returned — sentinel NOT armed"})
        proofs.append(entry)
    return proofs


# --------------------------------------------------------------------------- market discovery

def _city_table(cities_path: Path | None = None) -> dict[str, dict]:
    raw = json.loads(Path(cities_path or CITIES_PATH).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else list((raw or {}).values())
    return {str(row.get("city_id")): row for row in rows if isinstance(row, dict) and row.get("city_id")}


def _event_slug(city_slug: str, local_date: str, direction: str) -> str:
    """Mirror of ``market_adapter.event_slug`` — kept local so live/ stays paper-independent."""
    parsed = date.fromisoformat(local_date)
    word = "highest" if direction == "high" else "lowest"
    return f"{word}-temperature-in-{city_slug}-on-{parsed.strftime('%B').lower()}-{parsed.day}-{parsed.year}"


def candidate_sessions(state_path: Path | None = None, *, today: str | None = None,
                       limit: int = MAX_DISCOVERY_ATTEMPTS) -> list[dict]:
    """``[{city, local_date, direction}]`` — armed paper sessions (today + next day)."""
    day = date.fromisoformat(today) if today else datetime.now(timezone.utc).date()
    armed: list[dict] = []
    path = Path(state_path or STATE_PATH)
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            keys = (state.get("weatherbotyes2re") or {}).get("armed") or {}
            for key in keys:
                parts = str(key).split("|")
                if len(parts) != 3:
                    continue
                city, local_date, direction = parts
                armed.append({"city": city, "local_date": local_date, "direction": direction})
        except (OSError, ValueError):
            armed = []
    out: list[dict] = []
    for row in armed:
        out.append(row)
        try:
            out.append({**row, "local_date": (date.fromisoformat(row["local_date"]) + timedelta(days=1)).isoformat()})
        except ValueError:
            continue
    if not out:  # no armed session to mirror: fall back to the city table
        for city in sorted(_city_table())[:6]:
            for offset in (0, 1):
                out.append({"city": city, "local_date": (day + timedelta(days=offset)).isoformat(),
                            "direction": "high"})
    return out[:limit]


def _book_from_summary(summary) -> dict:
    """Normalise a v2 order book into the shape order_plan / clamp_limit consume.

    v2's ``get_order_book`` answers with a **dict** (``bids``/``asks``/``tick_size``/
    ``min_order_size``/``neg_risk``); the SDK also ships an ``OrderBookSummary`` dataclass, so
    both shapes are accepted here.
    """
    def _get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def levels(rows):
        out = []
        for row in rows or []:
            price = row.get("price") if isinstance(row, dict) else getattr(row, "price", None)
            size = row.get("size") if isinstance(row, dict) else getattr(row, "size", None)
            if price is not None:
                out.append({"price": str(price), "size": str(size)})
        return out

    # CLOB /book returns bids ascending and asks descending (worst-first), so the best ask is
    # the *minimum* ask and the best bid the *maximum* bid — the same convention as
    # clob_market_data.BookSnapshot.best_ask/best_bid.
    bids = sorted(levels(_get(summary, "bids")), key=lambda r: Decimal(r["price"]), reverse=True)
    asks = sorted(levels(_get(summary, "asks")), key=lambda r: Decimal(r["price"]))
    return {
        "best_bid": bids[0]["price"] if bids else None,
        "best_ask": asks[0]["price"] if asks else None,
        "tick_size": _get(summary, "tick_size"),
        "min_order_size": _get(summary, "min_order_size"),
        "neg_risk": bool(_get(summary, "neg_risk", False)),
        "bids": bids[:5],
        "asks": asks[:5],
    }


def discover_market(client, *, city=None, local_date=None, direction=None, token_id=None,
                    cap=None, timeout=25, limit=MAX_DISCOVERY_ATTEMPTS,
                    state_path: Path | None = None) -> dict:
    """Find one real, live weather token to dry-run against (read-only Gamma + CLOB GET)."""
    attempts: list[dict] = []
    cities = _city_table()

    def _usable(book: dict, key: str) -> bool:
        """Gamma's bestAsk is a hint only: the CLOB book decides (it may be stale/thin)."""
        try:
            ask_dec = Decimal(str(book.get("best_ask")))
        except Exception:  # noqa: BLE001
            attempts.append({"key": key, "status": "no_live_ask_in_book"})
            return False
        if cap is not None and ask_dec > cap:
            attempts.append({"key": key, "status": "book_ask_above_cap", "best_ask": str(ask_dec)})
            return False
        return True

    if token_id:
        summary = client.get_order_book(token_id)
        return {
            "market": {"city": city, "local_date": local_date, "direction": direction,
                       "bucket": None, "title": "explicit --token-id", "token_id": token_id},
            "book": _book_from_summary(summary),
            "attempts": [{"key": f"token:{token_id}", "status": "ok"}],
        }

    sessions = ([{"city": city, "local_date": local_date, "direction": direction}]
                if city and local_date else candidate_sessions(state_path, limit=limit))
    if direction:
        sessions = [{**row, "direction": direction} for row in sessions]
    if city:
        sessions = [row for row in sessions if row["city"] == city]
    if local_date:
        sessions = [row for row in sessions if row["local_date"] == local_date]

    for row in sessions[:limit]:
        record = cities.get(row["city"]) or {}
        slug = str(record.get("market_city_slug") or row["city"])
        key = f"{row['city']}|{row['local_date']}|{row['direction']}"
        try:
            event = clob_client.http_json(
                GAMMA_EVENT_ENDPOINT + _event_slug(slug, row["local_date"], row["direction"]),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            attempts.append({"key": key, "status": f"gamma_error:{type(exc).__name__}"})
            continue
        markets = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
        live = []
        for market in markets:
            ask = market.get("bestAsk")
            if not market.get("acceptingOrders") or not ask:
                continue
            try:
                ask_dec = Decimal(str(ask))
            except Exception:  # noqa: BLE001
                continue
            if 0 < ask_dec <= (cap if cap is not None else Decimal("1")):
                live.append((ask_dec, market))
        if not live:
            attempts.append({"key": key, "status": "no_live_ask", "markets": len(markets)})
            continue
        ask_dec, market = max(live, key=lambda row: float(row[1].get("volumeNum") or 0))
        tokens = json.loads(market.get("clobTokenIds") or "[]")
        if not tokens:
            attempts.append({"key": key, "status": "no_token_ids"})
            continue
        summary = client.get_order_book(tokens[0])
        book = _book_from_summary(summary)
        if not _usable(book, key):
            continue
        attempts.append({"key": key, "status": "ok", "best_ask": str(ask_dec)})
        return {
            "market": {"city": row["city"], "local_date": row["local_date"], "direction": row["direction"],
                       "bucket": market.get("groupItemTitle"), "title": market.get("question"),
                       "event_slug": event.get("slug"), "volume": market.get("volumeNum"),
                       "token_id": tokens[0], "yes_token_id": tokens[0],
                       "no_token_id": tokens[1] if len(tokens) > 1 else None},
            "book": book,
            "attempts": attempts,
        }

    raise RuntimeError(f"no live market found in {len(sessions[:limit])} candidate(s): {attempts[-3:]}")


# --------------------------------------------------------------------------- signing

def _build_client(creds) -> object:
    """Real CLOB client (construction only — no network, no submit)."""
    return clob_client.build_client(creds)


def _sign(client, plan, creds, *, offline: bool):
    """Sign one order with the **v2** SDK. ``offline`` uses v2's local builder (no network).

    Signing only — the dry run never reaches ``post_order`` (the sentinels plus the AST guard
    make that structural, not a convention).
    """
    if offline:
        return _offline_signed_order(creds, plan)
    lib = clob_client._py_clob_v2()
    args = lib["OrderArgs"](token_id=plan["token_id"], price=float(plan["price"]),
                            size=float(plan["size"]), side=plan["side"])
    options = lib["PartialCreateOrderOptions"](tick_size=str(plan["tick"]),
                                               neg_risk=bool(plan.get("neg_risk")))
    return client.create_order(args, options)


def _offline_signed_order(creds, plan):
    """Local-only v2 signing for ``--scenario`` (v2's own builder; zero network).

    Mirrors what ``ClobClient.create_order`` does, minus the tick/fee/version lookups that
    need the wire: the caller supplies the tick and neg_risk from the (synthetic) book.
    """
    from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.builder import OrderBuilder
    from py_clob_client_v2.signer import Signer

    tick = str(plan["tick"])
    builder = OrderBuilder(Signer(creds["private_key"], CHAIN_ID),
                           signature_type=creds["signature_type"], funder=creds["funder_address"])
    args = OrderArgs(token_id=str(plan["token_id"]), price=float(plan["price"]),
                     size=float(plan["size"]), side=plan["side"])
    options = PartialCreateOrderOptions(tick_size=tick, neg_risk=bool(plan.get("neg_risk")))
    return builder.build_order(args, options)


def _contract_config(neg_risk: bool):
    """v2 contract config: the EIP-712 domain uses the **v2** exchange addresses.

    v2's ``get_contract_config(chain_id)`` returns both generations; a v2 order is signed
    against ``exchange_v2`` (or ``neg_risk_exchange_v2``), which is what the order builder
    itself uses.
    """
    from types import SimpleNamespace

    from py_clob_client_v2.config import get_contract_config

    cfg = get_contract_config(CHAIN_ID)
    exchange = (cfg.neg_risk_exchange_v2 if neg_risk else cfg.exchange_v2)
    return SimpleNamespace(exchange=exchange, collateral=cfg.collateral,
                           conditional_tokens=cfg.conditional_tokens,
                           neg_risk_exchange_v2=cfg.neg_risk_exchange_v2)


def _order_hash(signed, creds, plan) -> str | None:
    """v2 EIP-712 order hash (the digest the signature covers); None when unavailable.

    Informative, never load-bearing: a failure here must not fail the dry run.
    """
    try:
        from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
        from py_clob_client_v2.signer import Signer

        contract = _contract_config(bool(plan.get("neg_risk")))
        builder = ExchangeOrderBuilderV2(contract.exchange, CHAIN_ID, Signer(creds["private_key"], CHAIN_ID))
        return builder.build_order_hash(builder.build_order_typed_data(signed))
    except Exception:  # noqa: BLE001 - the hash is informative, not load-bearing
        return None


def _signed_fields(signed) -> dict:
    """Flatten a v2 SignedOrderV2 (dataclass) — attribute access, no ``.get()``.

    The ``signature`` is deliberately **dropped**: Phase 2's artifact must stay unsubmittable
    (presence/length/prefix are reported separately).
    """
    fields: dict[str, Any] = {}
    for key, value in (vars(signed).items() if hasattr(signed, "__dict__") else []):
        if key == "signature":
            continue
        fields[key] = value if isinstance(value, str) else str(value)
    if not fields:                                   # v1-style object with .dict(), if ever seen
        order = getattr(signed, "order", None)
        raw = order.dict() if hasattr(order, "dict") else {}
        fields = {key: (value if isinstance(value, str) else str(value)) for key, value in raw.items()}
    return fields


def _describe_signed(signed, order_hash, note: str) -> dict:
    raw = _signed_fields(signed)
    signature = getattr(signed, "signature", None) or ""
    return {
        "hash": order_hash,
        "signature_present": bool(signature),
        "signature_length": len(signature),
        "signature_prefix": signature[:10] or None,
        "maker_amount": str(raw.get("makerAmount")),
        "taker_amount": str(raw.get("takerAmount")),
        "order": raw,
        "note": note,
    }


# --------------------------------------------------------------------------- orchestration

def _plan_json(plan: dict) -> dict:
    return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in plan.items()}


def _skeleton(env: dict, scenario: bool) -> dict:
    return {
        "ok": False,
        "reason": None,
        "ts_utc": _now_iso(),
        "phase": "phase2-dryrun",
        "scenario": scenario,
        "mode": str(env.get("YES2RE_MODE") or "").strip() or None,
        "leg": None,
        "sentinel": {"armed": False, "installed": [], "missing": [], "proof": [],
                     "order_submit_methods": list(SUBMIT_METHODS)},
        "caps": {},
        "caps_source": str(order_plan.CONFIG_PATH.relative_to(ROOT)),
        "market": None,
        "discovery": None,
        "book": None,
        "plan": None,
        "signed_order": None,
        "submit": {"attempted": False, "statement": "no submit call exists in this module"},
    }


def run_dryrun(*, scenario: bool = False, confirm: bool = False, env: dict | None = None,
               city=None, local_date=None, direction=None, leg=None, token_id=None,
               budget_usdc=None, price_cap=None, timeout: int = 25,
               state_path: Path | None = None) -> dict:
    """Build + sign one order without submitting. Never raises — failures are fail-closed."""
    env = env if env is not None else creds_mod.load_env_file()
    report = _skeleton(env, scenario)
    if not confirm:
        report["reason"] = "confirm_flag_missing"
        return report
    try:
        _apply_proxy_env(env)
        clob_client.force_ipv4()
        credentials = creds_mod.validate_creds(env)

        caps = order_plan.load_caps()
        report["caps"] = {key: str(value) for key, value in caps.items()}
        leg_direction = leg or "buy_yes"  # buy_yes / buy_no / sell (the *leg* we intend)
        cap = order_plan.cap_for(leg_direction, caps, price_cap)
        budget = budget_usdc or (SCENARIO["budget_usdc"] if scenario else "3")
        report["leg"] = leg_direction

        client = _build_client(credentials)
        report["sentinel"] = install_sentinels(client)
        if not report["sentinel"]["proof"] or not all(p.get("blocked") for p in report["sentinel"]["proof"]):
            raise RuntimeError("sentinels did not arm — refusing to continue")
        required = {f"client.{name}" for name in SUBMIT_METHODS}
        absent = required - set(report["sentinel"]["installed"])
        if absent:
            raise RuntimeError(f"order-submit sentinels missing: {sorted(absent)} — refusing to continue")

        if scenario:
            market, book = dict(SCENARIO["market"]), dict(SCENARIO["book"])
            report["discovery"] = {"attempts": [{"key": "scenario", "status": "synthetic"}]}
        else:
            found = discover_market(client, city=city, local_date=local_date, direction=direction,
                                    token_id=token_id, cap=cap, timeout=timeout, state_path=state_path)
            market, book = found["market"], found["book"]
            report["discovery"] = {"attempts": found["attempts"]}
        report["market"] = dict(market)
        report["book"] = dict(book)

        plan = order_plan.plan_order(direction=leg_direction, token_id=market.get("token_id"), book=book,
                                     budget_usdc=budget, price_cap=cap)
        report["plan"] = _plan_json(plan)
        if not plan["ok"]:
            report["reason"] = f"order_plan:{plan['reason']}"
            return report

        plan = {**plan, "neg_risk": bool(book.get("neg_risk"))}
        signed = _sign(client, plan, credentials, offline=scenario)
        report["signed_order"] = _describe_signed(
            signed,
            _order_hash(signed, credentials, plan),
            "signature intentionally not persisted — a Phase-2 artifact must stay unsubmittable",
        )
        report["submit"] = {
            "attempted": False,
            "sentinel_blocked_methods": report["sentinel"]["installed"],
            "statement": "NO ORDER SUBMITTED — local signing only; every order / cancel / RFQ / credential-admin / state-write method is sentinel-blocked",
        }
        report["ok"] = True
    except Exception as exc:  # fail closed, keep the partial snapshot
        report["ok"] = False
        try:
            report["reason"] = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
        except Exception:  # pragma: no cover - non-string values in a hand-built env
            report["reason"] = f"{type(exc).__name__}: <detail withheld: non-string value in env>"
    return report


def human_summary(report: dict) -> str:
    sentinel = report.get("sentinel") or {}
    proofs = sentinel.get("proof") or []
    lines = [
        "=== live dry-run (Phase 2) — NO ORDER SUBMITTED / 未提交任何订单 ===",
        f"ok={report.get('ok')} reason={report.get('reason')} scenario={report.get('scenario')} "
        f"mode={report.get('mode')} ts={report.get('ts_utc')}",
        f"sentinel: {len(sentinel.get('installed') or [])} armed, "
        f"{sum(1 for p in proofs if p.get('blocked'))}/{len(proofs)} proven blocked"
        + (f", missing={sentinel['missing']}" if sentinel.get("missing") else ""),
    ]
    for proof in proofs:
        where = proof.get("target", "client")
        lines.append(f"  {where}.{proof['method']}() -> "
                     + ("BLOCKED" if proof.get("blocked") else "NOT BLOCKED")
                     + (f": {proof.get('error')}" if proof.get("blocked") else ""))
    market = report.get("market")
    if market:
        lines.append(f"market: {market.get('city')} {market.get('local_date')} {market.get('direction')} "
                     f"bucket={market.get('bucket')} token={str(market.get('token_id'))[:12]}…")
    book = report.get("book")
    if book:
        lines.append(f"book: best_ask={book.get('best_ask')} best_bid={book.get('best_bid')} "
                     f"tick={book.get('tick_size')} min_order_size={book.get('min_order_size')} "
                     f"neg_risk={book.get('neg_risk')}")
    plan = report.get("plan")
    if plan:
        lines.append(f"plan: {plan.get('direction')} {plan.get('side')} price={plan.get('price')} "
                     f"size={plan.get('size')} max_cost={plan.get('max_cost_usdc')} USDC "
                     f"(budget={plan.get('budget_usdc')}, cap={plan.get('price_cap')}"
                     + (f", reason={plan.get('reason')}: {plan.get('detail')}" if not plan.get("ok") else "")
                     + ")")
    signed = report.get("signed_order")
    if signed:
        lines.append(f"signed: hash={signed.get('hash')} maker_amount={signed.get('maker_amount')} "
                     f"taker_amount={signed.get('taker_amount')} "
                     f"signature={'present(' + str(signed.get('signature_length')) + ' chars, not persisted)' if signed.get('signature_present') else 'MISSING'}")
    submit = report.get("submit") or {}
    lines.append(f"submit: attempted={submit.get('attempted')} — {submit.get('statement')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-2 dry-run: sign locally, submit nothing")
    parser.add_argument("--confirm-dryrun", action="store_true",
                        help="required acknowledgement that this is a signing-only dry run")
    parser.add_argument("--scenario", action="store_true", help="offline synthetic book (no network)")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"report path (default {DEFAULT_OUT})")
    parser.add_argument("--city")
    parser.add_argument("--date")
    parser.add_argument("--direction")
    parser.add_argument("--leg", help="buy_yes (default) / buy_no / sell")
    parser.add_argument("--token-id")
    parser.add_argument("--budget-usdc")
    parser.add_argument("--price-cap")
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args(argv)

    if not args.confirm_dryrun:
        report = run_dryrun(scenario=args.scenario, confirm=False)
        print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else human_summary(report))
        print("refusing to run without --confirm-dryrun (nothing built, nothing signed, nothing written)")
        return 2

    report = run_dryrun(scenario=args.scenario, confirm=True, city=args.city, local_date=args.date,
                        direction=args.direction, leg=args.leg, token_id=args.token_id,
                        budget_usdc=args.budget_usdc, price_cap=args.price_cap, timeout=args.timeout)

    wrote = ""
    if report.get("sentinel", {}).get("armed") or report.get("ok"):
        out_path = Path(args.out)
        try:
            if out_path.parent != Path(""):
                out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            wrote = f"\nwrote {out_path}"
        except OSError as exc:
            wrote = f"\ncould not write {out_path}: {exc}"

    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else human_summary(report) + wrote)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
