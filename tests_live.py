#!/usr/bin/env python3
"""stdlib-only tests for the live layer — Phase 1 (read-only) + Phase 2 (dry-run)
+ Phase 3 (gated submit channel). No network, no py-clob-client, no real order.

Run:  python3.13 tests_live.py    → prints PASS/FAIL per check, exit = #failures.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from live import (clob_client, creds, order_plan, port, reconcile, risk_gate, sign_dryrun,
                  smoke, submit, v2_transport)

ROOT = Path(__file__).resolve().parent

# Module-scope safeguard: any submit/cancel the suite makes that forgets to patch AUDIT_PATH
# lands in a throwaway file, never in the real data/live_events.jsonl (audit integrity).
TMP_AUDIT_DIR = tempfile.mkdtemp(prefix="live-tests-audit-")
submit.AUDIT_PATH = Path(TMP_AUDIT_DIR) / "live_events.jsonl"

PRIVATE_KEY = "0x" + "ab" * 32
FUNDER = "0x" + "cd" * 20
MAX_UINT = str(2 ** 256 - 1)

VALID_ENV = {
    "POLY_PRIVATE_KEY": PRIVATE_KEY,
    "POLY_FUNDER_ADDRESS": FUNDER,
    "POLY_SIGNATURE_TYPE": "1",
    "POLY_API_KEY": "api-key-opaque",
    "POLY_API_SECRET": "api-secret-opaque",
    "POLY_API_PASSPHRASE": "pass-opaque",
    "YES2RE_MODE": "live",
    "LIVE_FIRE_BUDGET_USDC": "10",
    "LIVE_MAX_OPEN_POSITIONS": "22",
    "LIVE_MAX_CAPITAL_USDC": "500",
}

BALANCE_ALLOWANCE = {
    "balance": "51713622",
    "allowances": {
        "0xExchange1": MAX_UINT,
        "0xExchange2": "0",
        "0xExchange3": "1500000",
    },
}

POSITIONS = [
    {"asset": "1", "conditionId": "0xc1", "outcome": "No", "size": 20.0, "avgPrice": 0.5,
     "curPrice": 0.62, "currentValue": 12.5, "cashPnl": 2.4, "title": "Highest temp NYC",
     "slug": "highest-temp-nyc", "eventSlug": "highest-temp-nyc-2026-09-10", "redeemable": False},
    {"asset": "2", "conditionId": "0xc2", "outcome": "Yes", "size": 1.0, "avgPrice": 0.04,
     "curPrice": 0.05, "currentValue": 0.05, "title": "dust", "slug": "dust", "eventSlug": "dust"},
]


class _Fail(RuntimeError):
    pass


def _patch(**overrides):
    """Swap clob_client I/O for in-memory fakes (reads only)."""
    defs = {
        "build_client": lambda creds_, host=None, chain_id=None: object(),
        "get_balance_allowance": lambda client: dict(BALANCE_ALLOWANCE),
        "get_open_orders": lambda client: [],
        "fetch_positions": lambda address, limit=500, timeout=25: [dict(p) for p in POSITIONS],
        "fetch_egress_info": lambda timeout=15: {"ip": "203.0.113.7", "country": "MY", "org": "AS0 Test"},
    }
    defs.update(overrides)
    originals = {}
    for name, value in defs.items():
        originals[name] = getattr(clob_client, name)
        setattr(clob_client, name, value)
    return originals


def _restore(originals):
    for name, value in originals.items():
        setattr(clob_client, name, value)


# --------------------------------------------------------------------------- risk gate

def test_risk_gate_allow():
    ok = dict(usdc_balance="51.71", open_positions=1, committed_usdc="12.5",
              fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    got = risk_gate.evaluate(**ok)
    assert got["allow"] is True and got["reason"] == risk_gate.OK, got
    assert set(got) == {"allow", "reason", "detail"}, got


def test_risk_gate_deny_codes():
    base = dict(usdc_balance="100", open_positions=1, committed_usdc="5",
                fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    cases = [
        ({"usdc_balance": "9.99"}, risk_gate.BUDGET_EXCEEDS_BALANCE),
        ({"usdc_balance": "0"}, risk_gate.INSUFFICIENT_BALANCE),
        ({"open_positions": 22}, risk_gate.MAX_OPEN_POSITIONS),
        ({"open_positions": 30}, risk_gate.MAX_OPEN_POSITIONS),
        ({"committed_usdc": "495"}, risk_gate.CAPITAL_CAP),
        ({"max_capital_usdc": "105", "committed_usdc": "100"}, risk_gate.CAPITAL_CAP),
    ]
    for override, expected in cases:
        got = risk_gate.evaluate(**{**base, **override})
        assert got["allow"] is False and got["reason"] == expected, (override, got)
        assert got["detail"], got


def test_risk_gate_fail_closed():
    base = dict(usdc_balance="100", open_positions=1, committed_usdc="5",
                fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    bad = [
        {"usdc_balance": None},
        {"open_positions": None},
        {"committed_usdc": "abc"},
        {"fire_budget_usdc": "0"},
        {"fire_budget_usdc": "-1"},
        {"max_open_positions": ""},
        {"max_capital_usdc": "NaN"},
        {"usdc_balance": "Infinity"},
        {"usdc_balance": True},
        {"open_positions": 1.5},
    ]
    for override in bad:
        got = risk_gate.evaluate(**{**base, **override})
        assert got["allow"] is False and got["reason"] == risk_gate.INVALID_INPUT, (override, got)
    # missing limit entirely (env key absent -> None) still denies
    got = risk_gate.evaluate(**{**base, "max_capital_usdc": None})
    assert got["reason"] == risk_gate.INVALID_INPUT, got


# --------------------------------------------------------------------------- creds

def test_creds_validate_ok():
    got = creds.validate_creds(VALID_ENV)
    assert got["signature_type"] == 1 and isinstance(got["signature_type"], int), got
    assert got["funder_address"] == FUNDER and got["private_key"] == PRIVATE_KEY
    assert got["api_key"] == "api-key-opaque"


def test_creds_optional_api_trio():
    env = {k: v for k, v in VALID_ENV.items() if not k.startswith("POLY_API_")}
    assert creds.validate_creds(env)["api_key"] is None
    partial = dict(VALID_ENV)
    partial.pop("POLY_API_SECRET")
    try:
        creds.validate_creds(partial)
    except creds.CredError as exc:
        assert "POLY_API_SECRET" in str(exc) and "api-secret-opaque" not in str(exc), exc
    else:
        raise AssertionError("partial API trio must be rejected")


def test_creds_rejects_bad_formats():
    bad = {
        "POLY_PRIVATE_KEY": "0x" + "ab" * 31,          # 62 hex
        "POLY_FUNDER_ADDRESS": "0x" + "cd" * 21,       # 42 hex
        "POLY_SIGNATURE_TYPE": "3",
    }
    for key, value in bad.items():
        try:
            creds.validate_creds({**VALID_ENV, key: value})
        except creds.CredError as exc:
            assert key in str(exc), exc
            assert value not in str(exc), "error must not echo the value"
        else:
            raise AssertionError(f"{key}={value!r} must be rejected")
    for key in ("POLY_PRIVATE_KEY", "POLY_FUNDER_ADDRESS", "POLY_SIGNATURE_TYPE"):
        try:
            creds.validate_creds({k: v for k, v in VALID_ENV.items() if k != key})
        except creds.CredError as exc:
            assert key in str(exc), exc
        else:
            raise AssertionError(f"missing {key} must be rejected")


def test_creds_env_file_and_mask():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(
            "# comment\n"
            "export POLY_PRIVATE_KEY=%s\n"
            "POLY_FUNDER_ADDRESS=%s\n"
            '\nPOLY_SIGNATURE_TYPE="2"\n' % (PRIVATE_KEY, FUNDER),
            encoding="utf-8",
        )
        env = creds.load_env_file(path)
        assert env["POLY_SIGNATURE_TYPE"] == "2" and env["POLY_PRIVATE_KEY"] == PRIVATE_KEY
        loaded = creds.validate_creds(env)
        assert loaded["signature_type"] == 2
    masked = creds.mask(PRIVATE_KEY)
    assert masked == PRIVATE_KEY[:8] + "…" + PRIVATE_KEY[-4:], masked
    assert PRIVATE_KEY not in masked and len(masked) < len(PRIVATE_KEY)
    assert creds.mask(None) == "***" and creds.mask("short") == "***"
    assert "api-secret-opaque" not in creds.sanitize(
        f"boom api-secret-opaque", VALID_ENV)


# --------------------------------------------------------------------------- reconcile

def test_reconcile_structure():
    originals = _patch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            report = reconcile.collect(
                {**VALID_ENV, "http_proxy": "http://127.0.0.1:9"}, timeout=5
            )
    finally:
        _restore(originals)
    expected = {"ok", "reason", "ts_utc", "mode", "api_ok", "usdc_balance", "allowances",
                "open_orders", "positions", "positions_raw_count", "positions_value_usdc",
                "risk_gate", "limits",
                "egress_ip", "egress_country", "egress_org"}
    assert expected <= set(report), sorted(expected - set(report))
    assert report["ok"] is True and report["reason"] is None
    assert report["api_ok"] is True and report["mode"] == "live"
    assert report["usdc_balance"] == 51.713622, report["usdc_balance"]
    assert report["allowances"] == {"0xExchange1": "max", "0xExchange2": 0, "0xExchange3": 1.5}
    assert report["open_orders"] == 0
    assert len(report["positions"]) == 1, "currentValue <= 0.1 must be filtered out"
    assert report["positions_raw_count"] == 2, report["positions_raw_count"]
    assert report["positions"][0]["asset"] == "1"
    assert report["positions_value_usdc"] == 12.5
    assert report["risk_gate"]["allow"] is True, report["risk_gate"]
    assert report["egress_ip"] == "203.0.113.7" and report["egress_country"] == "MY"
    assert PRIVATE_KEY not in json.dumps(report) and FUNDER not in json.dumps(report)


def test_reconcile_gate_denies_on_real_balance():
    originals = _patch(get_balance_allowance=lambda client: {"balance": "5000000", "allowances": {}})
    try:
        report = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    assert report["usdc_balance"] == 5.0
    assert report["risk_gate"]["allow"] is False
    assert report["risk_gate"]["reason"] == risk_gate.BUDGET_EXCEEDS_BALANCE


def test_reconcile_non_mapping_env():
    """collect() must honour "Never raises": a bad env returns the fail-closed shape."""
    originals = _patch()
    try:
        reference = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    for bad in ([], "env", 1, 1.5, object(), ["a"], {"nested": 1}.keys()):
        try:
            got = reconcile.collect(bad)
        except Exception as exc:  # noqa: BLE001 - not raising is the whole point
            raise AssertionError(f"collect({bad!r}) raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False, (bad, got)
        assert got["reason"] == "env: not a mapping", (bad, got["reason"])
        assert set(got) == set(reference), sorted(set(reference) ^ set(got))
        assert got["ts_utc"].endswith("Z") and got["limits"], got


def test_reconcile_non_string_values():
    """dict-shaped but ill-typed env values must not raise either (F2)."""
    originals = _patch()
    try:
        reference = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    cases = [
        {"YES2RE_MODE": 1},                                   # was: AttributeError .strip()
        {"POLY_PRIVATE_KEY": 12345678},                        # was: TypeError len(int)
        {"POLY_API_KEY": 1, "LIVE_MAX_CAPITAL_USDC": 50},      # was: TypeError len(int)
        {"LIVE_FIRE_BUDGET_USDC": 5, "POLY_SIGNATURE_TYPE": 1, "POLY_FUNDER_ADDRESS": 42},
        {"POLY_PRIVATE_KEY": None, "POLY_FUNDER_ADDRESS": None, "POLY_SIGNATURE_TYPE": None},
    ]
    for env in cases:
        try:
            got = reconcile.collect(env)
        except Exception as exc:  # noqa: BLE001 - not raising is the whole point
            raise AssertionError(f"collect({env!r}) raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False, (env, got)
        assert set(got) == set(reference), sorted(set(reference) ^ set(got))
        assert isinstance(got["reason"], str) and got["reason"], (env, got["reason"])
    assert reconcile.collect({"YES2RE_MODE": 1})["mode"] == "1", "non-str mode is stringified"
    assert "12345678" not in reconcile.collect({"POLY_PRIVATE_KEY": 12345678})["reason"], \
        "non-string value must not be echoed into the report"


def test_reconcile_fail_closed():
    # missing creds
    report = reconcile.collect({})
    assert report["ok"] is False and "POLY_PRIVATE_KEY" in report["reason"], report["reason"]

    # network/library failure + partial snapshot kept
    originals = _patch(build_client=lambda *a, **k: (_ for _ in ()).throw(_Fail("client exploded")))
    try:
        report = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    assert report["ok"] is False and report["api_ok"] is False
    assert "_Fail: client exploded" in report["reason"], report["reason"]

    # never leaks a secret inside the error text
    originals = _patch(build_client=lambda *a, **k: (_ for _ in ()).throw(
        _Fail(f"bad key {PRIVATE_KEY}")))
    try:
        report = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    assert PRIVATE_KEY not in report["reason"], report["reason"]


def test_reconcile_cli_json_and_exit_codes():
    originals = _patch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "live_reconcile.json"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = reconcile.main(["--json", "--out", str(out)])
            assert code == 0, code
            printed = json.loads(buf.getvalue())
            on_disk = json.loads(out.read_text(encoding="utf-8"))
            assert printed["ok"] and on_disk["ok"]
            assert printed["ts_utc"].endswith("Z")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = reconcile.main(["--out", str(out)])
            assert code == 0 and "risk_gate allow=True" in buf.getvalue(), buf.getvalue()
            assert PRIVATE_KEY not in buf.getvalue()

        originals2 = _patch(build_client=lambda *a, **k: (_ for _ in ()).throw(_Fail("down")))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = reconcile.main(["--out", "/tmp/does-not-matter-live.json"])
            assert code == 2 and "reason=" in buf.getvalue(), buf.getvalue()
        finally:
            _restore(originals2)
    finally:
        _restore(originals)
    Path("/tmp/does-not-matter-live.json").unlink(missing_ok=True)


# --------------------------------------------------------------------------- read-only guard

#: names that must never appear as a *real call* anywhere in live/
FORBIDDEN_CALLS = (
    "create_and_post_order", "post_order", "post_orders", "create_market_order",
    "cancel", "cancel_order", "cancel_orders", "cancel_all", "cancel_market_orders",
    "create_api_key", "derive_api_key", "delete_api_key", "update_balance_allowance",
    "post_heartbeat", "delete_readonly_api_key", "submit",
)
#: signing entry point (local only) — allowed in the phase-2 dry run and the submit channel
SIGN_ONLY_CALLS = ("create_order",)
#: the controlled write channels (v1 submit.py, v2 v2_transport.py) — nowhere else
SUBMIT_MODULE = "submit.py"
WRITE_CHANNEL_MODULES = (SUBMIT_MODULE, "v2_transport.py")
#: write calls allowed inside those channels only
CONTROLLED_WRITE_CALLS = ("create_order", "post_order", "cancel", "cancel_order", "cancel_orders")
#: v2-client-only methods: inside the v2 transport they must stay pure data (never a code use);
#: callers may only reach them through the v1 channel's module function (``submit.cancel_order``)
V2_CLIENT_ONLY = ("cancel_order",)
#: modules a caller may delegate a write call to (``submit.cancel_order(...)`` etc.)
DELEGATE_MODULES = {"submit", "sign_dryrun", "v2_transport"}
#: names that may appear in SUBMIT_MODULE only as their single call site
CHANNEL_ONLY = ("post_order", "cancel", "cancel_orders")
#: module-name style uses we do not police (``submit.audit(...)`` in the orchestrator)
NAME_USAGE_SKIP = {"submit"}


def test_static_no_order_path():
    """Write-path confinement, AST-based (Phase 1/2 rule, tightened for Phase 3).

    Phase 1/2 forbade write calls outright. Phase 3 introduces exactly one controlled
    channel, so the invariant becomes:

    * every write-class call lives in ``live/submit.py`` and nowhere else;
    * there is exactly **one** ``post_order`` call site in the whole package (one auditable
      chokepoint) and exactly one ``cancel``;
    * forbidden names appear in ``submit.py`` only as those call sites — never as a
      free-standing attribute/name that could be handed to something else;
    * phase-2 names (``post_orders``/``cancel_all``/RFQ/credential-admin/state-write) remain
      *data only*: string literals and ``setattr`` targets, never calls;
    * no module may call a computed function (``f()()``) — a hidable write path.
    """
    files = sorted((ROOT / "live").glob("*.py"))
    assert files, "no live/ modules found"
    calls = {name: [] for name in CONTROLLED_WRITE_CALLS}
    uses = {name: [] for name in FORBIDDEN_CALLS if name not in NAME_USAGE_SKIP}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                else:
                    assert not isinstance(func, ast.Call), (
                        f"{path.name}:{node.lineno} calls a computed function (f()()) — hidable write path"
                    )
                    name = None
                if name in FORBIDDEN_CALLS:
                    delegated = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                                 and func.value.id in DELEGATE_MODULES)
                    assert path.name in WRITE_CHANNEL_MODULES or delegated, (
                        f"{path.name}:{node.lineno} calls {name}() directly — only "
                        f"{WRITE_CHANNEL_MODULES} may write (or delegate via {sorted(DELEGATE_MODULES)})"
                    )
                if name in calls:
                    calls[name].append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in uses:
                uses[node.attr].append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in uses:
                uses[node.id].append(f"{path.name}:{node.lineno}")

    # exactly one order-placing call site in the package, and one cancel site — both in v2
    assert sorted(site.split(":")[0] for site in calls["post_order"]) == ["v2_transport.py"], \
        calls["post_order"]
    assert not calls["cancel"], f"the v1 cancel() call is gone: {calls['cancel']}"
    assert {site.split(":")[0] for site in calls["cancel_orders"]} == {"v2_transport.py"}, calls["cancel_orders"]
    assert {site.split(":")[0] for site in calls["create_order"]} == \
        {"v2_transport.py", "sign_dryrun.py"}, calls["create_order"]
    # the channels' write calls may only appear as those call sites (never as values)
    for name in CHANNEL_ONLY:
        assert len(uses.get(name, [])) == len(calls[name]), (name, uses.get(name), calls[name])
        for site in uses.get(name, []):
            assert site.split(":")[0] in WRITE_CHANNEL_MODULES, (name, site)
    # every other forbidden name stays data-only (string literal / setattr target)
    for name in FORBIDDEN_CALLS:
        if name in CHANNEL_ONLY or name in NAME_USAGE_SKIP or name in V2_CLIENT_ONLY:
            continue
        assert not uses.get(name), f"{name} must stay data-only, found code use at {uses[name]}"
    # v2-client-only methods: no code use inside the v2 transport (it uses cancel_orders([id]))
    for name in V2_CLIENT_ONLY:
        leaked = [site for site in uses.get(name, []) if site.startswith("v2_transport.py:")]
        assert not leaked, f"{name} must stay data-only inside v2_transport.py: {leaked}"

    # the smoke orchestrator must reach the write path only through the channel module
    smoke_text = (ROOT / "live" / "smoke.py").read_text(encoding="utf-8")
    assert "submit.submit_order" in smoke_text and "submit.cancel_order" in smoke_text, \
        "smoke must submit/cancel through live/submit.py"
    signers = {
        path.name for path in files
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in SIGN_ONLY_CALLS
               for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert signers == {"sign_dryrun.py", "v2_transport.py"}, f"unexpected signing modules: {signers}"


# --------------------------------------------------------------------------- Phase 2: order_plan

PLAN_BOOK = {"best_ask": "0.523", "best_bid": "0.517", "tick_size": "0.01", "min_order_size": "5"}


def test_order_plan_tick_alignment_and_rounding():
    plan = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                 budget_usdc="3", price_cap="0.9")
    assert plan["ok"] and plan["reason"] == order_plan.OK, plan
    assert plan["side"] == "BUY" and plan["price"] == Decimal("0.52"), plan
    assert plan["tick"] == Decimal("0.01"), plan
    assert plan["size"] == Decimal("5.76"), plan            # 3 / 0.52, rounded down at 2dp
    assert plan["max_cost_usdc"] <= Decimal("3"), plan
    assert set(plan) >= {"ok", "reason", "price", "size", "max_cost_usdc", "tick"}

    fine = order_plan.plan_order(direction="buy_yes", token_id="T",
                                 book={**PLAN_BOOK, "tick_size": "0.001"},
                                 budget_usdc="3", price_cap="0.9")
    assert fine["price"] == Decimal("0.523"), fine          # 1-tick market: nothing to round
    assert fine["tick"] == Decimal("0.001"), fine

    coarse = order_plan.plan_order(direction="buy_yes", token_id="T",
                                   book={**PLAN_BOOK, "tick_size": "0.1"},
                                   budget_usdc="3", price_cap="0.9")
    assert coarse["price"] == Decimal("0.5"), coarse        # aligned DOWN to the tick

    six = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                budget_usdc="3", price_cap="0.9", size_decimals=6)
    assert six["size"] == Decimal("5.769230"), six          # 3 / 0.52 rounded down at 6dp
    assert order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                 budget_usdc="3", price_cap="0.9",
                                 size_decimals=9)["reason"] == order_plan.INVALID_INPUT

    override = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                     budget_usdc="3", price_cap="0.9",
                                     tick_size="0.1", min_order_size="1")
    assert override["price"] == Decimal("0.5") and override["size"] == Decimal("6.00"), override


def test_order_plan_deny_branches():
    base = dict(direction="buy_yes", token_id="T", book=PLAN_BOOK, budget_usdc="3", price_cap="0.9")
    no_min_book = {key: value for key, value in PLAN_BOOK.items() if key != "min_order_size"}
    cases = [
        (order_plan.PRICE_ABOVE_CAP, {**base, "book": {**PLAN_BOOK, "best_ask": "0.95"}}),
        (order_plan.PRICE_ABOVE_CAP, {**base, "price_cap": "0.5"}),
        (order_plan.BELOW_MIN_ORDER_SIZE, {**base, "budget_usdc": "1"}),
        (order_plan.INSUFFICIENT_BUDGET, {**base, "budget_usdc": "0.002"}),
        (order_plan.NO_BOOK, {**base, "book": {}}),
        (order_plan.NO_BOOK, {**base, "book": {**PLAN_BOOK, "best_ask": None, "asks": []}}),
        (order_plan.INVALID_INPUT, {**base, "direction": "buy"}),
        (order_plan.INVALID_INPUT, {**base, "token_id": None}),
        (order_plan.INVALID_INPUT, {**base, "token_id": "   "}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "0"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "-1"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "NaN"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": None}),
        (order_plan.INVALID_INPUT, {**base, "price_cap": "abc"}),
        (order_plan.INVALID_INPUT, {**base, "price_cap": "1.5"}),
        (order_plan.INVALID_INPUT, {**base, "min_order_size": None, "book": no_min_book}),
        (order_plan.INVALID_INPUT, {**base, "book": {**PLAN_BOOK, "tick_size": "0"}}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "1e24"}),     # F3: huge but finite
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "1e400"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "1e13"}),
    ]
    for expected, kwargs in cases:
        got = order_plan.plan_order(**kwargs)
        assert got["ok"] is False and got["reason"] == expected, (kwargs, got)
        assert got["price"] is None and got["size"] is None and got["max_cost_usdc"] is None, got
        assert got["detail"], got


def test_order_plan_sell_and_caps():
    sell = order_plan.plan_order(direction="sell", token_id="T", book=PLAN_BOOK,
                                 budget_usdc="3", price_cap=None)
    assert sell["ok"] and sell["side"] == "SELL" and sell["price"] == Decimal("0.51"), sell

    caps = order_plan.load_caps()
    assert caps["no_max_ask"] == Decimal("1.0") and caps["yes_max_ask"] == Decimal("0.9"), caps
    assert order_plan.cap_for("buy_no", caps) == Decimal("1.0")
    assert order_plan.cap_for("buy_yes", caps) == Decimal("0.9")
    assert order_plan.cap_for("sell", caps) is None
    assert order_plan.cap_for("buy_yes", caps, "0.48") == Decimal("0.48")
    assert order_plan.cap_for("buy_yes", caps, "nonsense") is None
    assert order_plan.cap_for("buy_yes", caps, "nonsense") is None

    # F3: a huge-but-finite budget must fail closed, never raise
    for budget in ("1e24", "1e400"):
        try:
            got = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                        budget_usdc=budget, price_cap="0.9")
        except Exception as exc:  # noqa: BLE001 - raising is the bug being fixed
            raise AssertionError(f"budget {budget} raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False and got["reason"] == order_plan.INVALID_INPUT, (budget, got)
    assert order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                 budget_usdc=str(order_plan.MAX_BUDGET_USDC),
                                 price_cap="0.9")["ok"] is True


def test_load_caps_fails_closed():
    """F5: a cap that cannot be read must never degrade into "no limit"."""
    assert issubclass(order_plan.CapsError, ValueError)
    assert order_plan.CAP_KEYS == ("no_max_ask", "yes_max_ask")
    variants = [
        ('{"strategy":{"yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"1.0"}}', "yes_max_ask"),
        ('{"strategy":{"no_max_ask":null,"yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"abc","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"NaN","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"1.5","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"0","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"1.0","yes_max_ask":"-1"}}', "yes_max_ask"),
        ("{}", "no_max_ask"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for body, key in variants:
            path = Path(tmp) / "cfg.json"
            path.write_text(body, encoding="utf-8")
            try:
                order_plan.load_caps(path)
            except order_plan.CapsError as exc:
                assert key in str(exc), (body, exc)
            else:
                raise AssertionError(f"{body} must fail closed (missing/invalid {key})")
        good = Path(tmp) / "good.json"
        good.write_text('{"strategy":{"no_max_ask":"0.85","yes_max_ask":"0.48"}}', encoding="utf-8")
        assert order_plan.load_caps(good) == {"no_max_ask": Decimal("0.85"),
                                             "yes_max_ask": Decimal("0.48")}


def test_dryrun_fails_closed_when_caps_unreadable():
    """F5 end-to-end: an unreadable cap ⇒ ok:false (run_dryrun calls load_caps inside its try)."""
    def _boom(*_a, **_k):
        raise order_plan.CapsError("strategy.no_max_ask missing from config")


# --------------------------------------------------------------------------- Phase 2: sentinels

#: the write surface is the **v2** one now (v1 orders have been rejected since 2026-04-28)
SUBMIT_METHOD_NAMES = tuple(v2_transport.SUBMIT_METHODS)
ADMIN_METHOD_NAMES = tuple(v2_transport.ADMIN_METHODS)
RFQ_METHOD_NAMES = tuple(v2_transport.RFQ_SUBMIT_METHODS)
STATE_WRITE_METHOD_NAMES = tuple(v2_transport.STATE_WRITE_METHODS)
ALL_WRITE_METHOD_NAMES = SUBMIT_METHOD_NAMES + ADMIN_METHOD_NAMES + STATE_WRITE_METHOD_NAMES


class _FakeSigned:
    """Stands in for v2's ``SignedOrderV2`` (a dataclass: attributes, no ``.dict()``)."""

    def __init__(self, signature="0x" + "ab" * 65):
        self.salt = "1"
        self.maker = "0xmaker"
        self.signer = "0xsigner"
        self.tokenId = "9" * 20
        self.makerAmount = "2995200"
        self.takerAmount = "5760000"
        self.side = 0
        self.signatureType = 1
        self.timestamp = "1789000000000"
        self.metadata = "0x" + "0" * 64
        self.builder = "0x" + "0" * 64
        self.expiration = "0"
        self.signature = signature


class _FakeRfq:
    """Stands in for the client's RFQ sub-client (a second order-entry surface)."""


class _FakeClient:
    """Stands in for the **v2** ClobClient — write methods return a marker until sentinels arm."""

    def __init__(self):
        self.rfq = _FakeRfq()

    def create_order(self, *args, **kwargs):
        return _FakeSigned()

    def post_order(self, *args, **kwargs):
        return {"called": "post_order", "orderID": "ORD-1"}

    def cancel_orders(self, *args, **kwargs):
        return {"called": "cancel_orders", "canceled": [str(x) for x in (args[0] if args else [])]}

    def get_open_orders(self):
        return []


_MARKER = (lambda name: lambda self, *a, **k: {"called": name})
for _name in ALL_WRITE_METHOD_NAMES + RFQ_METHOD_NAMES:
    if _name in ("post_order", "cancel_orders"):        # the released writes keep real behaviour
        continue
    setattr(_FakeRfq if _name in RFQ_METHOD_NAMES else _FakeClient, _name, _MARKER(_name))


def _patch_dryrun(**overrides):
    defs = {
        "_build_client": lambda creds_: _FakeClient(),
        "_sign": lambda client, plan, creds_, offline=False: _FakeSigned(),
        "_order_hash": lambda signed, creds_, plan: "0x" + "de" * 32,
    }
    defs.update(overrides)
    originals = {name: getattr(sign_dryrun, name) for name in defs}
    for name, value in defs.items():
        setattr(sign_dryrun, name, value)
    return originals


def _restore_dryrun(originals):
    for name, value in originals.items():
        setattr(sign_dryrun, name, value)


def test_sentinels_are_load_bearing():
    client = _FakeClient()
    # before arming these would happily "submit" — that is what makes the arm load-bearing
    assert client.post_order().get("called") == "post_order"
    assert client.rfq.create_rfq_quote() == {"called": "create_rfq_quote"}
    before = {name: getattr(client, name) for name in SUBMIT_METHOD_NAMES}
    before_rfq = {name: getattr(client.rfq, name) for name in RFQ_METHOD_NAMES}

    armed = sign_dryrun.install_sentinels(client)
    assert armed["armed"] is True, armed
    for name in SUBMIT_METHOD_NAMES:
        assert f"client.{name}" in armed["installed"], armed
    for name in RFQ_METHOD_NAMES:
        assert f"client.rfq.{name}" in armed["installed"], armed
    for name in STATE_WRITE_METHOD_NAMES:      # F4: heartbeat / notification state writes
        assert f"client.{name}" in armed["installed"], armed
    assert len(armed["proof"]) == len(armed["installed"]), armed["proof"]
    for proof in armed["proof"]:
        assert proof["blocked"] is True, proof
        assert proof["patched"] is True, proof
        assert proof["target"] in ("client", "client.rfq"), proof
        assert "SUBMIT BLOCKED" in proof["error"], proof

    for name in SUBMIT_METHOD_NAMES:
        method = getattr(client, name)
        assert method is not before[name], f"{name} was not replaced"
        assert getattr(method, "dryrun_sentinel_for", None) == name, name
        try:
            method()
        except RuntimeError as exc:
            assert "SUBMIT BLOCKED" in str(exc), exc
        else:
            raise AssertionError(f"{name}() did not raise — sentinel is not load-bearing")

    for name in RFQ_METHOD_NAMES:  # the second order-entry surface is closed too
        method = getattr(client.rfq, name)
        assert method is not before_rfq[name], f"client.rfq.{name} was not replaced"
        try:
            method()
        except RuntimeError as exc:
            assert "SUBMIT BLOCKED" in str(exc), exc
        else:
            raise AssertionError(f"client.rfq.{name}() did not raise — sentinel is not load-bearing")


def test_sentinel_refuses_when_methods_absent():
    """If a library version drops an order-submit method, the dry run must refuse to run."""
    class _BareNoCancel:
        rfq = None
        post_order = _MARKER("post_order")

    class _Bare:
        rfq = None

    originals = _patch_dryrun(_build_client=lambda creds_: _BareNoCancel())
    try:
        report = sign_dryrun.run_dryrun(scenario=True, confirm=True, env=VALID_ENV)
    finally:
        _restore_dryrun(originals)
    assert report["ok"] is False, report
    assert "order-submit sentinels missing" in report["reason"], report["reason"]
    assert "client.cancel" in report["reason"], report["reason"]

    bare = sign_dryrun.install_sentinels(_Bare())
    assert bare["armed"] is False and bare["installed"] == [], bare
    assert len(bare["missing"]) == (len(sign_dryrun.SUBMIT_METHODS)
                                    + len(sign_dryrun.ADMIN_METHODS)
                                    + len(sign_dryrun.STATE_WRITE_METHODS)), bare
    assert len(bare["missing"]) == (len(sign_dryrun.SUBMIT_METHODS)
                                    + len(sign_dryrun.ADMIN_METHODS)
                                    + len(sign_dryrun.STATE_WRITE_METHODS)), bare


#: every name matching these prefixes must have a sentinel (F4 coverage)
WRITE_PREFIXES = ("post_", "cancel_", "delete_", "update_", "create_", "drop_",
                  "derive_", "approve_", "accept_", "set_")

#: exemptions, each with the reason it is safe (verified by the Phase-2 audit, §2.3 / §8)
READ_ONLY_WHITELIST = {
    "create_order": "Phase 2's only write call — local EIP-712 signing; audited traffic: GET only",
    "create_market_order": "same local signing path as create_order; audited: 1 GET / 0 non-GET",
    "create_or_derive_api_creds": "composed call; delegates to create_api_key/derive_api_key, both sentineled",
    "set_api_creds": "local-only: rewrites self.creds/self.mode, zero network",
}

#: dir(ClobClient) of py-clob-client-v2 (snapshot so the coverage test runs stdlib-only)
V2_CLIENT_METHODS = (
    "are_orders_scoring",
    "assert_level_1_auth",
    "assert_level_2_auth",
    "calculate_market_price",
    "cancel_all",
    "cancel_market_orders",
    "cancel_order",
    "cancel_orders",
    "create_and_post_market_order",
    "create_and_post_order",
    "create_api_key",
    "create_builder_api_key",
    "create_market_order",
    "create_or_derive_api_key",
    "create_order",
    "create_readonly_api_key",
    "delete_api_key",
    "delete_readonly_api_key",
    "derive_api_key",
    "drop_notifications",
    "get_address",
    "get_api_keys",
    "get_balance_allowance",
    "get_builder_api_keys",
    "get_builder_trades",
    "get_clob_market_info",
    "get_closed_only_mode",
    "get_current_rewards",
    "get_earnings_for_user_for_day",
    "get_fee_exponent",
    "get_fee_rate_bps",
    "get_last_trade_price",
    "get_last_trades_prices",
    "get_market",
    "get_market_trades_events",
    "get_markets",
    "get_midpoint",
    "get_midpoints",
    "get_neg_risk",
    "get_notifications",
    "get_ok",
    "get_open_orders",
    "get_order",
    "get_order_book",
    "get_order_book_hash",
    "get_order_books",
    "get_pre_migration_orders",
    "get_price",
    "get_prices",
    "get_prices_history",
    "get_raw_rewards_for_market",
    "get_readonly_api_keys",
    "get_reward_percentages",
    "get_sampling_markets",
    "get_sampling_simplified_markets",
    "get_server_time",
    "get_simplified_markets",
    "get_spread",
    "get_spreads",
    "get_tick_size",
    "get_total_earnings_for_user_for_day",
    "get_trades",
    "get_trades_paginated",
    "get_user_earnings_and_markets_config",
    "get_version",
    "is_order_scoring",
    "post_heartbeat",
    "post_order",
    "post_orders",
    "revoke_builder_api_key",
    "set_api_creds",
    "update_balance_allowance",
)
#: v2 names that match a write prefix but are safe, each with the reason (audit §2.3/§8)
V2_READ_ONLY_WHITELIST = {
    "create_order": "local EIP-712 signing only — the v2 channel posts what this returns",
    "create_market_order": "same local signing path (never posts by itself)",
    "set_api_creds": "local-only: rewrites self.creds/self.mode, zero network",
}


#: every name matching these prefixes must have a sentinel (F4 coverage)
WRITE_PREFIXES = ("post_", "cancel_", "delete_", "update_", "create_", "drop_",
                  "derive_", "approve_", "accept_", "set_")


def _is_write(name):
    return any(name.startswith(prefix) for prefix in WRITE_PREFIXES)


def _surface(names):
    obj = type("Surface", (), {})()
    for name in names:
        setattr(obj, name, _MARKER(name))
    return obj


def _surface_names(kind):
    """Legacy helper: the v1 surface is gone, so everything is the v2 client now."""
    names, source = _v2_surface_names()
    return tuple(names), source


def _v2_surface_names():
    """Live reflection when py-clob-client-v2 is importable, else the recorded snapshot."""
    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError:
        return tuple(V2_CLIENT_METHODS), "recorded snapshot"
    return tuple(sorted(n for n in dir(ClobClient) if not n.startswith("_"))), "live reflection"


def test_sentinel_coverage_over_client_surface():
    """Every write-looking method of the **v2** client must end up sentineled."""
    names, source = _v2_surface_names()
    for name, reason in V2_READ_ONLY_WHITELIST.items():
        assert name in names, f"stale whitelist entry {name!r} ({reason})"
    client = _surface(names)
    client.rfq = _surface(v2_transport.RFQ_SUBMIT_METHODS)      # v2 may add a nested rfq client
    armed = v2_transport.install_sentinels(client)
    installed = set(armed["installed"])
    assert armed["missing"] == [], armed["missing"]
    assert all(p["blocked"] for p in armed["proof"]), armed["proof"]

    expected = {f"client.{name}" for name in names
                if _is_write(name) and name not in V2_READ_ONLY_WHITELIST}
    expected |= {f"client.rfq.{name}" for name in v2_transport.RFQ_SUBMIT_METHODS if _is_write(name)}
    uncovered = expected - installed
    assert not uncovered, f"[{source}] write methods without a sentinel: {sorted(uncovered)}"
    for name in STATE_WRITE_METHOD_NAMES:      # heartbeat / notification state writes
        assert name in {entry.split(".", 1)[1] for entry in installed}, (name, installed)
    print(f"    ({source}: {len(expected)} write methods covered, "
          f"{len(V2_READ_ONLY_WHITELIST)} whitelisted with reasons)")
def test_scenario_dryrun_offline():
    originals = _patch_dryrun()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "live_order_dryrun.json"
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = sign_dryrun.main(["--scenario", "--confirm-dryrun", "--json", "--out", str(out)])
            assert code == 0, (code, captured.getvalue())
            report = json.loads(out.read_text(encoding="utf-8"))
    finally:
        _restore_dryrun(originals)

    assert report["ok"] is True and report["scenario"] is True, report["reason"]
    assert report["phase"] == "phase2-dryrun"
    assert report["sentinel"]["armed"] is True
    assert report["sentinel"]["proof"] and all(p["blocked"] for p in report["sentinel"]["proof"])
    assert report["plan"]["ok"] is True, report["plan"]
    assert report["plan"]["token_id"] == sign_dryrun.SCENARIO["market"]["token_id"]
    assert report["submit"]["attempted"] is False
    assert "NO ORDER SUBMITTED" in report["submit"]["statement"]
    assert report["signed_order"]["hash"] == "0x" + "de" * 32
    assert report["signed_order"]["signature_present"] is True
    assert report["signed_order"]["maker_amount"] == "2995200"
    assert "signature" not in report["signed_order"], "the raw signature must not be persisted"
    blob = json.dumps(report, ensure_ascii=False)
    assert PRIVATE_KEY not in blob and FUNDER not in blob and "api-secret-opaque" not in blob


def test_dryrun_fail_closed():
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = sign_dryrun.main(["--scenario"])          # no --confirm-dryrun
    assert code == 2 and "confirm_flag_missing" in captured.getvalue(), captured.getvalue()

    report = sign_dryrun.run_dryrun(scenario=True, confirm=True, env={})   # no creds
    assert report["ok"] is False and "POLY_PRIVATE_KEY" in report["reason"], report["reason"]

    originals = _patch_dryrun()
    try:
        report = sign_dryrun.run_dryrun(scenario=True, confirm=True, env=VALID_ENV,
                                        budget_usdc="0.002")
    finally:
        _restore_dryrun(originals)
    assert report["ok"] is False and report["reason"] == "order_plan:insufficient_budget", report["reason"]
    assert report["signed_order"] is None, "nothing may be signed when the plan denies"
    assert all(p["blocked"] for p in report["sentinel"]["proof"]), "proof missing on the deny path"
    assert report["submit"]["attempted"] is False


# --------------------------------------------------------------------------- Phase 3: gates

def _flow_env(**overrides):
    env = {**VALID_ENV, "LIVE_SUBMIT_ENABLED": "1", "LIVE_FIRE_BUDGET_USDC": "5",
           "LIVE_MAX_OPEN_POSITIONS": "22", "LIVE_MAX_CAPITAL_USDC": "500"}
    env.update(overrides)
    return env


@contextlib.contextmanager
def _audit_path(path):
    saved = submit.AUDIT_PATH
    submit.AUDIT_PATH = Path(path)
    try:
        yield Path(path)
    finally:
        submit.AUDIT_PATH = saved


def _audit_lines(path):
    text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_submit_gate_matrix():
    today = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert submit.phrase(today) == "SMOKE-2026-09-10", submit.phrase(today)
    assert submit.phrase(today).startswith("SMOKE-")
    good_env = {"LIVE_SUBMIT_ENABLED": "1"}
    ok = submit.gate_status(enable_submit=True, env=good_env, confirm="SMOKE-2026-09-10", now=today)
    assert ok["ok"] and ok["reason"] == submit.GATE_OK, ok
    matrix = [
        (dict(enable_submit=False, env=good_env, confirm="SMOKE-2026-09-10"), submit.GATE_FLAG),
        (dict(enable_submit=True, env={}, confirm="SMOKE-2026-09-10"), submit.GATE_ENV),
        (dict(enable_submit=True, env={"LIVE_SUBMIT_ENABLED": "0"}, confirm="SMOKE-2026-09-10"), submit.GATE_ENV),
        (dict(enable_submit=True, env={"LIVE_SUBMIT_ENABLED": "true"}, confirm="SMOKE-2026-09-10"), submit.GATE_ENV),
        (dict(enable_submit=True, env=good_env, confirm=None), submit.GATE_CONFIRM_MISSING),
        (dict(enable_submit=True, env=good_env, confirm="  "), submit.GATE_CONFIRM_MISSING),
        (dict(enable_submit=True, env=good_env, confirm="SMOKE-2026-09-09"), submit.GATE_CONFIRM_MISMATCH),
        (dict(enable_submit=True, env=good_env, confirm="letmein"), submit.GATE_CONFIRM_MISMATCH),
    ]
    for kwargs, reason in matrix:
        got = submit.gate_status(now=today, **kwargs)
        assert got["ok"] is False and got["reason"] == reason, (kwargs, got)
        assert got["expected_phrase"] == "SMOKE-2026-09-10", got
    # yesterday's phrase must not work today (no stale replay)
    assert submit.gate_status(enable_submit=True, env=good_env, confirm=submit.phrase(today),
                              now=today + timedelta(days=1))["ok"] is False


def test_submit_non_marketable():
    book = {"best_ask": "0.52", "best_bid": "0.50", "tick_size": "0.01"}
    assert submit.check_non_marketable(side="BUY", price="0.50", book=book)["ok"] is True
    assert submit.check_non_marketable(side="BUY", price="0.51", book=book)["ok"] is True
    for price in ("0.52", "0.53", "0.90"):
        got = submit.check_non_marketable(side="BUY", price=price, book=book)
        assert got["reason"] == submit.NM_VIOLATION, (price, got)
    assert submit.check_non_marketable(side="SELL", price="0.50", book=book)["reason"] == submit.NM_VIOLATION
    assert submit.check_non_marketable(side="SELL", price="0.49", book=book)["reason"] == submit.NM_VIOLATION
    assert submit.check_non_marketable(side="SELL", price="0.51", book=book)["ok"] is True

    assert submit.check_non_marketable(side="BUY", price="0.5", book={})["reason"] == submit.NM_NO_BOOK
    assert submit.check_non_marketable(side="BUY", price="0.5",
                                       book={"best_ask": None})["reason"] == submit.NM_NO_BOOK
    assert submit.check_non_marketable(side="BUY", price="0.5", book=None)["reason"] == submit.NM_NO_BOOK
    assert submit.check_non_marketable(side="BUY", price=None, book=book)["reason"] == submit.NM_INVALID
    assert submit.check_non_marketable(side="BUY", price="0", book=book)["reason"] == submit.NM_INVALID
    assert submit.check_non_marketable(side="BUY", price="NaN", book=book)["reason"] == submit.NM_INVALID
    assert submit.check_non_marketable(side="HOLD", price="0.5", book=book)["reason"] == submit.NM_INVALID
    assert submit.check_non_marketable(side="BUY", price="0.5",
                                       book={"best_ask": "abc"})["reason"] == submit.NM_INVALID
    # F-B: a non-finite/負 reference price is a fail-closed deny, never an exception
    for bad_reference in ("NaN", "Infinity", "-Infinity", "-1", "0"):
        try:
            got = submit.check_non_marketable(side="BUY", price="0.5", book={"best_ask": bad_reference})
        except Exception as exc:  # noqa: BLE001 - raising is the bug being fixed
            raise AssertionError(f"best_ask={bad_reference!r} raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False and got["reason"] == submit.NM_INVALID, (bad_reference, got)
        got = submit.check_non_marketable(side="SELL", price="0.5", book={"best_bid": bad_reference})
        assert got["ok"] is False and got["reason"] == submit.NM_INVALID, (bad_reference, got)


def test_submit_limits():
    base = dict(notional_usdc="5", fire_budget_usdc="10", committed_usdc="100", max_capital_usdc="500")
    assert submit.check_limits(**base)["ok"] is True
    assert submit.check_limits(**{**base, "notional_usdc": "10"})["ok"] is True
    assert submit.check_limits(**{**base, "notional_usdc": "10.01"})["reason"] == submit.LIMIT_FIRE_BUDGET
    assert submit.check_limits(**{**base, "committed_usdc": "496"})["reason"] == submit.LIMIT_CAPITAL_CAP
    assert submit.check_limits(**{**base, "committed_usdc": "495"})["ok"] is True
    for broken in ({"notional_usdc": None}, {"fire_budget_usdc": None}, {"committed_usdc": None},
                   {"max_capital_usdc": None}, {"notional_usdc": "abc"}, {"notional_usdc": "-1"},
                   {"max_capital_usdc": "NaN"}):
        got = submit.check_limits(**{**base, **broken})
        assert got["ok"] is False and got["reason"] == submit.LIMIT_INVALID, (broken, got)


# --------------------------------------------------------------------------- Phase 3: sentinels

def test_submit_order_audits_itself():
    """F-D: the channel module — not its caller — owns the submit/cancel audit records."""
    client = _SmokeClient()
    good = submit.gate_status(enable_submit=True, env={"LIVE_SUBMIT_ENABLED": "1"},
                              confirm=submit.phrase())
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        with _stub_clob_types(), _audit_path(log):
            result = submit.submit_order(client, token_id="TOK-1", price="0.50", size="10",
                                         side="BUY", tick="0.01", neg_risk=True, gates=good)
            lines = _audit_lines(log)
            # v2 audits intent+submit, and takes the unfilled remainder down (engine default)
            assert [line["action"] for line in lines][:2] == ["intent", "submit"], lines
            assert result["ok"] is True and result["order_id"] == "ORD-1"
            assert result["audited"] is True, result
            assert lines[0]["reason"] == "execute_leg" and lines[0]["params"]["token_id"] == "TOK-1"
            assert lines[1]["order_id"] == "ORD-1" and lines[1]["response_summary"]["orderID"] == "ORD-1"
            assert [call[0] for call in client.calls][:2] == ["create_order", "post_order"], client.calls
            cancelled = submit.cancel_order(client, "ORD-1")
        assert cancelled["ok"] is True and cancelled["audited"] is True
        after = [line["action"] for line in _audit_lines(log)]
        assert after[:2] == ["intent", "submit"] and after[-1] == "cancel", after

        # the pre-action audit is mandatory for the dangerous direction: no log ⇒ no order
        before = list(client.calls)
        with contextlib.redirect_stderr(io.StringIO()):   # the module shouts on stderr by design
            with _audit_path(Path(tmp)):                  # a directory is not writable ⇒ AuditError
                try:
                    submit.submit_order(client, token_id="TOK-1", price="0.50", size="10", gates=good)
                except submit.AuditError as exc:
                    assert "cannot append" in str(exc), exc
                else:
                    raise AssertionError("an unwritable audit log must block submit_order")
            assert client.calls == before, f"nothing signed/sent without an audit trail: {client.calls}"

            # cancelling is the recovery direction: a broken log must NOT block it
            with _audit_path(Path(tmp)):
                rescue = submit.cancel_order(client, "ORD-2")
        assert rescue["ok"] is True and rescue["audited"] is False, rescue

        # and a gates-less refusal is audited too
        with _audit_path(log):
            try:
                submit.submit_order(client, token_id="T", price="0.5", size="1", gates=None)
            except PermissionError:
                pass
        assert [line["reason"] for line in _audit_lines(log)][-1] == "gates_missing"


def test_submit_sentinels_least_privilege():
    client_names, _ = _v2_surface_names()
    client = _surface(client_names)
    client.rfq = _surface(v2_transport.RFQ_SUBMIT_METHODS)
    originals = {name: getattr(client, name) for name in submit.RELEASE_WRITE_METHODS}

    armed = submit.arm_controlled_sentinels(client)
    assert armed["armed"] is True
    assert armed["released"] == [f"client.{name}" for name in submit.RELEASE_WRITE_METHODS], armed["released"]
    assert armed["read_only_available"] == list(submit.RELEASE_READ_METHODS)
    assert set(submit.RELEASE_WRITE_METHODS) == set(v2_transport.RELEASE_WRITE_METHODS), \
        submit.RELEASE_WRITE_METHODS
    assert set(submit.ALLOWED_RELEASE) == set(v2_transport.RELEASE_WRITE_METHODS
                                              + v2_transport.RELEASE_READ_METHODS), submit.ALLOWED_RELEASE
    assert "cancel_orders" in submit.RELEASE_WRITE_METHODS and "cancel" not in submit.RELEASE_WRITE_METHODS
    # the released writes are restored, NOT invoked (invoking post_order would be a real order)
    for name in submit.RELEASE_WRITE_METHODS:
        assert getattr(client, name) is originals[name], f"{name} was not restored"
    released_proof = [p for p in armed["proof"] if p.get("status") == "released"]
    assert {p["method"] for p in released_proof} == set(submit.RELEASE_WRITE_METHODS), released_proof
    assert all(p.get("status") == "released" for p in released_proof), released_proof
    blocked_proof = [p for p in armed["proof"] if p.get("status") == "blocked"]
    assert blocked_proof and all("SUBMIT BLOCKED" in p.get("error", "") for p in blocked_proof)

    # everything else must still be sentinel-blocked, proven by invoking it
    must_stay = (set(sign_dryrun.SUBMIT_METHODS) - set(submit.RELEASE_WRITE_METHODS)) \
        | set(sign_dryrun.ADMIN_METHODS) | set(sign_dryrun.STATE_WRITE_METHODS)
    for name in sorted(must_stay):
        assert f"client.{name}" in armed["still_blocked"], (name, armed["still_blocked"])
        method = getattr(client, name)
        assert getattr(method, "dryrun_sentinel_for", None) == name, name
        try:
            method()
        except RuntimeError as exc:
            assert "SUBMIT BLOCKED" in str(exc), exc
        else:
            raise AssertionError(f"{name}() was not blocked — least privilege violated")
    for name in sign_dryrun.RFQ_SUBMIT_METHODS:
        assert f"client.rfq.{name}" in armed["still_blocked"], name
        try:
            getattr(client.rfq, name)()
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"client.rfq.{name}() was not blocked")

    # fail closed when a released method is missing
    class _NoPostOrder:
        rfq = None
    try:
        submit.arm_controlled_sentinels(_NoPostOrder())
    except RuntimeError as exc:
        assert "post_order" in str(exc), exc
    else:
        raise AssertionError("arming without post_order must fail closed")


# --------------------------------------------------------------------------- Phase 3: audit log

def test_submit_audit_log():
    required = {"ts_utc", "action", "reason", "params", "response_summary", "order_id"}
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "nested" / "live_events.jsonl"
        with _audit_path(log):
            record = submit.audit({"action": "intent", "reason": "unit_test", "order_id": "ORD-1",
                                   "params": {"token_id": "123", "price": "0.5", "api_key": "SECRET-VALUE",
                                              "api_secret": "SECRET-VALUE"},
                                   "response_summary": {"status": "live"}})
            submit.audit({"action": "gate_deny", "reason": submit.GATE_FLAG, "params": {"intent": "smoke"}})
            lines = _audit_lines(log)
        assert len(lines) == 2, lines
        assert required <= set(lines[0]), sorted(required - set(lines[0]))
        assert record["params"]["api_key"] == "<redacted>" and record["params"]["api_secret"] == "<redacted>"
        assert "SECRET-VALUE" not in log.read_text(encoding="utf-8"), "no credential may reach the log"
        assert record["params"]["token_id"] == "123" and record["order_id"] == "ORD-1"
        assert lines[1]["action"] == "gate_deny", "refusals must be recorded too"
        assert lines[0]["ts_utc"].endswith("Z") and lines[0]["phase"] == "phase3"
        # unwritable log ⇒ refuse to act (acting without an audit trail is forbidden)
        with _audit_path(Path(tmp)):
            try:
                submit.audit({"action": "intent"})
            except submit.AuditError:
                pass
            else:
                raise AssertionError("an unwritable audit log must raise AuditError")


# --------------------------------------------------------------------------- Phase 3: smoke plan

def test_smoke_plan_branches():
    caps = {"no_max_ask": Decimal("1.0"), "yes_max_ask": Decimal("0.9")}
    built = smoke.build_smoke_plan(book=dict(smoke.DRY_BOOK), budget_usdc="5", token_id="T", caps=caps)
    assert built["ok"] and built["plan"]["price"] == Decimal("0.50"), built
    assert built["plan"]["size"] == Decimal("10.00"), built["plan"]
    assert built["plan"]["max_cost_usdc"] <= Decimal("5")
    assert built["plan"]["side"] == "BUY"

    # price rule: min(best_bid, best_ask - 2 ticks) and strictly below the ask
    wide = {"best_ask": "0.60", "best_bid": "0.58", "tick_size": "0.01", "min_order_size": "5"}
    assert smoke.smoke_price(wide)["price"] == Decimal("0.58")
    thin = {"best_ask": "0.60", "best_bid": "0.591", "tick_size": "0.01", "min_order_size": "5"}
    assert smoke.smoke_price(thin)["price"] == Decimal("0.58"), smoke.smoke_price(thin)

    denies = [
        ("no_book", {"best_ask": "0.52", "tick_size": "0.01", "min_order_size": "5"}),
        ("no_book", {"best_bid": "0.50", "tick_size": "0.01", "min_order_size": "5"}),
        ("no_book", {"best_bid": None, "best_ask": "0.52", "tick_size": "0.01"}),
        ("invalid_book", {"best_bid": "0.60", "best_ask": "0.52", "tick_size": "0.01"}),
        ("price_below_tick", {"best_bid": "0.009", "best_ask": "0.02", "tick_size": "0.01"}),
        ("invalid_input", {"best_bid": "0.5", "best_ask": "0.52", "tick_size": "0"}),
    ]
    for reason, book in denies:
        got = smoke.smoke_price(book)
        assert got["ok"] is False and got["reason"] == reason, (book, got)
        assert smoke.build_smoke_plan(book=book, budget_usdc="5", token_id="T",
                                      caps=caps)["ok"] is False

    # a budget below the market minimum must not plan
    small = smoke.build_smoke_plan(book=dict(smoke.DRY_BOOK), budget_usdc="1", token_id="T", caps=caps)
    assert small["reason"] == order_plan.BELOW_MIN_ORDER_SIZE, small

    # operator override: aligned to tick, then still subject to passivity + cap
    override = smoke.build_smoke_plan(book=dict(smoke.DRY_BOOK), budget_usdc="5", token_id="T",
                                      caps=caps, price_override="0.49")
    assert override["ok"] and override["plan"]["price"] == Decimal("0.49"), override
    assert submit.check_non_marketable(side="BUY", price=override["plan"]["price"],
                                       book=smoke.DRY_BOOK)["ok"] is True
    marketable = smoke.build_smoke_plan(book=dict(smoke.DRY_BOOK), budget_usdc="5", token_id="T",
                                        caps=caps, price_override="0.80")
    assert marketable["ok"] is True, marketable          # order_plan is fine (<= cap)...
    assert submit.check_non_marketable(side="BUY", price=marketable["plan"]["price"],
                                       book=smoke.DRY_BOOK)["reason"] == submit.NM_VIOLATION  # ...step ④ is not
    assert smoke.build_smoke_plan(book=dict(smoke.DRY_BOOK), budget_usdc="5", token_id="T",
                                  caps=caps, price_override="0.95")["reason"] == order_plan.PRICE_ABOVE_CAP
    assert smoke.build_smoke_plan(book=dict(smoke.DRY_BOOK), budget_usdc="5", token_id="T",
                                  caps=caps, price_override="abc")["reason"] == "invalid_input"


# --------------------------------------------------------------------------- Phase 3: smoke flow

class _BookSummary:
    """Minimal v2 OrderBookSummary look-alike (attribute access, like the real client)."""

    def __init__(self, *, bid="0.50", ask="0.52", tick="0.01", min_size="5", neg_risk=False):
        self.tick_size = tick
        self.min_order_size = min_size
        self.neg_risk = neg_risk
        self.bids = [] if bid is None else [{"price": bid, "size": "100"}]
        self.asks = [] if ask is None else [{"price": ask, "size": "100"}]


class _SmokeClient:
    """Fake v2 client: records writes, serves reads. No network, no real order."""

    def __init__(self, *, status="live", size_matched="0", cancel_ok=True,
                 status_after_cancel="canceled", open_orders=(0,), open_order_id="ORD-1"):
        self.rfq = _FakeRfq()
        self.calls = []
        self._status = status
        self._size_matched = size_matched
        self._cancel_ok = cancel_ok
        self._status_after_cancel = status_after_cancel
        self._open_orders = list(open_orders)
        self._open_order_id = open_order_id
        self.cancelled = False
        self.list_calls = 0

    # reads
    def get_order_book(self, token_id):
        return _BookSummary(bid="0.50", ask="0.52")

    def get_order(self, order_id):
        if self.cancelled:
            return {"id": order_id, "status": self._status_after_cancel, "size_matched": "0",
                    "price": "0.5", "original_size": "10"}
        return {"id": order_id, "status": self._status, "size_matched": self._size_matched,
                "price": "0.5", "original_size": "10", "asset_id": "tok"}

    def get_open_orders(self):
        self.list_calls += 1
        count = self._open_orders.pop(0) if self._open_orders else 0
        return [{"id": (self._open_order_id if i == 0 else f"LEFTOVER-{i}"), "asset_id": "tok",
                 "side": "BUY", "price": "0.5", "original_size": "10", "size_matched": "0",
                 "status": "live"} for i in range(count)]

    def get_trades(self):
        return []

    def get_balance_allowance(self, params=None):
        return {"balance": "50000000", "allowances": {}}

    # released writes
    def create_order(self, args, options):
        self.calls.append(("create_order", args, options))
        return "SIGNED-ORDER"

    def post_order(self, signed, order_type="GTC", post_only=False):
        self.calls.append(("post_order", signed, order_type, post_only))
        return {"orderID": "ORD-1", "status": "live", "success": True}

    def cancel_orders(self, order_ids):
        order_id = (list(order_ids) or [""])[0]
        self.calls.append(("cancel_orders", order_id))
        self.cancelled = True
        if self._cancel_ok:
            return {"canceled": [order_id], "not_canceled": {}}
        return {"canceled": [], "not_canceled": {order_id: "already_matched"}}


for _name in [n for n in ALL_WRITE_METHOD_NAMES
              if n not in ("post_order", "cancel_orders")]:
    setattr(_SmokeClient, _name, _MARKER(_name))


@contextlib.contextmanager
def _stub_clob_types():
    """Minimal ``py_clob_client_v2`` so the v2 write channel runs under a stdlib interpreter."""
    names = ("py_clob_client_v2", "py_clob_client_v2.clob_types", "py_clob_client_v2.client")
    saved = {name: sys.modules.get(name) for name in names}
    pkg = types.ModuleType("py_clob_client_v2")
    pkg.__path__ = []
    mod = types.ModuleType("py_clob_client_v2.clob_types")

    class OrderArgs:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PartialCreateOrderOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class OrderType:
        GTC = "GTC"

    class ApiCreds:
        def __init__(self, *args):
            self.args = args

    class AssetType:
        COLLATERAL = "COLLATERAL"

    class BalanceAllowanceParams:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    mod.OrderArgs = OrderArgs
    mod.PartialCreateOrderOptions = PartialCreateOrderOptions
    mod.OrderType = OrderType
    mod.ApiCreds = ApiCreds
    mod.AssetType = AssetType
    mod.BalanceAllowanceParams = BalanceAllowanceParams
    client_mod = types.ModuleType("py_clob_client_v2.client")
    client_mod.ClobClient = _FakeClient
    sys.modules["py_clob_client_v2"] = pkg
    sys.modules["py_clob_client_v2.clob_types"] = mod
    sys.modules["py_clob_client_v2.client"] = client_mod
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _snapshot(*, ok=True, balance="50", orders=0, positions=None, value="0"):
    positions = positions if positions is not None else []
    return {"ok": ok, "reason": None if ok else "forced_failure", "usdc_balance": balance,
            "open_orders": orders, "positions": positions, "positions_raw_count": len(positions),
            "positions_value_usdc": value}


def _patch_smoke(client, *, market=None, book=None, resolve=None):
    """Point the smoke flow at fakes; returns a restore callable."""
    saved = {
        "build_client": sign_dryrun._build_client,
        "select": smoke.select_tradeable_bucket,
        "audit": submit.AUDIT_PATH,
    }
    sign_dryrun._build_client = lambda creds_, **kw: client
    smoke.select_tradeable_bucket = lambda *a, **kw: {
        "ok": True, "reason": smoke.BUCKET_OK,
        "market": market or {"city": "london", "local_date": "2026-09-11", "direction": "high",
                             "bucket": "23C", "title": "t", "token_id": "TOK-1"},
        "book": book or {"best_ask": "0.52", "best_bid": "0.50", "tick_size": "0.01",
                         "min_order_size": "5", "neg_risk": True},
        "attempts": [], "rejected": [],
        "selection": {"rule": "test", "assessment": {"ok": True, "passive_price": "0.50",
                                                     "best_bid": "0.50", "best_ask": "0.52"}},
    }
    if resolve is not None:
        submit.check_non_marketable = resolve

    def restore():
        sign_dryrun._build_client = saved["build_client"]
        smoke.select_tradeable_bucket = saved["select"]
        submit.AUDIT_PATH = saved["audit"]
        if resolve is not None:
            submit.check_non_marketable = _ORIGINAL_NON_MARKETABLE

    return restore


_ORIGINAL_NON_MARKETABLE = submit.check_non_marketable


def test_submit_order_requires_gates():
    """The dangerous direction must be unreachable without a passing gate record."""
    client = _SmokeClient()
    good = submit.gate_status(enable_submit=True, env={"LIVE_SUBMIT_ENABLED": "1"},
                              confirm=submit.phrase())
    assert good["ok"] is True
    with _stub_clob_types():
        bad_gates = [None, {}, {"ok": False}, {"ok": True, "checks": {"cli_flag": True}},
                     {"ok": True, "checks": {"cli_flag": True, "env_flag": True,
                                             "confirm_phrase": False}}]
        for gates in bad_gates:
            try:
                submit.submit_order(client, token_id="T", price="0.5", size="10", gates=gates)
            except PermissionError as exc:
                assert "gates must pass" in str(exc), exc
            else:
                raise AssertionError(f"gates={gates!r} must be refused")
        kinds = [call[0] for call in client.calls]
        assert kinds == [], f"nothing may be signed or sent when gates fail: {kinds}"
        # cancelling stays available without gates — it is the recovery direction
        assert submit.cancel_order(client, "ORD-X")["ok"] is True


class _OpenOrderClient:
    """Fake with a controllable open-order list (for rescue-scope and price-match tests)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.rfq = None

    def get_open_orders(self):
        self.calls.append("get_open_orders")
        return list(self.rows)

    def cancel_orders(self, order_ids):
        order_id = (list(order_ids) or [""])[0]
        self.calls.append(("cancel_orders", order_id))
        return {"canceled": [order_id], "not_canceled": {}}


def test_rescue_cancel_scope_and_price_match():
    """F-A: never blind-cancel; F-C: match prices numerically, not as strings."""
    # ① no order id and no token ⇒ refuse before any scan
    blind = _OpenOrderClient([{"id": "X", "asset_id": "TOK-1", "price": "0.5"}])
    with tempfile.TemporaryDirectory() as tmp, _audit_path(Path(tmp) / "log.jsonl"):
        out = smoke.rescue_cancel(blind, order_id=None, token_id=None, price=None)
    assert out["ok"] is False and out["reason"].startswith("no_scope"), out
    assert blind.calls == [], f"a blind scan must not even list orders: {blind.calls}"

    # ② scoped by token: scans, matches numerically ("0.50" row vs "0.5" query), skips others
    client = _OpenOrderClient([
        {"id": "MATCH", "asset_id": "TOK-1", "price": "0.50"},
        {"id": "WRONG-PRICE", "asset_id": "TOK-1", "price": "0.51"},
        {"id": "OTHER-TOKEN", "asset_id": "TOK-2", "price": "0.50"},
        {"id": "GARBAGE-PRICE", "asset_id": "TOK-1", "price": "n/a"},
    ])
    with tempfile.TemporaryDirectory() as tmp, _audit_path(Path(tmp) / "log.jsonl"):
        out = smoke.rescue_cancel(client, order_id=None, token_id="TOK-1", price="0.5")
    assert out["ok"] is True and out["canceled"] == ["MATCH"], out
    assert out["matched_open_orders"] == ["MATCH"], out
    assert [call for call in client.calls if isinstance(call, tuple)] == [("cancel_orders", "MATCH")], client.calls

    # ③ no price ⇒ any order on that token (still token-scoped, never account-wide)
    client2 = _OpenOrderClient([{"id": "A", "asset_id": "TOK-1", "price": "0.50"},
                                {"id": "B", "asset_id": "TOK-2", "price": "0.50"}])
    with tempfile.TemporaryDirectory() as tmp, _audit_path(Path(tmp) / "log.jsonl"):
        out2 = smoke.rescue_cancel(client2, order_id=None, token_id="TOK-1")
    assert out2["canceled"] == ["A"], out2

    # ④ by order id: no scan needed
    client3 = _OpenOrderClient([{"id": "Z", "asset_id": "TOK-9", "price": "0.99"}])
    with tempfile.TemporaryDirectory() as tmp, _audit_path(Path(tmp) / "log.jsonl"):
        out3 = smoke.rescue_cancel(client3, order_id="Z")
    assert out3["ok"] is True and out3["strategy"] == "by_order_id", out3
    assert "get_open_orders" not in client3.calls, client3.calls


# ---------------------------------------------------------------- Phase 3b: port + v2 transport

def test_shared_gate_validation_is_strict():
    """F1: a truthy ``ok`` is not enough — every individual gate check must have passed."""
    assert submit.GATE_CHECKS == ("cli_flag", "env_flag", "confirm_phrase"), submit.GATE_CHECKS
    strict_cases = [
        ({"ok": True}, False),
        ({"ok": True, "checks": {}}, False),
        ({"ok": True, "checks": {"cli_flag": True}}, False),
        ({"ok": True, "checks": {"cli_flag": True, "env_flag": True}}, False),
        ({"ok": True, "checks": {"cli_flag": True, "env_flag": True, "confirm_phrase": False}}, False),
        ({"ok": False, "checks": {"cli_flag": True, "env_flag": True, "confirm_phrase": True}}, False),
        ({}, False), (None, False), ("ok", False),
        ({"ok": True, "checks": {"cli_flag": True, "env_flag": True, "confirm_phrase": True}}, True),
    ]
    for gates, expected in strict_cases:
        assert submit.gates_all_passed(gates) is expected, gates
    # the v1 channel really consumes the shared check (forged record ⇒ refused, even with a
    # gate-flag-only kwarg path)
    import inspect
    source = inspect.getsource(submit.submit_order)
    assert "gates_all_passed" in source, "submit_order must use the shared gate check"
    assert "gates_all_passed" in inspect.getsource(v2_transport.execute_leg), \
        "execute_leg must use the shared gate check"


def test_port_reuses_v1_safety_machinery():
    """The v2 channel must reuse — not fork — the audited v1 gate/audit/check machinery."""
    assert v2_transport.submit is submit, "v2 transport must call the same submit module"
    assert v2_transport.submit.gate_status is submit.gate_status
    assert v2_transport.submit.audit is submit.audit
    assert v2_transport.submit.check_non_marketable is submit.check_non_marketable
    assert v2_transport.submit.check_limits is submit.check_limits
    assert v2_transport.submit.gates_all_passed is submit.gates_all_passed
    assert port.submit is submit and port.risk_gate is risk_gate
    # the v2 sentinel list is the v2 method surface, released with the same least privilege
    assert set(v2_transport.RELEASE_WRITE_METHODS) == {"post_order", "cancel_orders"}
    assert "cancel_orders" in v2_transport.SUBMIT_METHODS
    assert "post_order" in v2_transport.SUBMIT_METHODS
    for name in ("create_and_post_market_order", "create_or_derive_api_key", "revoke_builder_api_key"):
        assert name in v2_transport.SUBMIT_METHODS + v2_transport.ADMIN_METHODS, name
    assert v2_transport.QTY > 0


def test_port_live_limits_read_live_env():
    env = dict(VALID_ENV, LIVE_FIRE_BUDGET_USDC="12", LIVE_MAX_OPEN_POSITIONS="10",
               LIVE_MAX_CAPITAL_USDC="50")
    limits = port.live_limits(env)
    assert limits == {"fire_budget_usdc": "12", "max_open_positions": "10",
                      "max_capital_usdc": "50"}, limits
    status = port.port_status({"mode": "paper"}, env=env)
    assert status["ok"] is True and status["mode"] == "paper"
    live_status = port.port_status({"mode": "live"}, env=env)
    assert live_status["ok"] is False and live_status["limits"] == limits, live_status
    assert not any(v is None for v in live_status["limits"].values())


# --------------------------------------------------------------------------- Phase 3: bucket selection

class _FakeSummary:
    """Stands in for OrderBookSummary (the shape sign_dryrun._book_from_summary reads)."""

    def __init__(self, *, bids=(), asks=(), tick="0.01", min_size="5", neg_risk=True):
        self.bids = [{"price": p, "size": z} for p, z in bids]
        self.asks = [{"price": p, "size": z} for p, z in asks]
        self.tick_size = tick
        self.min_order_size = min_size
        self.neg_risk = neg_risk


def _book(bid=None, ask=None, *, tick="0.01", min_size="5", bid_size="100", ask_size="100"):
    return {"best_bid": bid, "best_ask": ask, "tick_size": tick, "min_order_size": min_size,
            "neg_risk": True,
            "bids": [] if bid is None else [{"price": bid, "size": bid_size}],
            "asks": [] if ask is None else [{"price": ask, "size": ask_size}]}


def test_assess_book_tradability():
    """A bucket qualifies only when it is two-sided, near-price and size-sufficient."""
    cap = Decimal("0.9")
    good = smoke.assess_book(_book("0.50", "0.52"), cap=cap)
    assert good["ok"] and good["reason"] == smoke.BUCKET_OK, good
    assert good["passive_price"] == "0.50" and good["ask_distance"] == "0.02", good

    cases = [
        (smoke.NO_BID, _book(None, "0.52")),                       # dead bucket: no bid
        (smoke.NO_BID, _book("0", "0.52")),
        (smoke.NO_ASK, _book("0.50", None)),                       # no resting ask
        (smoke.NO_PASSIVE_PRICE, _book("0.001", "0.002")),         # ask 0.001 ⇒ no room to rest
        (smoke.ASK_AT_EXTREME, _book("0.97", "0.999")),            # ask within 2 ticks of 1
        (smoke.ASK_AT_EXTREME, _book("0.50", "1")),
        (smoke.INVALID_BOOK, _book("0.60", "0.52")),               # crossed
        (smoke.ASK_SIZE_BELOW_MIN, _book("0.50", "0.52", ask_size="1")),
        (smoke.ASK_ABOVE_CAP, _book("0.94", "0.95")),              # over the config cap
        (smoke.NO_PASSIVE_PRICE, _book("0.01", "0.02", tick="0.01")),   # min(bid, ask-2t) < tick
        (smoke.INVALID_BOOK, {"best_bid": "0.50", "best_ask": "0.52", "tick_size": "0.01"}),  # min size missing
        (smoke.INVALID_BOOK, None),
    ]
    for reason, book in cases:
        got = smoke.assess_book(book, cap=cap)
        assert got["ok"] is False and got["reason"] == reason, (book, got)
        assert got["detail"], got

    # near-ask depth may be spread over several levels (within 2 ticks)
    spread = {"best_bid": "0.50", "best_ask": "0.52", "tick_size": "0.01", "min_order_size": "5",
              "bids": [{"price": "0.50", "size": "100"}],
              "asks": [{"price": "0.52", "size": "2"}, {"price": "0.53", "size": "4"}]}
    assert smoke.assess_book(spread, cap=cap)["ok"] is True, smoke.assess_book(spread, cap=cap)
    far = {**spread, "asks": [{"price": "0.52", "size": "2"}, {"price": "0.90", "size": "400"}]}
    assert smoke.assess_book(far, cap=cap)["reason"] == smoke.ASK_SIZE_BELOW_MIN


def test_choose_bucket_prefers_near_mid():
    """Among qualifying buckets: |best_ask - 0.5| wins, ties break on volume."""
    def candidate(bid, ask, volume):
        return {"volume": volume, "assessment": smoke.assess_book(_book(bid, ask), cap=Decimal("0.9")),
                "book": _book(bid, ask), "market": {"bucket": f"{bid}/{ask}"}}

    mixed = [candidate("0.05", "0.06", 9000),      # far from mid
             candidate("0.20", "0.21", 100),
             candidate("0.48", "0.49", 10),        # closest to mid
             candidate("0.50", "0.52", 5000)]
    assert smoke.choose_bucket(mixed)["market"]["bucket"] == "0.48/0.49", smoke.choose_bucket(mixed)
    tie = [candidate("0.48", "0.49", 10), candidate("0.50", "0.51", 5000)]   # both distance 0.01
    assert smoke.choose_bucket(tie)["market"]["bucket"] == "0.50/0.51", smoke.choose_bucket(tie)
    assert smoke.choose_bucket([candidate("0.001", "0.001", 1)]) is None


def _market(bucket, token, volume, bid=None, ask=None):
    return {"groupItemTitle": bucket, "clobTokenIds": json.dumps([token, f"{token}-no"]),
            "volumeNum": volume, "acceptingOrders": True, "bestBid": bid, "bestAsk": ask,
            "question": f"{bucket}?"}


class _BucketClient:
    """Fake transport for select_tradeable_bucket: Gamma via http_json, books per token."""

    def __init__(self, books):
        self.books = books
        self.requested = []

    def get_order_book(self, token_id):
        self.requested.append(token_id)
        summary = self.books.get(token_id)
        if summary is None:
            raise RuntimeError(f"no book for {token_id}")
        return summary


def _fake_state(tmp, sessions):
    """A minimal paper-state file so candidate_sessions() never reads the real data/ state."""
    path = Path(tmp) / "state.json"
    path.write_text(json.dumps({"weatherbotyes2re": {"armed": {key: {} for key in sessions}}}),
                    encoding="utf-8")
    return path


@contextlib.contextmanager
def _fake_gamma(markets):
    saved = clob_client.http_json
    clob_client.http_json = lambda url, **kw: {"slug": "fake", "markets": markets}
    try:
        yield
    finally:
        clob_client.http_json = saved


def test_select_tradeable_bucket_only_dead_buckets():
    """4(a): dead buckets only ⇒ refuse with no_tradeable_bucket + the rejected list."""
    markets = [_market("19C", "T-DEAD", 12000, bid=None, ask="0.001"),
               _market("20C", "T-DEAD2", 9000, bid="0", ask="0.002")]
    client = _BucketClient({"T-DEAD": _FakeSummary(bids=[], asks=[("0.001", "1000")]),
                            "T-DEAD2": _FakeSummary(bids=[], asks=[("0.002", "1000")])})
    with tempfile.TemporaryDirectory() as tmp, _fake_gamma(markets):
        out = smoke.select_tradeable_bucket(client, city="amsterdam", local_date="2026-09-10",
                                            direction="high", cap=Decimal("0.9"),
                                            state_path=_fake_state(tmp, ["amsterdam|2026-09-10|high"]))
    assert out["ok"] is False and out["reason"] == smoke.NO_TRADEABLE_BUCKET, out
    assert len(out["rejected"]) == 2 and len(out["attempts"]) == 2, out["attempts"]
    assert {row["status"] for row in out["rejected"]} == {smoke.NO_BID}, out["rejected"]
    assert all(row["best_bid"] is None and row["bucket"] for row in out["rejected"]), out["rejected"]


def test_select_tradeable_bucket_mixed_books():
    """4(b): with a live bucket present, pick the one whose best_ask is closest to 0.5."""
    markets = [_market("30C", "T-FAR", 30000, bid="0.05", ask="0.06"),
               _market("28C", "T-MID", 100, bid="0.48", ask="0.49"),
               _market("29C", "T-DEAD", 20000, bid=None, ask="0.001"),
               _market("27C", "T-OTHER", 5000, bid="0.50", ask="0.52")]
    client = _BucketClient({
        "T-FAR": _FakeSummary(bids=[("0.05", "500")], asks=[("0.06", "500")]),
        "T-MID": _FakeSummary(bids=[("0.48", "50")], asks=[("0.49", "50")]),
        "T-DEAD": _FakeSummary(bids=[], asks=[("0.001", "900")]),
        "T-OTHER": _FakeSummary(bids=[("0.50", "300")], asks=[("0.52", "300")]),
    })
    with tempfile.TemporaryDirectory() as tmp, _fake_gamma(markets):
        out = smoke.select_tradeable_bucket(client, city="london", local_date="2026-09-11",
                                            direction="high", cap=Decimal("0.9"),
                                            state_path=_fake_state(tmp, ["london|2026-09-11|high"]))
    assert out["ok"] is True, out
    assert out["market"]["token_id"] == "T-MID", out["market"]
    assert out["selection"]["assessment"]["best_ask"] == "0.49", out["selection"]
    # passive price = min(bid 0.48, ask 0.49 - 2 ticks 0.47) = 0.47
    assert out["selection"]["assessment"]["passive_price"] == "0.47", out["selection"]
    statuses = {row["bucket"]: row["status"] for row in out["attempts"]}
    assert statuses["29C"] == smoke.NO_BID and statuses["30C"] == smoke.BUCKET_OK, statuses
    # dead bucket is recorded as rejected, never silently skipped
    assert [row["bucket"] for row in out["rejected"]] == ["29C"], out["rejected"]

    # a pinned --token-id is assessed, not chosen
    with _fake_gamma(markets):
        pinned = smoke.select_tradeable_bucket(client, token_id="T-FAR", cap=Decimal("0.9"))
        pinned_dead = smoke.select_tradeable_bucket(client, token_id="T-DEAD", cap=Decimal("0.9"))
    assert pinned["ok"] is True and pinned["market"]["token_id"] == "T-FAR", pinned
    assert pinned_dead["ok"] is False and pinned_dead["reason"] == smoke.NO_TRADEABLE_BUCKET, pinned_dead


def test_smoke_denies_when_no_tradeable_bucket():
    """The flow must stop (exit 2, audited) instead of planning against a dead bucket."""
    client = _SmokeClient()
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        with _stub_clob_types(), _audit_path(log):
            restore = _patch_smoke(client)
            smoke.select_tradeable_bucket = lambda *a, **kw: {
                "ok": False, "reason": smoke.NO_TRADEABLE_BUCKET,
                "detail": "no tradable bucket among 3 candidate(s)",
                "attempts": [{"key": "amsterdam|2026-09-10|high", "bucket": "19C",
                              "token_id": "T", "status": smoke.NO_BID, "best_bid": None,
                              "best_ask": "0.001"}],
                "rejected": [{"key": "amsterdam|2026-09-10|high", "bucket": "19C", "token_id": "T",
                              "status": smoke.NO_BID, "best_bid": None, "best_ask": "0.001",
                              "detail": "one-sided/dead bucket"}],
                "selection": None,
            }
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                         env=_flow_env(), sleep=lambda _s: None, attempts=1,
                                         reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
        assert report["ok"] is False and report["exit_code"] == 2, report["reason"]
        assert report["reason"] == "bucket:no_tradeable_bucket", report["reason"]
        assert report["plan"] is None and report["order"]["submitted"] is False
        assert [call[0] for call in client.calls] == [], client.calls
        assert len(report["bucket_rejected"]) == 1 and report["bucket_attempts"], report
        lines = _audit_lines(log)
        deny = [line for line in lines if line["action"] == "deny"][-1]
        assert deny["reason"] == "bucket:no_tradeable_bucket", deny
        assert deny["response_summary"]["rejected"][0]["status"] == smoke.NO_BID, deny
        assert "rejects: 1" in smoke.human_summary(report), smoke.human_summary(report)


def test_smoke_never_blind_cancels_on_failure():
    """F-A ②/③: a failure with no scope (and the read-only mode) must not touch the order book."""
    def _explode_discover(*_a, **_k):
        raise RuntimeError("bucket selection blew up before any plan existed")

    def _forbidden_list(*_a, **_k):
        raise AssertionError("rescue listed open orders without scope — blind cancel risk")

    saved = (smoke.select_tradeable_bucket, submit.list_open_orders)
    submit.list_open_orders = _forbidden_list
    try:
        with tempfile.TemporaryDirectory() as tmp, _stub_clob_types(), _audit_path(Path(tmp) / "l.jsonl"):
            client = _SmokeClient()
            restore = _patch_smoke(client)
            smoke.select_tradeable_bucket = _explode_discover   # after the patch helper
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                         env=_flow_env(), sleep=lambda _s: None, attempts=1,
                                         reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
            assert report["ok"] is False and report["reason"].startswith("RuntimeError"), report
            assert report["residual_risk"] is False, report
            assert [call[0] for call in client.calls] == [], client.calls
            actions = [(line["action"], line["reason"]) for line in _audit_lines(Path(tmp) / "l.jsonl")]
            assert ("rescue", "skipped_no_scope") in actions, actions

            # read-only mode: same failure, still no rescue attempt
            client2 = _SmokeClient()
            restore = _patch_smoke(client2)
            smoke.select_tradeable_bucket = _explode_discover
            try:
                report2 = smoke.run_smoke(budget_usdc="5", env=_flow_env(), readonly=True,
                                          sleep=lambda _s: None, attempts=1,
                                          reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
            assert report2["residual_risk"] is False and report2["exit_code"] == 2, report2
            assert [call[0] for call in client2.calls] == [], client2.calls
    finally:
        smoke.select_tradeable_bucket, submit.list_open_orders = saved


def test_smoke_tolerates_open_order_list_lag():
    """A confirmed cancel + a lagging list endpoint must NOT be reported as residual risk."""
    client = _SmokeClient(open_orders=(1, 1, 0), open_order_id="ORD-1")   # two stale reads, then clear
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        with _stub_clob_types(), _audit_path(log):
            restore = _patch_smoke(client)
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                         env=_flow_env(), sleep=lambda _s: None, attempts=2,
                                         open_orders_attempts=3, open_orders_sleep=0,
                                         reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
        actions = [(line["action"], line["reason"]) for line in _audit_lines(log)]
    check = report["open_orders_check"]
    assert report["ok"] is True and report["exit_code"] == 0, report["reason"]
    assert report["residual_risk"] is False, report
    assert check["status"] == "clear" and check["attempts"] == 3 and check["lag_observed"] is True, check
    assert check["note"] == "cancel_confirmed_but_list_lag", check
    assert "lag=cancel_confirmed_but_list_lag" in smoke.human_summary(report)
    assert ("open_orders", "clear") in actions, actions


def test_smoke_reports_residual_risk_when_list_persists():
    """Persistent evidence ⇒ real residual risk; a different order id too."""
    # (a) our own cancelled id never clears → residual risk, with an explicit reason
    persistent = _SmokeClient(open_orders=(1, 1, 1, 1), open_order_id="ORD-1")
    with tempfile.TemporaryDirectory() as tmp:
        with _stub_clob_types(), _audit_path(Path(tmp) / "l.jsonl"):
            restore = _patch_smoke(persistent)
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                         env=_flow_env(), sleep=lambda _s: None, attempts=2,
                                         open_orders_attempts=3, open_orders_sleep=0,
                                         reconcile_fn=lambda env, **kw: _snapshot(orders=1))
            finally:
                restore()
    assert report["ok"] is False and report["exit_code"] == 2
    assert report["residual_risk"] is True, report
    assert report["reason"] == "cancel_confirmed_but_list_still_shows_order", report["reason"]
    assert report["open_orders_check"]["remaining_ids"] == ["ORD-1"], report["open_orders_check"]
    assert "RESIDUAL RISK" in smoke.human_summary(report)
    assert persistent.list_calls == 3, persistent.list_calls          # bounded: 3 reads, no more

    # (b) a *different* order id ⇒ residual risk immediately (never attributed to list lag)
    foreign = _SmokeClient(open_orders=(1, 1, 1), open_order_id="SOMEONE-ELSE")
    with tempfile.TemporaryDirectory() as tmp:
        with _stub_clob_types(), _audit_path(Path(tmp) / "l.jsonl"):
            restore = _patch_smoke(foreign)
            try:
                report2 = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                          env=_flow_env(), sleep=lambda _s: None, attempts=2,
                                          open_orders_attempts=3, open_orders_sleep=0,
                                          reconcile_fn=lambda env, **kw: _snapshot(orders=1))
            finally:
                restore()
    assert report2["ok"] is False and report2["residual_risk"] is True
    assert report2["reason"] == "other_orders_remain", report2["reason"]
    assert report2["open_orders_check"]["attempts"] == 1, report2["open_orders_check"]
    assert foreign.list_calls == 1, foreign.list_calls


def test_await_no_open_orders_branches():
    """Unit-level: the helper's own branches (pure, one fake client)."""

    class _Client:
        def __init__(self, sequence, raises=False):
            self.sequence = list(sequence)
            self.raises = raises
            self.calls = 0

        def get_open_orders(self):
            self.calls += 1
            if self.raises:
                raise RuntimeError("list endpoint down")
            n = self.sequence.pop(0) if self.sequence else 0
            return [{"id": "ORD-1"}] * n

    with tempfile.TemporaryDirectory() as tmp, _audit_path(Path(tmp) / "l.jsonl"):
        clear = smoke.await_no_open_orders(_Client([0]), "ORD-1", cancel_confirmed=True,
                                           attempts=3, sleep_seconds=0, sleep=lambda _s: None)
        assert clear == {"ok": True, "status": "clear", "attempts": 1, "remaining_ids": [],
                         "lag_observed": False, "note": ""}, clear
        lag = smoke.await_no_open_orders(_Client([1, 1, 0]), "ORD-1", cancel_confirmed=True,
                                         attempts=3, sleep_seconds=0, sleep=lambda _s: None)
        assert lag["ok"] and lag["lag_observed"] and lag["note"] == "cancel_confirmed_but_list_lag", lag
        # a lag *without* a confirmed cancel stays a plain (non-lag) clear
        unconfirmed = smoke.await_no_open_orders(_Client([1, 0]), "ORD-1", cancel_confirmed=False,
                                                attempts=3, sleep_seconds=0, sleep=lambda _s: None)
        assert unconfirmed["ok"] and unconfirmed["note"] == "", unconfirmed
        stuck = smoke.await_no_open_orders(_Client([1, 1, 1]), "ORD-1", cancel_confirmed=True,
                                           attempts=3, sleep_seconds=0, sleep=lambda _s: None)
        assert not stuck["ok"] and stuck["status"] == "cancel_confirmed_but_list_still_shows_order", stuck
        stuck_other = smoke.await_no_open_orders(_Client([1, 1, 1]), "ORD-1", cancel_confirmed=False,
                                                 attempts=3, sleep_seconds=0, sleep=lambda _s: None)
        assert not stuck_other["ok"] and stuck_other["status"] == "open_orders_remain", stuck_other
        broken = smoke.await_no_open_orders(_Client([], raises=True), "ORD-1", cancel_confirmed=True,
                                            attempts=3, sleep_seconds=0, sleep=lambda _s: None)
        assert not broken["ok"] and broken["status"] == "list_failed", broken


def test_smoke_readonly_preflight_never_writes():
    """--readonly-preflight runs the live chain and stops before the write."""
    client = _SmokeClient()
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        with _stub_clob_types(), _audit_path(log):
            restore = _patch_smoke(client)
            try:
                report = smoke.run_smoke(budget_usdc="5", env=_flow_env(), readonly=True,
                                         sleep=lambda _s: None, attempts=2,
                                         reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
        kinds = [call[0] for call in client.calls]
        assert kinds == [], f"read-only preflight touched the write path: {kinds}"
        assert report["ok"] is True and report["exit_code"] == 0, report["reason"]
        assert report["reason"] == "readonly_stop_before_submit"
        assert report["readonly"] is True
        assert report["plan"]["ok"] is True and report["non_marketable"]["ok"] is True
        actions = [line["action"] for line in _audit_lines(log)]
        assert "preflight_ok" in actions and "submit" not in actions, actions
        # and it works with the gates unsatisfied (it cannot submit, so it needs none)
        assert report["gates"]["ok"] is False, report["gates"]


def test_smoke_refuses_without_gates():
    """A missing gate must stop the run before a client is even built."""
    def _explode(*_a, **_k):
        raise AssertionError("client must not be built when a gate is missing")

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        saved = sign_dryrun._build_client
        sign_dryrun._build_client = _explode
        try:
            with _audit_path(log):
                matrix = [
                    (dict(enable_submit=False, env=_flow_env(), confirm=submit.phrase()), 3, "submit_flag_missing"),
                    (dict(enable_submit=True, env=VALID_ENV, confirm=submit.phrase()), 3, "submit_env_missing"),
                    (dict(enable_submit=True, env=_flow_env(), confirm=None), 3, "confirm_phrase_missing"),
                    (dict(enable_submit=True, env=_flow_env(), confirm="SMOKE-1999-01-01"), 3, "confirm_phrase_mismatch"),
                ]
                for kwargs, code, reason in matrix:
                    report = smoke.run_smoke(**kwargs)
                    assert report["ok"] is False and report["exit_code"] == code, (kwargs, report)
                    assert report["reason"] == reason, (kwargs, report["reason"])
                    assert report["order"]["submitted"] is False
                assert smoke.main(["--dry-plan"]) == 0
        finally:
            sign_dryrun._build_client = saved
        actions = [line["action"] for line in _audit_lines(log)]
        assert actions.count("gate_deny") == 4, actions
        assert "plan" in actions, actions


def test_smoke_flow_with_fakes():
    """Full loop against a fake transport: plan → submit once → confirm → cancel → confirm → reconcile."""
    client = _SmokeClient(status="live", size_matched="0")
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        with _stub_clob_types(), _audit_path(log):
            restore = _patch_smoke(client)
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(),
                                         budget_usdc="5", env=_flow_env(),
                                         sleep=lambda _s: None, attempts=2,
                                         reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
        # client calls: exactly one signing + one post, then read/cancel only
        kinds = [call[0] for call in client.calls]
        assert kinds.count("post_order") == 1, kinds
        assert kinds.count("create_order") == 1, kinds
        assert kinds.count("cancel_orders") == 1, kinds        # smoke takes its own order down
        _, signed, order_type, post_only = client.calls[kinds.index("post_order")]
        assert signed == "SIGNED-ORDER" and post_only is True, client.calls
        _, args, _ = client.calls[kinds.index("create_order")]
        assert args.side == "BUY" and args.token_id == "TOK-1", vars(args)
        assert Decimal(str(args.price)) < Decimal("0.52"), vars(args)
        assert Decimal(str(args.size)) == Decimal("10.00"), vars(args)

        assert report["ok"] is True and report["exit_code"] == 0, report["reason"]
        assert report["order"]["order_id"] == "ORD-1"
        assert report["order"]["confirmed"]["status"] == "live"
        assert report["order"]["status_after_cancel"] == "canceled"
        assert report["residual_risk"] is False
        assert report["sentinel"]["released"] == ["client.post_order", "client.cancel_orders"], \
        report["sentinel"]["released"]
        assert report["risk_gate"]["allow"] is True
        assert report["limits_check"]["ok"] is True
        assert report["non_marketable"]["ok"] is True
        actions = [line["action"] for line in _audit_lines(log)]
        for expected in ("intent", "sentinel_armed", "discover", "plan", "risk", "limits",
                         "non_marketable", "submit", "query", "cancel", "reconcile", "complete"):
            assert expected in actions, (expected, actions)
        assert actions.index("risk") < actions.index("plan") < actions.index("submit"), actions
        assert actions.index("submit") < actions.index("cancel") < actions.index("reconcile"), actions


def test_smoke_flow_denies_before_submit():
    """Every pre-submit refusal must happen with zero write calls (nothing signed, nothing sent)."""
    cases = [
        ("risk_gate:max_open_positions_reached",
         dict(env=_flow_env(LIVE_MAX_CAPITAL_USDC="0", LIVE_MAX_OPEN_POSITIONS="0"),
              snapshot=lambda env, **kw: _snapshot())),
        ("risk_gate:budget_exceeds_balance",
         dict(env=_flow_env(), snapshot=lambda env, **kw: _snapshot(balance="1"))),
        ("preflight:reconcile:forced_failure",
         dict(env=_flow_env(), snapshot=lambda env, **kw: _snapshot(ok=False))),
        # step ③ is a second, independent check on the *signed* notional: request a bigger
        # plan than the configured per-fire budget (risk_gate looks at the config value)
        ("limits:notional_exceeds_fire_budget",
         dict(env=_flow_env(LIVE_FIRE_BUDGET_USDC="5"), budget_usdc="10",
              snapshot=lambda env, **kw: _snapshot())),
        # nb: check_limits' capital branch is unreachable through run_smoke (risk_gate uses the
        # config fire budget, which is >= the plan notional) — it is covered directly above.
        ("order_plan:price_above_cap",
         dict(env=_flow_env(), snapshot=lambda env, **kw: _snapshot(), price_override="0.95")),
        ("order_plan:below_min_order_size",
         dict(env=_flow_env(), snapshot=lambda env, **kw: _snapshot(), budget_usdc="1")),
        ("non_marketable:marketable_would_fill",
         dict(env=_flow_env(), snapshot=lambda env, **kw: _snapshot(), price_override="0.80")),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        for expected, case in cases:
            client = _SmokeClient()
            with _stub_clob_types(), _audit_path(log):
                restore = _patch_smoke(client)
                try:
                    report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(),
                                             budget_usdc=case.get("budget_usdc", "5"),
                                             env=case["env"], sleep=lambda _s: None, attempts=2,
                                             price_override=case.get("price_override"),
                                             reconcile_fn=case["snapshot"])
                finally:
                    restore()
            assert report["ok"] is False and report["exit_code"] == 2, (expected, report["reason"])
            assert report["reason"] == expected, (report["reason"], expected)
            kinds = [call[0] for call in client.calls]
            assert "post_order" not in kinds, f"submitted despite {expected}: {kinds}"
            assert "create_order" not in kinds, f"signed despite {expected}: {kinds}"
            assert report["order"]["submitted"] is False
        actions = [line["action"] for line in _audit_lines(log)]
        assert actions.count("deny") >= 4, actions
        assert "submit" not in actions, actions


def test_smoke_flow_reports_residual_risk():
    """A failed cancel must be reported as residual risk, never as success."""
    client = _SmokeClient(cancel_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        with _stub_clob_types(), _audit_path(log):
            restore = _patch_smoke(client)
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                         env=_flow_env(), sleep=lambda _s: None, attempts=2,
                                         reconcile_fn=lambda env, **kw: _snapshot(orders=1))
            finally:
                restore()
        assert report["ok"] is False and report["exit_code"] == 2
        assert report["reason"] in ("cancel_not_confirmed", "open_orders_remain"), report["reason"]
        assert report["residual_risk"] is True
        assert report["order"]["order_id"] == "ORD-1"
        assert "RESIDUAL RISK" in smoke.human_summary(report)

    # an unexpected fill is a result, not residual risk — and must be loud
    filled = _SmokeClient(status="matched", size_matched="3")
    with tempfile.TemporaryDirectory() as tmp:
        with _stub_clob_types(), _audit_path(Path(tmp) / "log.jsonl"):
            restore = _patch_smoke(filled)
            try:
                report = smoke.run_smoke(enable_submit=True, confirm=submit.phrase(), budget_usdc="5",
                                         env=_flow_env(), sleep=lambda _s: None, attempts=2,
                                         reconcile_fn=lambda env, **kw: _snapshot())
            finally:
                restore()
    assert report["ok"] is False and report["order"]["unexpected_fill"] == "3", report
    assert "UNEXPECTED FILL" in smoke.human_summary(report)
    assert [call[0] for call in filled.calls].count("cancel_orders") == 0, "a filled order is not cancelled"

CHECKS = [
    ("risk_gate: allow path", test_risk_gate_allow),
    ("risk_gate: all deny codes", test_risk_gate_deny_codes),
    ("risk_gate: fail-closed inputs", test_risk_gate_fail_closed),
    ("creds: valid set", test_creds_validate_ok),
    ("creds: optional/partial API trio", test_creds_optional_api_trio),
    ("creds: bad formats rejected", test_creds_rejects_bad_formats),
    ("creds: .env parsing + masking", test_creds_env_file_and_mask),
    ("reconcile: output structure", test_reconcile_structure),
    ("reconcile: gate uses real balance", test_reconcile_gate_denies_on_real_balance),
    ("reconcile: fail-closed + secret-free", test_reconcile_fail_closed),
    ("reconcile: non-mapping env never raises", test_reconcile_non_mapping_env),
    ("reconcile: non-string env values never raise", test_reconcile_non_string_values),
    ("reconcile: CLI json/exit codes", test_reconcile_cli_json_and_exit_codes),
    ("live/: no order/cancel path (static)", test_static_no_order_path),
    ("order_plan: tick alignment + rounding", test_order_plan_tick_alignment_and_rounding),
    ("order_plan: deny branches", test_order_plan_deny_branches),
    ("order_plan: sell path + config caps", test_order_plan_sell_and_caps),
    ("dry-run: sentinels are load-bearing", test_sentinels_are_load_bearing),
    ("dry-run: refuses when sentinels cannot arm", test_sentinel_refuses_when_methods_absent),
    ("dry-run: sentinel coverage over client surface", test_sentinel_coverage_over_client_surface),
    ("order_plan: load_caps fails closed", test_load_caps_fails_closed),
    ("dry-run: fails closed when caps unreadable", test_dryrun_fails_closed_when_caps_unreadable),
    ("submit: shared gate validation is strict (F1)", test_shared_gate_validation_is_strict),
    ("port: reuses v1 gate/audit/checks", test_port_reuses_v1_safety_machinery),
    ("port: live limits come from LIVE_* env", test_port_live_limits_read_live_env),
    ("submit: triple gate matrix", test_submit_gate_matrix),
    ("submit: non-marketable check", test_submit_non_marketable),
    ("submit: per-order + cumulative limits", test_submit_limits),
    ("submit: submit_order requires passing gates", test_submit_order_requires_gates),
    ("submit: sentinel least privilege", test_submit_sentinels_least_privilege),
    ("submit: audit log (incl. refusals)", test_submit_audit_log),
    ("submit: submit_order/cancel_order audit themselves", test_submit_order_audits_itself),
    ("smoke: plan construction branches", test_smoke_plan_branches),
    ("smoke: assess_book tradability", test_assess_book_tradability),
    ("smoke: choose_bucket prefers near-mid", test_choose_bucket_prefers_near_mid),
    ("smoke: selection rejects dead buckets only", test_select_tradeable_bucket_only_dead_buckets),
    ("smoke: selection picks near-mid live bucket", test_select_tradeable_bucket_mixed_books),
    ("smoke: denies when no tradeable bucket", test_smoke_denies_when_no_tradeable_bucket),
    ("smoke: tolerates open-order list lag", test_smoke_tolerates_open_order_list_lag),
    ("smoke: residual risk when the list persists", test_smoke_reports_residual_risk_when_list_persists),
    ("smoke: await_no_open_orders branches", test_await_no_open_orders_branches),
    ("smoke: rescue scope + numeric price match", test_rescue_cancel_scope_and_price_match),
    ("smoke: never blind-cancels on failure", test_smoke_never_blind_cancels_on_failure),
    ("smoke: readonly preflight never writes", test_smoke_readonly_preflight_never_writes),
    ("smoke: refuses without all three gates", test_smoke_refuses_without_gates),
    ("smoke: full flow (fake transport)", test_smoke_flow_with_fakes),
    ("smoke: pre-submit refusals write nothing", test_smoke_flow_denies_before_submit),
    ("smoke: residual risk / unexpected fill", test_smoke_flow_reports_residual_risk),
    ("dry-run: --scenario offline artifact", test_scenario_dryrun_offline),
    ("dry-run: fail-closed paths", test_dryrun_fail_closed),
]


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test runner reports everything
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
