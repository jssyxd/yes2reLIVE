#!/usr/bin/env python3
"""stdlib-only tests for the execution port + CLOB v2 transport.

No network, no v2 SDK, no real order: every transport is a stub and the paper path is
compared against golden numbers captured from the pre-port engine (``_r_cycle._paper_fire``).

Run:  python3.13 tests_port.py    → prints PASS/FAIL per check, exit = #failures.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _r_cycle  # noqa: E402  (must import cleanly without py-clob-client-v2)
import _r_state  # noqa: E402
import re_execution  # noqa: E402
from live import port as port_mod  # noqa: E402
from live import submit, v2_transport  # noqa: E402

TMP_AUDIT_DIR = tempfile.mkdtemp(prefix="port-tests-audit-")
submit.AUDIT_PATH = Path(TMP_AUDIT_DIR) / "live_events.jsonl"

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
ZERO = Decimal("0")

CFG = {"mode": "paper", "fire_budget_usdc": 20.0, "fire_budget_ms": 8000,
       "log_path": "/tmp/port_tests_events.jsonl", "paper_initial_capital_usdc": 600.0}

FIRE = {
    "key": "london|2026-09-10|high", "city_id": "london", "icao": "EGLL",
    "market_local_date": "2026-09-10", "local_fire_time": "2026-09-10T14:05:00+01:00",
    "market_unit": "C", "direction": "high", "ref_extreme": 25.5, "ref_source": "taf",
    "running_extreme": 26.0, "jump": 1, "budget_usdc": "20.0",
    "broken_bucket_id": "B1", "new_bucket_id": "B2",
    "legs": [
        {"leg": "buy_no_broken", "token_id": "TOK_NO", "side": "BUY", "outcome": "NO", "cap": "0.65",
         "notional_pct": "0.75", "floor": "", "bucket_lo": 25, "bucket_hi": 26, "bucket_label": "25-26"},
        {"leg": "buy_yes_new", "token_id": "TOK_YES", "side": "BUY", "outcome": "YES", "cap": "0.48",
         "notional_pct": "0.25", "floor": "0.48", "bucket_lo": 26, "bucket_hi": 27, "bucket_label": "26-27"},
    ],
}
BOOKS = {
    "TOK_NO": {"best_ask": "0.60", "tick_size": "0.01", "neg_risk": True,
               "asks": [{"price": "0.60", "size": "5"}, {"price": "0.61", "size": "40"}],
               "bids": [{"price": "0.59", "size": "100"}]},
    "TOK_YES": {"best_ask": None, "tick_size": "0.01", "neg_risk": True, "asks": [], "bids": []},
}

#: captured by running the PRE-PORT ``_r_cycle._paper_fire`` (git show HEAD:_r_cycle.py)
#: against FIRE/BOOKS above — the port must reproduce these exactly
GOLDEN_LEGS = {
    "buy_no_broken": {"cost_usdc": "14.0288", "shares": "23.08", "avg_price": "0.61", "bucket_id": "B1"},
    "buy_yes_new": {"cost_usdc": "0", "shares": "0", "avg_price": None, "bucket_id": "B2"},
}
GOLDEN_DEBIT = Decimal("14.0288")   # ledger stores it as a float
GOLDEN_LADDER = [("buy_no_broken", "send_fak", "0.60", "23.08"),
                 ("buy_yes_new", "no_book", None, None),
                 ("buy_no_broken", "send_fak", "0.62", "18.08"),
                 ("buy_yes_new", "no_book", None, None),
                 ("buy_yes_new", "no_book", None, None)]

VALID_ENV = {
    "POLY_PRIVATE_KEY": "0x" + "ab" * 32,
    "POLY_FUNDER_ADDRESS": "0x" + "cd" * 20,
    "POLY_SIGNATURE_TYPE": "1",
    "POLY_API_KEY": "k", "POLY_API_SECRET": "s", "POLY_API_PASSPHRASE": "p",
    "LIVE_SUBMIT_ENABLED": "1",
    "YES2RE_LIVE_ENABLE_SUBMIT": "1",
    "LIVE_FIRE_BUDGET_USDC": "12", "LIVE_MAX_OPEN_POSITIONS": "10", "LIVE_MAX_CAPITAL_USDC": "50",
}
GATES_OK = {"ok": True, "reason": "ok", "checks": {"cli_flag": True, "env_flag": True,
                                                   "confirm_phrase": True}}


def _live_env(**overrides):
    env = dict(VALID_ENV)
    env["YES2RE_LIVE_CONFIRM"] = submit.phrase()
    env.update(overrides)
    return env


# --------------------------------------------------------------------------- port selection

def test_port_selection_matrix():
    port_mod.reset_cache()
    paper = port_mod.get_port({"mode": "paper"}, env={})
    assert isinstance(paper, port_mod.PaperPort) and paper.mode == "paper"
    assert port_mod.get_port({}, env={}).mode == "paper"        # missing mode ⇒ paper
    assert port_mod.get_port({"mode": "PAPER"}, env={}).mode == "paper"

    # every single missing gate refuses, and never hands back a paper port
    full = {"YES2RE_LIVE_ENABLE_SUBMIT": "1", "LIVE_SUBMIT_ENABLED": "1",
            "YES2RE_LIVE_CONFIRM": submit.phrase()}
    matrix = [
        # service-side flag missing (env var not exported) ⇒ cli gate fails
        ({k: v for k, v in full.items() if k != "YES2RE_LIVE_ENABLE_SUBMIT"}, None, submit.GATE_FLAG),
        # explicit enable=False overrides the exported service flag
        ({k: v for k, v in full.items() if k != "YES2RE_LIVE_ENABLE_SUBMIT"}, False, submit.GATE_FLAG),
        # LIVE_SUBMIT_ENABLED missing
        ({k: v for k, v in full.items() if k != "LIVE_SUBMIT_ENABLED"}, None, submit.GATE_ENV),
        # confirm phrase absent / stale
        ({k: v for k, v in full.items() if k != "YES2RE_LIVE_CONFIRM"}, None, submit.GATE_CONFIRM_MISSING),
        ({**full, "YES2RE_LIVE_CONFIRM": "SMOKE-1999-01-01"}, None, submit.GATE_CONFIRM_MISMATCH),
        # env-only (no service-side opt-in exported) is not enough either
        ({"LIVE_SUBMIT_ENABLED": "1", "YES2RE_LIVE_CONFIRM": submit.phrase()}, None, submit.GATE_FLAG),
        ({}, True, submit.GATE_ENV),
    ]
    for env, enable, reason in matrix:
        try:
            got = port_mod.get_port({"mode": "live"}, env=env, enable_submit=enable)
        except port_mod.PortRefused as exc:
            assert exc.reason == reason, (env, enable, exc.reason, reason)
        else:
            raise AssertionError(f"live must be refused for {env} (enable={enable}), got {got!r}")
    # and the engine never silently downgrades: the fire returns no position on refusal
    status = port_mod.port_status({"mode": "live"}, env={})
    assert status["ok"] is False and status["reason"] == submit.GATE_FLAG, status

    try:
        port_mod.get_port({"mode": "warp"}, env={})
    except port_mod.PortRefused as exc:
        assert exc.reason == port_mod.REASON_UNKNOWN_MODE, exc.reason
    else:
        raise AssertionError("unknown mode must be refused")

    # all gates satisfied ⇒ LivePort with the stubbed transport
    port_mod.reset_cache()
    live = port_mod.get_port({"mode": "live"}, env=_live_env(), transport=object())
    assert isinstance(live, port_mod.LivePort) and live.mode == "live"

    # F3: a missing v2 SDK is reported as live_deps_missing (still fail-closed, never paper)
    class _NoSdk:
        RELEASE_WRITE_METHODS = ()
        V2_HINT = "py-clob-client-v2 is not importable …"

        @staticmethod
        def sdk_available():
            return False

    port_mod.reset_cache()
    try:
        port_mod.get_port({"mode": "live"}, env=_live_env(), transport=_NoSdk())
    except port_mod.PortRefused as exc:
        assert exc.reason == port_mod.REASON_LIVE_DEPS, exc.reason
        assert "not importable" in exc.detail, exc.detail
    else:
        raise AssertionError("a missing v2 SDK must be refused with live_deps_missing")
    # and a transport without the probe stays usable (backward compatible)
    port_mod.reset_cache()
    assert port_mod.get_port({"mode": "live"}, env=_live_env(), transport=object()).mode == "live"
    port_mod.reset_cache()
    assert live.describe()["mode"] == "live"
    port_mod.reset_cache()
    assert isinstance(port_mod.get_port({"mode": "paper"}, env={}), port_mod.PaperPort)


# --------------------------------------------------------------------------- paper regression

def test_paper_fire_matches_pre_port_golden():
    """The port-driven fire must reproduce the pre-port engine exactly, field by field."""
    state: dict = {"paper_initial_capital_usdc": 600.0}
    events: list = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_patched(_r_cycle, book_cache=lambda: copy.deepcopy(BOOKS),
                                     log_event=lambda path, payload: events.append(payload)))
        position, ladlog = _r_cycle._paper_fire(CFG, state, copy.deepcopy(FIRE), NOW)
    assert position is not None, "paper fire must still open a position"
    assert position["key"] == FIRE["key"] and position["kind"] == "reversal"
    assert position["budget_usdc"] == "20.0" and position["jump"] == 1
    assert position["settled"] is False
    legs = {leg["leg"]: leg for leg in position["legs"]}
    assert set(legs) == set(GOLDEN_LEGS), sorted(legs)
    for name, golden in GOLDEN_LEGS.items():
        got = {key: legs[name][key] for key in golden}
        assert got == golden, (name, got, golden)
    assert position["fires_at_utc"] == "2026-09-10T12:00:00Z"
    assert float(state["paper_total_debit_usdc"]) == float(GOLDEN_DEBIT), state
    assert events == [], events
    ladder = [(i.get("leg"), i.get("status"), i.get("limit_price"), i.get("shares")) for i in ladlog]
    assert ladder == GOLDEN_LADDER, ladder
    assert ladlog[0]["fill"] == {"filled": "5", "avg": "0.60", "unfilled": "18.08"}
    assert ladlog[2]["fill"] == {"filled": "18.08", "avg": "0.61", "unfilled": "0.00"}


def test_paper_port_matches_matcher_and_ledger():
    """PaperPort.match is literally paper_match_fak; fund is literally reserve."""
    book = {"best_ask": "0.60", "tick_size": "0.01",
            "asks": [{"price": "0.60", "size": "5"}, {"price": "0.62", "size": "9"}]}
    direct = re_execution.paper_match_fak(copy.deepcopy(book), Decimal("0.61"), Decimal("10"))
    via_port = port_mod.PaperPort().match(leg={"side": "BUY"}, book=copy.deepcopy(book),
                                          limit=Decimal("0.61"), shares=Decimal("10"))
    for key in ("filled_shares", "avg_price", "cost", "unfilled"):
        assert via_port[key] == direct[key], (key, via_port[key], direct[key])
    state = {"paper_initial_capital_usdc": 100.0}
    assert port_mod.PaperPort().fund(state=state, cfg=CFG, fire=FIRE,
                                     total_cost=Decimal("12.5"))["ok"] is True
    assert state["paper_total_debit_usdc"] == Decimal("12.5")
    poor = {"paper_initial_capital_usdc": 1.0}
    denied = port_mod.PaperPort().fund(state=poor, cfg=CFG, fire=FIRE, total_cost=Decimal("5"))
    assert denied["ok"] is False and denied["reason"] == "fire_insufficient_capital", denied
    assert poor.get("paper_total_debit_usdc") is None, "a denied funding must not mutate the ledger"
    assert port_mod.PaperPort().fund(state={}, cfg=CFG, fire=FIRE, total_cost=ZERO)["ok"] is True
    assert port_mod.PaperPort().preflight(fire=FIRE, cfg=CFG)["ok"] is True


def test_paper_fire_refuses_when_live_has_no_gates():
    """cfg mode=live without gates: no position, machine-readable refusal, no paper fallback."""
    fake = {"mode": "live"}
    state: dict = {"paper_initial_capital_usdc": 600.0}
    events: list = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_patched(_r_cycle, book_cache=lambda: copy.deepcopy(BOOKS),
                                     log_event=lambda path, payload: events.append(payload)))
        stack.enter_context(_patched(port_mod, creds_mod=types.SimpleNamespace(load_env_file=lambda: {})))
        position, ladlog = _r_cycle._paper_fire(fake, state, copy.deepcopy(FIRE), NOW)
    assert position is None, "must not open a position without live gates"
    assert ladlog and ladlog[0]["status"] == "port_refused", ladlog
    assert ladlog[0]["reason"] in (submit.GATE_FLAG, submit.GATE_ENV, submit.GATE_CONFIRM_MISSING)
    assert state.get("paper_total_debit_usdc") is None
    assert events and events[0]["type"] == "fire_port_refused", events


def test_paper_engine_needs_no_v2_sdk():
    """Importing the engine (and asking for the paper port) must not touch py-clob-client-v2."""
    assert "py_clob_client_v2" not in sys.modules, "the v2 SDK leaked into a stdlib run"
    port_mod.reset_cache()
    got = port_mod.get_port({"mode": "paper"}, env={}, transport=None)
    assert isinstance(got, port_mod.PaperPort)
    assert "py_clob_client_v2" not in sys.modules, "paper port must not import the v2 SDK"
    assert _r_cycle.__file__.endswith("_r_cycle.py")
    # the v2 transport itself stays importable (its SDK import is lazy)
    assert callable(v2_transport.build_client)
    assert "py_clob_client_v2" not in sys.modules, "v2_transport imported the SDK eagerly"
    try:
        v2_transport._py_clob_v2()
    except RuntimeError as exc:                      # stdlib interpreter: the hint must fire
        assert "live-probe-v2" in str(exc), exc
    else:                                            # v2 venv: the SDK really is importable
        assert "py_clob_client_v2" in sys.modules, "v2 must be loaded only on explicit request"


# --------------------------------------------------------------------------- live port (stubs)

class _StubTransport:
    """Module-like stub: records calls, returns scripted results. Never touches the network."""

    SUBMIT_METHODS = v2_transport.SUBMIT_METHODS
    ADMIN_METHODS = v2_transport.ADMIN_METHODS
    STATE_WRITE_METHODS = v2_transport.STATE_WRITE_METHODS
    RFQ_SUBMIT_METHODS = v2_transport.RFQ_SUBMIT_METHODS
    RELEASE_WRITE_METHODS = v2_transport.RELEASE_WRITE_METHODS
    RELEASE_READ_METHODS = v2_transport.RELEASE_READ_METHODS

    def __init__(self, *, results=None):
        self.calls = []
        self.results = list(results or [])

    def build_client(self, creds):      # pragma: no cover - only used via LivePort.preflight
        self.calls.append(("build_client",))
        return object()

    read_account = staticmethod(lambda client, address=None: {"usdc_balance": 51.0, "open_orders": 0,
                                                             "positions": [], "positions_value_usdc": 0.0})

    def execute_leg(self, client, **kwargs):
        self.calls.append(("execute_leg", kwargs))
        return self.results.pop(0) if self.results else {"ok": True, "status": "filled",
                                                         "filled_shares": ZERO, "avg_price": None,
                                                         "cost": ZERO, "unfilled": kwargs["size"],
                                                         "detail": ""}


ACCOUNT_OK = {"usdc_balance": 51.0, "open_orders": 0, "positions": [], "positions_value_usdc": 0.0}


def _live_port(transport, *, account=None, limits=None, preflight=False):
    port_mod.reset_cache()
    port = port_mod.LivePort(transport, env=_live_env(), gates=GATES_OK,
                             limits=limits or {"fire_budget_usdc": "12", "max_open_positions": "10",
                                               "max_capital_usdc": "50"},
                             account_reader=(lambda client: account) if account else None,
                             sleep=lambda _s: None)
    if preflight:
        ok = port.preflight(fire={**FIRE, "budget_usdc": "12"}, cfg={"fire_budget_usdc": 12})
        assert ok["ok"] is True, ok
    return port


def test_live_port_preflight_uses_real_caps():
    transport = _StubTransport()
    port = _live_port(transport, account={"usdc_balance": 51.0, "open_orders": 0, "positions": [],
                                          "positions_value_usdc": 0.0})
    ok = port.preflight(fire={**FIRE, "budget_usdc": "12"}, cfg={"fire_budget_usdc": 12})
    assert ok["ok"] is True and ok["account"]["usdc_balance"] == 51.0, ok
    assert ok["risk_gate"]["allow"] is True and ok["limits"]["ok"] is True, ok

    poor = _live_port(transport, account={"usdc_balance": 3.0, "open_orders": 0, "positions": [],
                                          "positions_value_usdc": 0.0})
    denied = poor.preflight(fire={**FIRE, "budget_usdc": "12"}, cfg={"fire_budget_usdc": 12})
    assert denied["ok"] is False and denied["reason"] == "risk_gate:budget_exceeds_balance", denied

    crowded = _live_port(transport, account={"usdc_balance": 51.0, "open_orders": 0,
                                             "positions": [{"id": i} for i in range(10)],
                                             "positions_value_usdc": 1.0})
    denied2 = crowded.preflight(fire=FIRE, cfg={"fire_budget_usdc": 12})
    assert denied2["ok"] is False and denied2["reason"] == "risk_gate:max_open_positions_reached", denied2

    over_budget = _live_port(transport, account={"usdc_balance": 51.0, "open_orders": 0,
                                                 "positions": [], "positions_value_usdc": 45.0})
    denied3 = over_budget.preflight(fire={**FIRE, "budget_usdc": "12"}, cfg={"fire_budget_usdc": 12})
    assert denied3["ok"] is False and denied3["reason"].startswith(("limits:", "risk_gate:")), denied3

    # an unreadable account is fail-closed too
    def _boom(_client):
        raise RuntimeError("no network")

    broken = _live_port(transport, account=None)
    broken.account_reader = _boom
    unreadable = broken.preflight(fire=FIRE, cfg=CFG)
    assert unreadable["ok"] is False and unreadable["reason"] == "live_account_unreadable", unreadable


def test_live_port_match_branches():
    """Five offline branches: partial fill, full fill, no fill, cancel failure, no preflight."""
    book = {"best_ask": "0.60", "best_bid": "0.59", "tick_size": "0.01", "neg_risk": True}
    leg = {"leg": "buy_no_broken", "token_id": "TOK_NO", "side": "BUY"}

    partial = _StubTransport(results=[{"ok": True, "status": "matched", "filled_shares": Decimal("6"),
                                       "avg_price": Decimal("0.60"), "cost": Decimal("3.6"),
                                       "unfilled": Decimal("4"), "limit_price": "0.59",
                                       "detail": "", "residual_risk": False}])
    port = _live_port(partial, account=ACCOUNT_OK, preflight=True)
    got = port.match(leg=leg, book=book, limit=Decimal("0.59"), shares=Decimal("10"))
    assert got["filled_shares"] == Decimal("6") and got["cost"] == Decimal("3.6"), got
    assert got["unfilled"] == Decimal("4") and got["source"] == "live", got
    kwargs = [call[1] for call in partial.calls if call[0] == "execute_leg"][0]
    assert kwargs["post_only"] is True and kwargs["size"] == Decimal("10"), kwargs

    full = _StubTransport(results=[{"ok": True, "status": "matched", "filled_shares": Decimal("10"),
                                    "avg_price": Decimal("0.59"), "cost": Decimal("5.9"),
                                    "unfilled": ZERO, "detail": ""}])
    assert _live_port(full, account=ACCOUNT_OK, preflight=True).match(
        leg=leg, book=book, limit=Decimal("0.59"), shares=Decimal("10"))["unfilled"] == ZERO

    unfilled = _StubTransport(results=[{"ok": True, "status": "cancelled", "filled_shares": ZERO,
                                        "avg_price": None, "cost": ZERO, "unfilled": Decimal("10"),
                                        "detail": ""}])
    got2 = _live_port(unfilled, account=ACCOUNT_OK, preflight=True).match(
        leg=leg, book=book, limit=Decimal("0.59"), shares=Decimal("10"))
    assert got2["filled_shares"] == ZERO and got2["unfilled"] == Decimal("10"), got2

    cancel_failed = _StubTransport(results=[{"ok": True, "status": "timeout", "filled_shares": ZERO,
                                             "avg_price": None, "cost": ZERO, "unfilled": None,
                                             "residual_risk": True, "detail": "cancel not confirmed"}])
    got3 = _live_port(cancel_failed, account=ACCOUNT_OK, preflight=True).match(
        leg=leg, book=book, limit=Decimal("0.59"), shares=Decimal("10"))
    assert got3["residual_risk"] is True and got3["status"] == "timeout", got3
    assert "cancel not confirmed" in got3["detail"], got3

    never_preflighted = _live_port(_StubTransport())
    blocked = never_preflighted.match(leg=leg, book=book, limit=Decimal("0.59"), shares=Decimal("10"))
    assert blocked["status"] == "live_not_preflighted" and blocked["filled_shares"] == ZERO, blocked
    assert blocked["unfilled"] == Decimal("10")


# --------------------------------------------------------------------------- v2 transport (stubs)

def test_v2_clamp_limit():
    """Clamping is what keeps a post_only order from being rejected as crossing the book."""
    book = {"best_ask": "0.60", "best_bid": "0.58", "tick_size": "0.01"}
    buy = v2_transport.clamp_limit(side="BUY", limit="0.599", book=book)
    assert buy["ok"] and buy["price"] == Decimal("0.59") and buy["clamped"] is True, buy
    assert buy["price"] < Decimal(book["best_ask"]), buy
    keep = v2_transport.clamp_limit(side="BUY", limit="0.55", book=book)
    assert keep["ok"] and keep["price"] == Decimal("0.55") and keep["clamped"] is False, keep
    sell = v2_transport.clamp_limit(side="SELL", limit="0.575", book=book)
    assert sell["ok"] and sell["price"] == Decimal("0.59") and sell["clamped"] is True, sell
    for bad, reason in (({"best_ask": "0.01", "tick_size": "0.01"}, "no_passive_price"),
                        ({"best_ask": None, "tick_size": "0.01"}, "no_book"),
                        (None, "no_book")):
        got = v2_transport.clamp_limit(side="BUY", limit="0.50", book=bad)
        assert got["ok"] is False and got["reason"] == reason, (bad, got)
    assert v2_transport.clamp_limit(side="HOLD", limit="0.5", book=book)["reason"] == "invalid_input"
    assert v2_transport.clamp_limit(side="BUY", limit=None, book=book)["reason"] == "invalid_input"


class _Summary:
    """Minimal OrderBookSummary look-alike (attributes, like the real v2 client returns)."""

    def __init__(self, *, bid="0.58", ask="0.60", tick="0.01", min_size="5", neg_risk=True):
        self.tick_size = tick
        self.min_order_size = min_size
        self.neg_risk = neg_risk
        self.bids = [] if bid is None else [{"price": bid, "size": "100"}]
        self.asks = [] if ask is None else [{"price": ask, "size": "100"}]


class _StubV2Client:
    """Stands in for the v2 ClobClient: scripted order/trade/cancel answers, call log."""

    def __init__(self, *, book=None, order_states=None, trades=None, cancel_response=None,
                 cancel_raises=False):
        self.calls = []
        self._book = book or _Summary()
        self._states = list(order_states or [])
        self._trades = trades or []
        self._cancel = cancel_response if cancel_response is not None else {"canceled": ["ORD-1"]}
        self._cancel_raises = cancel_raises

    def get_order_book(self, token_id):
        return self._book

    def create_order(self, args, options):
        self.calls.append(("create_order", args, options))
        return "SIGNED-V2"

    def post_order(self, signed, order_type, post_only=False):
        self.calls.append(("post_order", signed, str(order_type), post_only))
        return {"orderID": "ORD-1", "status": "live", "success": True}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        state = self._states.pop(0) if self._states else {"status": "live", "size_matched": "0",
                                                          "original_size": "10", "price": "0.59",
                                                          "asset_id": "TOK_NO"}
        return state

    def get_trades(self):
        self.calls.append(("get_trades",))
        return self._trades

    def cancel_orders(self, ids):
        self.calls.append(("cancel_orders", tuple(ids)))
        if self._cancel_raises:
            raise AttributeError("cancel_order() missing 1 required positional argument")
        return self._cancel


@contextlib.contextmanager
def _stub_v2_lib():
    """Minimal py_clob_client_v2.clob_types so v2_transport runs under a stdlib interpreter."""
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
    client_mod.ClobClient = _StubV2Client
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


def test_v2_execute_leg_end_to_end():
    """One passive post-only order: clamped price, one submit, real fill, cancel of the rest."""
    client = _StubV2Client(order_states=[{"status": "matched", "size_matched": "6",
                                          "original_size": "10", "price": "0.59",
                                          "asset_id": "TOK_NO"}],
                           trades=[{"orderID": "ORD-1", "size": "6", "price": "0.59"}])
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        result = v2_transport.execute_leg(
            client, token_id="TOK_NO", side="BUY", price="0.599", size="10",
            book={"best_ask": "0.60", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, sleep=lambda _s: None, poll_attempts=2, audit_path=log)
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert result["ok"] is True and result["order_id"] == "ORD-1", result
    assert result["limit_price"] == "0.59" and result["clamped"] is True, result
    assert result["filled_shares"] == Decimal("6") and result["cost"] == Decimal("3.54"), result
    assert result["avg_price"] == Decimal("0.59"), result
    kinds = [call[0] for call in client.calls]
    assert kinds.count("post_order") == 1 and kinds.count("create_order") == 1, kinds
    *_, post_only = [c for c in client.calls if c[0] == "post_order"][0]
    assert post_only is True
    assert ("cancel_orders", ("ORD-1",)) in client.calls, client.calls
    assert [line["action"] for line in lines] == ["intent", "submit", "cancel"], lines

    # submit without gates — or with a *forged/truncated* gate record — is impossible
    forged = [
        None,
        {},
        {"ok": True},                                                   # truthy ok, no checks
        {"ok": True, "checks": {}},
        {"ok": True, "checks": {"cli_flag": True}},                     # partial checks
        {"ok": True, "checks": {"cli_flag": True, "env_flag": True}},
        {"ok": True, "checks": {"cli_flag": True, "env_flag": True, "confirm_phrase": False}},
    ]
    before = list(client.calls)
    with tempfile.TemporaryDirectory() as tmp:
        for bad_gates in forged:
            for call in (lambda: v2_transport.execute_leg(client, token_id="T", side="BUY",
                                                          price="0.5", size="5", gates=bad_gates,
                                                          audit_path=Path(tmp) / "a.jsonl"),
                         lambda: submit.submit_order(client, token_id="T", price="0.5", size="5",
                                                     gates=bad_gates)):
                try:
                    call()
                except PermissionError:
                    pass
                else:
                    raise AssertionError(f"gates={bad_gates!r} must be refused")
    assert client.calls == before, f"nothing may be signed/sent with a bad gate record: {client.calls}"


def test_v2_cancel_retry_and_residual_risk():
    """A cancel that keeps failing must be reported with explicit residual risk."""
    client = _StubV2Client(order_states=[{"status": "live", "size_matched": "0", "original_size": "10",
                                          "price": "0.59", "asset_id": "TOK_NO"}] * 3,
                           cancel_response={"canceled": [], "not_canceled": {"ORD-1": "nope"}})
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        result = v2_transport.execute_leg(client, token_id="TOK_NO", side="BUY", price="0.59",
                                          size="10",
                                          book={"best_ask": "0.60", "tick_size": "0.01"},
                                          gates=GATES_OK, sleep=lambda _s: None, poll_attempts=2,
                                          audit_path=log)
    assert result["ok"] is True and result["filled_shares"] == ZERO, result
    assert result["residual_risk"] is True, result
    assert "residual" in result["detail"] or "not confirmed" in result["detail"], result
    cancels = [c for c in client.calls if c[0] == "cancel_orders"]
    assert len(cancels) == 3, cancels                     # retried
    assert result["cancel"]["attempts"] == 3, result["cancel"]

    # the retry also survives the v2 AttributeError trap on single-arg cancel
    flaky = _StubV2Client(order_states=[{"status": "live", "size_matched": "0",
                                         "original_size": "10", "price": "0.59",
                                         "asset_id": "TOK_NO"}] * 2, cancel_raises=True)
    with _stub_v2_lib():
        got = v2_transport.execute_leg(flaky, token_id="TOK_NO", side="BUY", price="0.59", size="10",
                                       book={"best_ask": "0.60", "tick_size": "0.01"},
                                       gates=GATES_OK, sleep=lambda _s: None, poll_attempts=1)
    assert got["residual_risk"] is True and got["cancel"]["ok"] is False, got


def test_v2_cancel_summary_tolerates_shapes():
    """v2 may answer with a dict *or* a bare string — both must be understood."""
    assert v2_transport.cancel_summary({"canceled": ["A"], "not_canceled": {}})["canceled"] == ["A"]
    assert v2_transport.cancel_summary("ok") == {"raw": "ok"}
    assert v2_transport.cancel_summary(None) == {"raw": "None"}
    assert v2_transport.canceled_ids({"canceled": ["A", "B"]}) == ["A", "B"]
    assert v2_transport.canceled_ids("nope") == []
    assert v2_transport.order_id_of({"orderID": "X"}) == "X"
    assert v2_transport.order_id_of(types.SimpleNamespace(orderId="Y")) == "Y"
    assert v2_transport.order_summary(types.SimpleNamespace(status="live", size_matched="0"))["status"] == "live"
    assert v2_transport.order_summary({"status": "matched"})["status"] == "matched"
    assert "raw" in v2_transport.order_summary("not json")


def test_v2_poll_fill_branches():
    matched = _StubV2Client(order_states=[{"status": "live", "size_matched": "0"},
                                          {"status": "matched", "size_matched": "10",
                                           "original_size": "10", "price": "0.59"}])
    got = v2_transport.poll_fill(matched, "ORD-1", attempts=3, sleep=lambda _s: None, sleep_seconds=0)
    assert got["ok"] and got["terminal"] and got["filled_shares"] == Decimal("10"), got
    assert got["attempts"] == 2, got

    timed_out = _StubV2Client(order_states=[{"status": "live", "size_matched": "0"}] * 2)
    late = v2_transport.poll_fill(timed_out, "ORD-1", attempts=2, sleep=lambda _s: None, sleep_seconds=0)
    assert late["ok"] is False and late["status"] == "timeout" and late["filled_shares"] == ZERO, late

    partial_then_cancelled = _StubV2Client(order_states=[{"status": "cancelled", "size_matched": "4",
                                                          "original_size": "10", "price": "0.59"}],
                                           trades=[{"orderID": "ORD-1", "size": "4", "price": "0.59"}])
    half = v2_transport.poll_fill(partial_then_cancelled, "ORD-1", attempts=1, sleep=lambda _s: None,
                                  sleep_seconds=0)
    assert half["ok"] and half["filled_shares"] == Decimal("4") and half["avg_price"] == Decimal("0.59"), half


def test_v2_sentinels_least_privilege():
    """Only post_order + cancel_orders are released on v2; everything else stays blocked."""
    write_names = (v2_transport.SUBMIT_METHODS + v2_transport.ADMIN_METHODS
                   + v2_transport.STATE_WRITE_METHODS)

    class _Surface:
        pass

    for name in write_names + ("get_order", "get_open_orders", "get_trades"):
        setattr(_Surface, name, lambda self, *a, **k: {"called": True})
    client = _Surface()
    client.rfq = None

    armed = v2_transport.arm_controlled_sentinels(client)
    assert armed["released"] == ["client.post_order", "client.cancel_orders"], armed["released"]
    assert set(v2_transport.RELEASE_WRITE_METHODS) == {"post_order", "cancel_orders"}
    assert "get_order_book" in v2_transport.RELEASE_READ_METHODS
    # F2: the v2 single-id cancel (DELETE /order) is armed but NEVER released
    assert "cancel_order" in v2_transport.SUBMIT_METHODS, "cancel_order must be armed"
    assert "cancel_order" not in v2_transport.RELEASE_WRITE_METHODS, "cancel_order must stay blocked"
    assert "client.cancel_order" in armed["still_blocked"], armed["still_blocked"]
    try:
        client.cancel_order("ORD-1")
    except RuntimeError as exc:
        assert "SUBMIT BLOCKED" in str(exc), exc
    else:
        raise AssertionError("cancel_order() must be sentinel-blocked on the v2 client")
    must_stay = [n for n in write_names if n not in v2_transport.RELEASE_WRITE_METHODS]
    for name in must_stay:
        assert f"client.{name}" in armed["still_blocked"], (name, armed["still_blocked"])
        try:
            getattr(client, name)()
        except RuntimeError as exc:
            assert "SUBMIT BLOCKED" in str(exc), exc
        else:
            raise AssertionError(f"{name}() was not blocked — least privilege violated")
    # reads stay usable (they were never sentineled)
    assert getattr(client, "get_order")() == {"called": True}

    class _NoPostOrder:
        rfq = None

    try:
        v2_transport.arm_controlled_sentinels(_NoPostOrder())
    except RuntimeError as exc:
        assert "post_order" in str(exc), exc
    else:
        raise AssertionError("arming without post_order must fail closed")


# --------------------------------------------------- Phase 3b-2: deployment env overrides

#: ``_r_state.load_config("config/yes2re_reversal.json")`` captured BEFORE the env-override
#: change (HEAD 69743da) — the no-override path must stay bit-for-bit identical to this.
GOLDEN_CONFIG = json.loads(r"""
{
 "active_icaos": null,
 "arm_book_interval_seconds": 8,
 "arm_metar_interval_seconds": 5,
 "base_fee_rate": "0.02",
 "checkwx_api_key_env": "CHECKWX_API_KEY",
 "contract_cities_path": "config/contract_cities.json",
 "fast_poll_interval_seconds": 5,
 "fire_budget_usdc": 20.0,
 "health_path": "data/yes2re_health.json",
 "idle_book_interval_seconds": 30,
 "idle_metar_interval_seconds": 30,
 "log_path": "data/yes2re_events.jsonl",
 "max_open_positions": 12,
 "mode": "paper",
 "paper_initial_capital_usdc": 600.0,
 "rules_refresh_interval_seconds": 1200,
 "scan_interval_seconds": 20,
 "settle_grace_hours": 2,
 "settle_max_hours": 72,
 "settle_poll_seconds": 60,
 "state_path": "data/yes2re_state.json",
 "strategy": {
  "allow_market_consensus_reference": true,
  "allow_market_ref_fire": true,
  "arm_c": 1.0,
  "break_confirm_margin_f": 1.0,
  "consensus_min_lead": "0.03",
  "consensus_min_samples": 8,
  "consensus_window_seconds": 3600,
  "fast_poll_seconds": 8,
  "fire_budget_ms": 8000,
  "high_fire_local_hour_end": 18,
  "high_fire_local_start": 12,
  "low_fire_local_end": 9,
  "low_fire_local_start": 0,
  "max_bucket_jump": 1,
  "max_obs_future_seconds": 900,
  "max_obs_lookback_seconds": 5400,
  "no_max_ask": "1.0",
  "no_notional_pct": 0.25,
  "require_consensus_filter": true,
  "require_fresh_obs_seconds": 180,
  "sleeve_enabled": false,
  "sleeve_long_window_s": 600,
  "sleeve_max_ask": 0.35,
  "sleeve_min_ticks": 4,
  "sleeve_neighbour_rise": 0.04,
  "sleeve_notional_pct": 0.08,
  "sleeve_rank1_drop": 0.05,
  "sleeve_short_window_s": 150,
  "sleeve_timeout_s": 1800,
  "yes_leg_enabled": true,
  "yes_max_ask": "0.9",
  "yes_min_ask": "0.48",
  "yes_notional_pct": 0.75
 },
 "taf_refresh_interval_seconds": 1800,
 "tail_hours": 144,
 "ws_triggered_metar_enabled": true
}
""")

GOLDEN_STRATEGY = json.loads(r"""
{
 "allow_market_consensus_reference": true,
 "allow_market_ref_fire": true,
 "arm_c": 1.0,
 "break_confirm_margin_f": 1.0,
 "consensus_min_lead": "0.03",
 "consensus_min_samples": 8,
 "consensus_window_seconds": 3600,
 "fast_poll_seconds": 8,
 "fire_budget_ms": 8000,
 "high_fire_local_hour_end": 18,
 "high_fire_local_start": 12,
 "low_fire_local_end": 9,
 "low_fire_local_start": 0,
 "max_bucket_jump": 1,
 "max_obs_future_seconds": 900,
 "max_obs_lookback_seconds": 5400,
 "no_max_ask": "1.0",
 "no_notional_pct": 0.25,
 "require_consensus_filter": true,
 "require_fresh_obs_seconds": 180,
 "sleeve_enabled": false,
 "sleeve_long_window_s": 600,
 "sleeve_max_ask": 0.35,
 "sleeve_min_ticks": 4,
 "sleeve_neighbour_rise": 0.04,
 "sleeve_notional_pct": 0.08,
 "sleeve_rank1_drop": 0.05,
 "sleeve_short_window_s": 150,
 "sleeve_timeout_s": 1800,
 "yes_leg_enabled": true,
 "yes_max_ask": "0.9",
 "yes_min_ask": "0.48",
 "yes_notional_pct": 0.75
}
""")


def test_load_config_env_overrides():
    """The three deployment overrides work, fail closed, and never touch strategy params."""
    import os
    from unittest import mock

    baseline = _r_state.load_config("config/yes2re_reversal.json")
    assert baseline == GOLDEN_CONFIG, "no-env load_config changed vs the pre-change baseline"
    assert baseline["mode"] == "paper" and baseline["fire_budget_usdc"] == 20.0
    assert baseline["max_open_positions"] == 12
    assert baseline["strategy"] == GOLDEN_STRATEGY

    # (1) each override lands exactly, and only on its own key
    cases = [
        ({"YES2RE_MODE": "live"}, {"mode": "live"}),
        ({"YES2RE_MODE": "paper"}, {"mode": "paper"}),
        ({"YES2RE_MODE": " LIVE "}, {"mode": "live"}),          # trimmed + lower-cased
        ({"YES2RE_FIRE_BUDGET_USDC": "7.5"}, {"fire_budget_usdc": 7.5}),
        ({"YES2RE_FIRE_BUDGET_USDC": "1e1"}, {"fire_budget_usdc": 10.0}),
        ({"YES2RE_MAX_OPEN_POSITIONS": "3"}, {"max_open_positions": 3}),
        ({"YES2RE_MAX_OPEN_POSITIONS": "+4"}, {"max_open_positions": 4}),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "1234.56"},
         {"paper_initial_capital_usdc": 1234.56}),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "500"}, {"paper_initial_capital_usdc": 500.0}),
        ({"YES2RE_MODE": "live", "YES2RE_FIRE_BUDGET_USDC": "12", "YES2RE_MAX_OPEN_POSITIONS": "10",
          "YES2RE_INITIAL_CAPITAL_USDC": "51.713622"},
         {"mode": "live", "fire_budget_usdc": 12.0, "max_open_positions": 10,
          "paper_initial_capital_usdc": 51.713622}),
    ]
    for env, expected in cases:
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = _r_state.load_config("config/yes2re_reversal.json", env=env)
        for key, value in expected.items():
            assert cfg[key] == value, (env, key, cfg[key], value)
        untouched = [k for k in GOLDEN_CONFIG if k not in expected]
        assert all(cfg[k] == GOLDEN_CONFIG[k] for k in untouched), (env, "override leaked")
        assert cfg["strategy"] == GOLDEN_STRATEGY, (env, "strategy parameters must never move")

    # (2) empty/absent values are ignored (not an override, not an error)
    for env in ({}, {"YES2RE_MODE": ""}, {"YES2RE_MODE": "   "},
                {"YES2RE_FIRE_BUDGET_USDC": ""}, {"YES2RE_MAX_OPEN_POSITIONS": ""},
                {"YES2RE_INITIAL_CAPITAL_USDC": ""}):
        assert _r_state.load_config("config/yes2re_reversal.json", env=env) == GOLDEN_CONFIG, env

    # (3) illegal values fail closed with a message naming the variable
    bad = [
        ({"YES2RE_MODE": "warp"}, "YES2RE_MODE"),
        ({"YES2RE_MODE": "true"}, "YES2RE_MODE"),
        ({"YES2RE_FIRE_BUDGET_USDC": "abc"}, "YES2RE_FIRE_BUDGET_USDC"),
        ({"YES2RE_FIRE_BUDGET_USDC": "-1"}, "YES2RE_FIRE_BUDGET_USDC"),
        ({"YES2RE_FIRE_BUDGET_USDC": "0"}, "YES2RE_FIRE_BUDGET_USDC"),
        ({"YES2RE_FIRE_BUDGET_USDC": "NaN"}, "YES2RE_FIRE_BUDGET_USDC"),
        ({"YES2RE_FIRE_BUDGET_USDC": "inf"}, "YES2RE_FIRE_BUDGET_USDC"),
        ({"YES2RE_MAX_OPEN_POSITIONS": "0"}, "YES2RE_MAX_OPEN_POSITIONS"),
        ({"YES2RE_MAX_OPEN_POSITIONS": "-2"}, "YES2RE_MAX_OPEN_POSITIONS"),
        ({"YES2RE_MAX_OPEN_POSITIONS": "2.5"}, "YES2RE_MAX_OPEN_POSITIONS"),
        ({"YES2RE_MAX_OPEN_POSITIONS": "many"}, "YES2RE_MAX_OPEN_POSITIONS"),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "0"}, "YES2RE_INITIAL_CAPITAL_USDC"),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "-5"}, "YES2RE_INITIAL_CAPITAL_USDC"),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "abc"}, "YES2RE_INITIAL_CAPITAL_USDC"),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "NaN"}, "YES2RE_INITIAL_CAPITAL_USDC"),
        ({"YES2RE_INITIAL_CAPITAL_USDC": "inf"}, "YES2RE_INITIAL_CAPITAL_USDC"),
    ]
    for env, key in bad:
        try:
            _r_state.load_config("config/yes2re_reversal.json", env=env)
        except SystemExit as exc:
            assert key in str(exc), (env, exc)
        else:
            raise AssertionError(f"{env} must fail closed")

    # (4) the config *file* can still never choose a non-paper mode (safety lock intact)
    with tempfile.TemporaryDirectory() as tmp:
        rogue = Path(tmp) / "rogue.json"
        rogue.write_text(json.dumps({"mode": "live"}), encoding="utf-8")
        try:
            _r_state.load_config(rogue)
        except SystemExit as exc:
            assert "safety lock" in str(exc), exc
        else:
            raise AssertionError("a config file must not be able to select live mode")
        # ...but an explicit env opt-in wins over the file (documented operator override)
        with contextlib.redirect_stderr(io.StringIO()):
            assert _r_state.load_config(rogue, env={"YES2RE_MODE": "live"})["mode"] == "live"
        assert _r_state.load_config(rogue, env={"YES2RE_MODE": "paper"})["mode"] == "paper"
    try:
        _r_state._validate_config({"mode": "live"}, Path("x.json"))
    except SystemExit as exc:
        assert "safety lock" in str(exc), exc
    else:
        raise AssertionError("_validate_config must keep the paper-only lock by default")

    # (5) the real os.environ path is what the runner uses (env=None)
    with mock.patch.dict(os.environ, {"YES2RE_MODE": "live", "YES2RE_FIRE_BUDGET_USDC": "9",
                                      "YES2RE_MAX_OPEN_POSITIONS": "2"}, clear=False):
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = _r_state.load_config("config/yes2re_reversal.json")
        assert (cfg["mode"], cfg["fire_budget_usdc"], cfg["max_open_positions"]) == ("live", 9.0, 2)
        assert cfg["strategy"] == GOLDEN_STRATEGY
        assert _r_state._env_overrides()["mode_explicit"] is True
    assert _r_state.ENV_OVERRIDE_KEYS == ("YES2RE_MODE", "YES2RE_FIRE_BUDGET_USDC",
                                          "YES2RE_MAX_OPEN_POSITIONS",
                                          "YES2RE_INITIAL_CAPITAL_USDC")
    assert _r_state.load_config("config/yes2re_reversal.json") == GOLDEN_CONFIG, \
        "os.environ must be back to normal after the patch"

    # (5b) the live instance aligns the ledger with the real balance: a *fresh* state seeds the
    #      ledger from cfg, so "current equity" starts from real money
    with contextlib.redirect_stderr(io.StringIO()):
        live_cfg = _r_state.load_config("config/yes2re_reversal.json",
                                        env={"YES2RE_MODE": "live",
                                             "YES2RE_INITIAL_CAPITAL_USDC": "51.713622"})
    assert live_cfg["paper_initial_capital_usdc"] == 51.713622, live_cfg["paper_initial_capital_usdc"]
    assert live_cfg["strategy"] == GOLDEN_STRATEGY, "capital override must not touch strategy"
    assert _r_state._blank_state(live_cfg)["paper_initial_capital_usdc"] == 51.713622
    assert _r_state._blank_state(GOLDEN_CONFIG)["paper_initial_capital_usdc"] == 600.0

    # (6) selecting live from the environment is announced on stderr (visible in the journal)
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        _r_state.load_config("config/yes2re_reversal.json", env={"YES2RE_MODE": "live"})
    assert "YES2RE_MODE=live" in captured.getvalue(), captured.getvalue()
    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        _r_state.load_config("config/yes2re_reversal.json", env={"YES2RE_MODE": "paper"})
    assert captured.getvalue() == "", captured.getvalue()


def test_env_override_live_still_needs_port_gates():
    """A live mode selected by env changes nothing about who may actually submit."""
    port_mod.reset_cache()
    cfg = {"mode": "live", "fire_budget_usdc": 12.0, "max_open_positions": 10}
    env = {"YES2RE_MODE": "live"}                      # no gate variables at all
    status = port_mod.port_status(cfg, env=env)
    assert status["ok"] is False and status["reason"] == submit.GATE_FLAG, status
    try:
        port_mod.get_port(cfg, env=env)
    except port_mod.PortRefused as exc:
        assert exc.reason == submit.GATE_FLAG, exc.reason
    else:
        raise AssertionError("live mode alone must not hand out a write-capable port")
    port_mod.reset_cache()


# --------------------------------------------------------------------------- helper

@contextlib.contextmanager
def _patched(module, **attributes):
    saved = {name: getattr(module, name) for name in attributes}
    for name, value in attributes.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


CHECKS = [
    ("config: env overrides (mode/budget/max_open)", test_load_config_env_overrides),
    ("config: live via env still needs port gates", test_env_override_live_still_needs_port_gates),
    ("port: selection matrix (no downgrade)", test_port_selection_matrix),
    ("port: missing v2 SDK ⇒ live_deps_missing (F3)", test_port_selection_matrix),
    ("paper: fire matches pre-port golden", test_paper_fire_matches_pre_port_golden),
    ("paper: port == matcher + ledger", test_paper_port_matches_matcher_and_ledger),
    ("paper: live without gates never falls back", test_paper_fire_refuses_when_live_has_no_gates),
    ("paper: stdlib run needs no v2 SDK", test_paper_engine_needs_no_v2_sdk),
    ("live: preflight honours real caps", test_live_port_preflight_uses_real_caps),
    ("live: match branches (partial/full/none/cancel/timeout)", test_live_port_match_branches),
    ("v2: clamp keeps orders passive", test_v2_clamp_limit),
    ("v2: execute_leg end to end (stub)", test_v2_execute_leg_end_to_end),
    ("v2: cancel retry + residual risk", test_v2_cancel_retry_and_residual_risk),
    ("v2: cancel shapes tolerated", test_v2_cancel_summary_tolerates_shapes),
    ("v2: poll_fill branches", test_v2_poll_fill_branches),
    ("v2: sentinel least privilege", test_v2_sentinels_least_privilege),
]


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - the runner reports everything
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {str(exc)[:400]}")
        else:
            print(f"PASS {name}")
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
