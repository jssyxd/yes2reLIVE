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
    # and a transport without the probe stays usable (backward compatible)
    port_mod.reset_cache()
    assert port_mod.get_port({"mode": "live"}, env=_live_env(), transport=object()).mode == "live"
    port_mod.reset_cache()
    assert live.describe()["mode"] == "live"
    port_mod.reset_cache()
    assert isinstance(port_mod.get_port({"mode": "paper"}, env={}), port_mod.PaperPort)


def test_port_missing_v2_sdk_fails_closed():
    """F3: a missing v2 SDK is reported as ``live_deps_missing`` — refuse, never fall back to paper."""
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
    """Five offline branches: partial fill, full fill, no fill, cancel failure, no preflight.

    A leg now only ever goes out as a FAK take (never passive): the leg carries its own cap and
    its book shows a resting ask, so the branches below exercise the real fill reconciliation.
    """
    book = {"best_ask": "0.60", "best_bid": "0.59", "tick_size": "0.01", "neg_risk": True}
    leg = {"leg": "buy_no_broken", "token_id": "TOK_NO", "side": "BUY", "cap": "0.65"}

    partial = _StubTransport(results=[{"ok": True, "status": "matched", "filled_shares": Decimal("6"),
                                       "avg_price": Decimal("0.60"), "cost": Decimal("3.6"),
                                       "unfilled": Decimal("4"), "limit_price": "0.59",
                                       "detail": "", "residual_risk": False}])
    port = _live_port(partial, account=ACCOUNT_OK, preflight=True)
    got = port.match(leg=leg, book=book, limit=Decimal("0.59"), shares=Decimal("10"))
    assert got["filled_shares"] == Decimal("6") and got["cost"] == Decimal("3.6"), got
    assert got["unfilled"] == Decimal("4") and got["source"] == "live", got
    kwargs = [call[1] for call in partial.calls if call[0] == "execute_leg"][0]
    assert kwargs["post_only"] is False and kwargs["clamp"] is False, kwargs
    assert kwargs["taker"] is True and kwargs["cap"] == Decimal("0.65"), kwargs
    assert kwargs["size"] == Decimal("10"), kwargs

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

    def create_market_order(self, args, options):
        """The market-order signing path the taker branch must use (never ``create_order``)."""
        self.calls.append(("create_market_order", args, options))
        return "SIGNED-MARKET-V2"

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

    class MarketOrderArgsV2:
        """Market-order args: ``amount`` (USDC for BUY / shares for SELL), not a size."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    MarketOrderArgs = MarketOrderArgsV2

    class PartialCreateOrderOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class OrderType:
        GTC = "GTC"
        FAK = "FAK"

    class ApiCreds:
        def __init__(self, *args):
            self.args = args

    class AssetType:
        COLLATERAL = "COLLATERAL"

    class BalanceAllowanceParams:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    mod.OrderArgs = OrderArgs
    mod.MarketOrderArgsV2 = MarketOrderArgsV2
    mod.MarketOrderArgs = MarketOrderArgs
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
    assert result["order_api"] == "limit" and result["market_amount"] is None, result
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
  "no_leg_enabled": false,
  "no_max_ask": "1.0",
  "no_notional_pct": 0.0,
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
  "yes_notional_pct": 1.0
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
 "no_leg_enabled": false,
 "no_max_ask": "1.0",
 "no_notional_pct": 0.0,
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
 "yes_notional_pct": 1.0
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


# ------------------------- LIVE take rule: leg-level, independent, NEVER passive (Phase 3c2)

#: the six boundary probes the operator-facing spec asks for (raw price string, is-taker)
BAND_PROBES = [("0.479", False), ("0.480", False), ("0.4801", True),
               ("0.8999", True), ("0.9000", True), ("0.9001", False)]

#: the ladder only ever emits ``send_fak`` when a resting ask exists, so most fixtures have one
TAKER_BOOK = {"best_ask": "0.62", "best_bid": "0.60", "tick_size": "0.01", "neg_risk": True}
NO_BOOK = {"best_ask": None, "best_bid": None, "tick_size": "0.01", "neg_risk": True}


def _yes_leg(px=None, *, cap="0.90"):
    """The YES leg the way the ladder intent hands it to ``match`` (explicit cap required)."""
    leg = {**FIRE["legs"][1], "cap": cap}
    if px is not None:
        leg["best_ask"] = str(px)
    return leg


def _no_leg(*, cap="0.65"):
    """The NO leg — judged independently, on its own cap (the shipped ``no_max_ask`` is 1.0)."""
    return {**FIRE["legs"][0], "cap": cap}


def _yes_fire(px, *, source="ladder", key="london|2026-09-10|high"):
    """A fire whose YES-leg price is reachable through one supported fire-level evidence source."""
    fire = copy.deepcopy(FIRE)
    fire["key"] = key
    fire["new_yes_token"] = "TOK_YES"
    if source == "ladder":
        fire["ladder"] = [{"leg": "buy_no_broken", "outcome": "NO", "best_ask": "0.60"},
                          {"leg": "buy_yes_new", "outcome": "YES", "best_ask": str(px)}]
    elif source == "yes_ask":
        fire["yes_ask"] = str(px)
    return fire


class _TakerStub(_StubTransport):
    """Stub transport + the read-only YES re-quote the port uses when the fire carries no price."""

    def __init__(self, *, results=None, yes_ask=None, book=None):
        super().__init__(results=results)
        self.yes_ask = yes_ask
        self.yes_book = book

    def refetch_book(self, client, token_id):
        self.calls.append(("refetch_book", str(token_id)))
        if self.yes_book is not None:
            return self.yes_book
        if self.yes_ask is None:
            return None
        return {"best_ask": str(self.yes_ask), "tick_size": "0.01"}


def _decide(px="0.83", *, cfg=None, source="ladder", fire=None, leg=None, book=TAKER_BOOK,
            cap="0.90", transport=None):
    """One pure ``fill_mode`` decision (no order is ever placed by this helper)."""
    port = _live_port(transport or _StubTransport(), account=ACCOUNT_OK, preflight=True)
    fire = fire if fire is not None else _yes_fire(px, source=source)
    target = leg if leg is not None else _yes_leg(px, cap=cap)
    return port.fill_mode(fire=fire, leg=target, cfg=cfg if cfg is not None else CFG,
                          book=book, client=object())


def _run(leg, *, fire=None, cfg=None, book=TAKER_BOOK, limit="0.83", transport=None):
    """One ``match`` call against a recording stub; returns (result, transport)."""
    t = transport or _StubTransport()
    port = _live_port(t, account=ACCOUNT_OK, preflight=True)
    got = port.match(leg=leg, book=book, limit=Decimal(limit), shares=Decimal("10"),
                     fire=fire, cfg=cfg if cfg is not None else CFG)
    return got, t


def _sent(t):
    """Every ``execute_leg`` keyword bundle the port actually handed to the transport."""
    return [call[1] for call in getattr(t, "calls", []) if call[0] == "execute_leg"]


def _taker_client(*, status="matched", matched="6", size="10", price="0.83", token="TOK_YES",
                  trades=None):
    """A scripted v2 client whose order ends in the requested state."""
    if trades is None:
        trades = ([{"orderID": "ORD-1", "size": matched, "price": price}]
                  if Decimal(matched) > 0 else [])
    return _StubV2Client(order_states=[{"status": status, "size_matched": matched,
                                        "original_size": size, "price": price,
                                        "asset_id": token}], trades=trades)


def test_live_taker_yes_band_matrix():
    """``(0.48, 0.90]`` half-open on the YES leg's own price — helper *and* end to end."""
    lo, hi = port_mod.DEFAULT_YES_MIN_ASK, port_mod.DEFAULT_YES_MAX_ASK
    assert (lo, hi) == (Decimal("0.48"), Decimal("0.90"))
    assert port_mod.GATE_YES_BAND == "yes_band" and port_mod.TAKER == "taker"
    assert port_mod.SKIP == "skip" and port_mod.MAKER == "maker"
    for raw, want in BAND_PROBES:
        # (1) the pure half-open predicate, for every numeric shape the leg may carry
        for probe in (raw, Decimal(raw), float(raw)):
            assert port_mod.in_yes_band(probe, lo, hi) is want, (probe, want)
        # (2) end to end on the YES leg: in band ⇒ one FAK; out of band ⇒ NOTHING goes out
        got, t = _run(_yes_leg(raw))
        assert got["order_mode"] == ("taker" if want else "skip"), (raw, got)
        assert got["yes_price"] == Decimal(raw), (raw, got)
        calls = _sent(t)
        if want:
            assert len(calls) == 1, (raw, calls)
            assert calls[0]["taker"] is True and calls[0]["post_only"] is False, calls
            assert calls[0]["clamp"] is False, calls
        else:
            assert calls == [], (raw, t.calls)
            assert got["filled_shares"] == ZERO and got["unfilled"] == Decimal("10"), (raw, got)
    # the two endpoints, explicitly: 0.48 itself is excluded, 0.90 itself is included
    assert port_mod.in_yes_band("0.480000", lo, hi) is False
    assert port_mod.in_yes_band("0.900000", lo, hi) is True
    # (3) the operator's stub-count matrix, on the exact prices of the instruction: the leg-level
    #     call count alone must show it — 0 orders out of band, exactly 1 in band, and a
    #     ``post_only=True`` submission may never appear anywhere.  (``taker=True`` ⇒ FAK is then
    #     proven at the transport level by ``test_live_taker_order_construction``: post_order gets
    #     ``OrderType.FAK`` with ``post_only=False``.)
    for raw in ("0.40", "0.479", "0.48", "0.9001", "0.99"):
        got, t = _run(_yes_leg(raw))
        assert got["order_mode"] == "skip" and _sent(t) == [], (raw, got, t.calls)
        assert not [c for c in _sent(t) if c.get("post_only") is True], (raw, t.calls)
    for raw in ("0.4801", "0.60", "0.8999", "0.90"):
        got, t = _run(_yes_leg(raw))
        calls = _sent(t)
        assert got["order_mode"] == "taker" and len(calls) == 1, (raw, got, t.calls)
        assert calls[0]["taker"] is True and calls[0]["post_only"] is False, (raw, calls)
        assert calls[0]["clamp"] is False, (raw, calls)
    # garbage / out-of-range prices can never take
    for bad in (None, "", "abc", "0", "-0.5", "1.5", "NaN", "inf"):
        assert port_mod.in_yes_band(bad, lo, hi) is False, bad


def test_live_leg_scope_is_independent():
    """The legs are judged independently — a YES band never decides the NO leg's fate.

    YES leg: inside the band ⇒ FAK; **outside the band ⇒ no order at all** (never a resting
    ``post_only`` order — that is the whole point of the corrected rule).
    NO leg: takes whenever its own book shows an ask, regardless of the YES band, and is
    ``no_book``-skipped otherwise.
    """
    # (a) YES leg in band ⇒ exactly one FAK take
    in_band, t_in = _run(_yes_leg("0.83"))
    assert in_band["order_mode"] == "taker" and len(_sent(t_in)) == 1, in_band
    assert _sent(t_in)[0]["audit_extra"]["taker_gate"] == "yes_band", _sent(t_in)[0]
    # (b) the operator's fake-breakout cases (and the other side of the band): zero orders
    for px in ("0.40", "0.39", "0.42", "0.48", "0.4799", "0.95", "0.99", "1.0"):
        got, t = _run(_yes_leg(px))
        expect = "yes_price_below_band" if Decimal(px) <= Decimal("0.48") else "yes_price_above_band"
        assert got["status"] == expect, (px, got)
        assert got["order_mode"] == "skip" and _sent(t) == [], (px, t.calls)
        assert got["filled_shares"] == ZERO and got["fill_and_kill"] is False, (px, got)
    # (c) the NO leg ignores the band in both directions: with an ask it takes on its own cap
    for yes_px in ("0.40", "0.83", "0.99"):
        got, t = _run(_no_leg(), fire=_yes_fire(yes_px))
        call = _sent(t)
        assert got["order_mode"] == "taker" and len(call) == 1, (yes_px, got)
        assert call[0]["taker"] is True and call[0]["post_only"] is False, call
        assert call[0]["cap"] == Decimal("0.65"), call
        assert call[0]["audit_extra"]["taker_gate"] == "no_leg_ask", call
        assert got["yes_price"] is None, (yes_px, got)
    # (d) ... and with no resting ask the NO leg is a no_book skip — still nothing passive
    got, t = _run(_no_leg(), book=NO_BOOK)
    assert got["status"] == "no_book" and got["order_mode"] == "skip", got
    assert _sent(t) == [], t.calls
    # (e) the shipped NO cap (``no_max_ask = 1.0``) is still a take: only the absolute
    #     ``<= 1`` limit binds (documented inconsistency with AGENTS.md's 0.65 — kept as-is)
    got, t = _run(_no_leg(cap="1.0"), limit="0.99")
    assert got["order_mode"] == "taker" and _sent(t)[0]["cap"] == Decimal("1.0"), got


def test_live_no_passive_fallback_anywhere():
    """Every non-take cause drops the leg: no order, no ``post_only``, status names the cause."""
    cases = [
        ("taker_cap_missing", _yes_leg("0.83", cap=None)),
        ("taker_cap_missing", {k: v for k, v in _yes_leg("0.83").items() if k != "cap"}),
        ("taker_cap_missing", _yes_leg("0.83", cap="1.5")),
        ("taker_cap_missing", _no_leg(cap="0")),
        ("yes_price_unknown", _yes_leg(None)),
    ]
    for want_status, leg in cases:
        got, t = _run(leg, fire=copy.deepcopy(FIRE))
        assert got["status"] == want_status, (want_status, got)
        assert got["order_mode"] == "skip" and got["filled_shares"] == ZERO, (want_status, got)
        assert got["unfilled"] == Decimal("10"), (want_status, got)
        assert _sent(t) == [], (want_status, t.calls)
    # the YES leg with no resting ask is a no_book skip, not a resting order
    got, t = _run(_yes_leg("0.83"), book=NO_BOOK)
    assert got["status"] == "no_book" and _sent(t) == [], got
    # an illegal band refuses every leg (a corrupt config stands the whole fire down)
    for bad in ({"yes_max_ask": "abc"}, {"yes_min_ask": "0"},
                {"yes_min_ask": "0.95", "yes_max_ask": "0.90"},
                {"strategy": {"yes_min_ask": "NaN", "yes_max_ask": "0.9"}},
                {"strategy": {"yes_min_ask": "0.48", "yes_max_ask": "1.5"}}):
        for leg in (_yes_leg("0.83"), _no_leg()):
            d = _decide("0.83", cfg=bad, leg=leg)
            assert d["taker"] is False and d["reason"] == "yes_band_unparsed", (bad, leg["leg"], d)
        got, t = _run(_yes_leg("0.83"), cfg=bad)
        assert got["status"] == "yes_band_unparsed" and _sent(t) == [], (bad, got)
    # the port advertises the rule it implements: no passive mode exists on the trading path
    port = _live_port(_StubTransport(), account=ACCOUNT_OK, preflight=True)
    described = port.describe()
    assert described["passive_fallback"] is False, described
    assert described["taker_scope"] == "leg_level_independent", described
    assert "maker" not in described["order_modes"], described
    assert described["taker_gates"] == ["yes_band", "no_leg_ask"], described


def test_live_taker_cap_required_and_bounded():
    """L-2: any take needs an explicit ``0 < cap <= 1``; missing/junk/``> 1`` refuses at BOTH layers."""
    # (a) port layer: an unusable cap ⇒ skip, never an uncapped aggressor
    for cap in (None, "", "abc", "0", "-0.5", "NaN", "inf", "1.5", "2", True):
        leg = {k: v for k, v in _yes_leg("0.83").items() if k != "cap"}
        if cap is not None:
            leg["cap"] = cap
        d = _decide("0.83", leg=leg)
        assert d["taker"] is False and d["reason"] == "taker_cap_missing", (cap, d)
        assert d["order_mode"] == "skip" and d["cap"] is None, (cap, d)
    # ... while cap == 1.0 (the shipped ``no_max_ask``) and any sane cap still take in band
    for cap in ("1.0", "0.90", "0.65", "0.4801", "0.0001"):
        d = _decide("0.83", leg=_yes_leg("0.83", cap=cap))
        assert d["taker"] is True and d["cap"] == Decimal(cap), (cap, d)
    # (b) transport layer (last line of defence, independent of the caller)
    for bad_cap in (None, "", "abc", "0", "-0.5", "1.5", "2"):
        client = _StubV2Client()
        with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
            log = Path(tmp) / "a.jsonl"
            out = v2_transport.execute_leg(client, token_id="TOK_YES", side="BUY", price="0.83",
                                           size="10", book=TAKER_BOOK, gates=GATES_OK, taker=True,
                                           cap=bad_cap, audit_path=log, sleep=lambda _s: None)
            lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert out["ok"] is False and out["status"] == "taker_cap_required", (bad_cap, out)
        assert client.calls == [], (bad_cap, client.calls)
        assert [line["reason"] for line in lines] == ["taker_cap_required"], (bad_cap, lines)
    # (c) cap == 1.0 is accepted; the absolute ``<= 1`` limit is what binds
    client = _taker_client(matched="10", price="0.99")
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        out = v2_transport.execute_leg(client, token_id="TOK_NO", side="BUY", price="0.99",
                                       size="10", book=TAKER_BOOK, gates=GATES_OK, taker=True,
                                       cap="1.0", audit_path=Path(tmp) / "a.jsonl",
                                       sleep=lambda _s: None, poll_attempts=2)
    assert out["ok"] is True and out["limit_price"] == "0.99", out
    assert out["order_mode"] == "taker" and out["order_type"] == "FAK", out
    assert [c[3] for c in client.calls if c[0] == "post_order"] == [False], client.calls


def test_live_ask_quote_out_of_range_is_not_no_book():
    """Audit LOW (2026-09-12): a quote that is *present but unusable* is ``ask_out_of_range``.

    An ask of ``> 1`` / ``<= 0`` / non-numeric used to fall through the same ``None`` as a genuinely
    empty book and was reported as ``no_book`` — which points the operator at a missing feed when
    the real problem is a corrupt one.  The two cases are now distinct reasons (both still send
    **no order at all**), while ``best_ask_of`` keeps its historical ``Decimal | None`` contract.
    """
    assert port_mod.FILL_REFUSE_ASK_BAD == "ask_out_of_range"
    # (a) the pure classifier: empty book vs. unusable quote
    for empty in ({}, {"best_ask": None}, {"best_ask": ""}, {"best_ask": "   "}, None):
        state = port_mod.ask_state_of(empty)
        assert state["present"] is False and state["ask"] is None, (empty, state)
    for bad in ("0", "-0.5", "1.5", "1.0001", "2", "abc", "NaN", "inf", True, {}):
        state = port_mod.ask_state_of({"best_ask": bad})
        assert state["present"] is True and state["ask"] is None, (bad, state)
        assert state["raw"] == bad, (bad, state)
    good = port_mod.ask_state_of({"best_ask": "0.83"})
    assert good == {"present": True, "ask": Decimal("0.83"), "raw": "0.83"}, good
    # (b) ``best_ask_of`` is unchanged (the contract callers/tests already rely on)
    assert port_mod.best_ask_of({"best_ask": "0.61"}) == Decimal("0.61")
    assert port_mod.best_ask_of({"best_ask": "1.5"}) is None
    assert port_mod.best_ask_of({"best_ask": None}) is None and port_mod.best_ask_of(None) is None
    # (c) end to end: the bad quote is named, nothing is sent, and it is *not* mislabelled no_book
    bad_book = {"best_ask": "1.5", "best_bid": "0.01", "tick_size": "0.01", "neg_risk": True}
    for leg in (_no_leg(), _yes_leg("0.83")):
        got, t = _run(leg, book=bad_book)
        assert got["status"] == "ask_out_of_range", (leg["leg"], got)
        assert got["order_mode"] == "skip" and got["filled_shares"] == ZERO, (leg["leg"], got)
        assert got["unfilled"] == Decimal("10") and got["fill_and_kill"] is False, (leg["leg"], got)
        assert _sent(t) == [], t.calls
    # (d) a genuinely empty book is still ``no_book`` — the two reasons never collapse again
    got, t = _run(_no_leg(), book=NO_BOOK)
    assert got["status"] == "no_book" and _sent(t) == [], got


def test_live_taker_price_bounds():
    """L-3: the taker path refuses a limit ``<= 0``, ``> cap`` or ``> 1`` — it never re-prices."""
    def _send(**kw):
        client = _taker_client()
        with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
            log = Path(tmp) / "a.jsonl"
            log.write_text("", encoding="utf-8")     # a refusal may legitimately write nothing
            out = v2_transport.execute_leg(client, token_id="TOK_YES", side="BUY", size="10",
                                           book=TAKER_BOOK, gates=GATES_OK, taker=True,
                                           audit_path=log, sleep=lambda _s: None, poll_attempts=2,
                                           **kw)
            lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        return out, client, lines

    out, client, lines = _send(price="1.5", cap="0.9")
    assert out["status"] == "price_out_of_range" and client.calls == [], out
    assert [line["reason"] for line in lines] == ["price_out_of_range"], lines
    out, client, _ = _send(price="0", cap="0.9")
    assert out["status"] == "invalid_input" and client.calls == [], out
    out, client, _ = _send(price="-0.5", cap="0.9")
    assert out["status"] == "invalid_input" and client.calls == [], out
    out, client, lines = _send(price="0.95", cap="0.9")
    assert out["status"] == "above_cap" and client.calls == [], out
    assert [line["reason"] for line in lines] == ["above_cap"], lines
    out, client, lines = _send(price="0.835", cap="0.9")
    assert out["status"] == "price_not_on_tick" and client.calls == [], out
    assert [line["reason"] for line in lines] == ["price_not_on_tick"], lines
    # the boundary values themselves are legal and go out unclamped
    for price in ("0.90", "1.0"):
        out, client, _ = _send(price=price, cap="1.0")
        assert out["ok"] is True and out["limit_price"] == price, (price, out)
        assert out["clamped"] is False and out["order_mode"] == "taker", out
        assert [c for c in client.calls if c[0] == "post_order"], client.calls


def test_live_taker_evidence_chain():
    """The YES leg's own evidence: leg ask → fire ladder → fire field → one read-only re-quote."""
    # (a) the ladder intent being matched is the everyday live case
    got = _decide("0.83")
    assert got["taker"] is True and got["source"] == "leg_best_ask", got
    # (b) the fire's own ladder row
    got = _decide(None, fire=_yes_fire("0.83"), leg=_yes_leg())
    assert got["taker"] is True and got["source"] == "fire_ladder", got
    # (c) an explicit fire field
    got = _decide(None, fire=_yes_fire("0.83", source="yes_ask"), leg=_yes_leg())
    assert got["taker"] is True and got["source"] == "fire_yes_ask", got
    # (d) ONE read-only re-quote per fire serves every later rung (memoised), never a write
    stub = _TakerStub(yes_ask="0.83")
    port = _live_port(stub, account=ACCOUNT_OK, preflight=True)
    fire = copy.deepcopy(FIRE)                     # no ladder, no field: the real fire shape
    leg = _yes_leg()                               # ... and no leg price either
    first = port.fill_mode(fire=fire, leg=leg, cfg=CFG, book=TAKER_BOOK, client=object())
    second = port.fill_mode(fire=fire, leg=leg, cfg=CFG, book=TAKER_BOOK, client=object())
    assert first["taker"] is True and first["source"] == "refetch_book_yes_leg", first
    assert second["taker"] is True and second["source"].endswith("_memo"), second
    assert [c for c in stub.calls if c[0] == "refetch_book"] == [("refetch_book", "TOK_YES")], stub.calls
    # ``build_client`` belongs to preflight; the point here is that no ORDER was ever placed
    assert not [c for c in stub.calls if c[0] == "execute_leg"], stub.calls
    nxt = port.fill_mode(fire=_yes_fire("0.95", key="other|2026-09-10|high"), leg=_yes_leg(),
                         cfg=CFG, book=TAKER_BOOK, client=object())
    assert nxt["taker"] is False and nxt["reason"] == "yes_price_above_band", nxt
    assert len([c for c in stub.calls if c[0] == "refetch_book"]) == 1, stub.calls
    # (e) a failed / empty re-quote only drops the leg (fail-closed, no crash)
    broken = _TakerStub(yes_ask=None)
    port = _live_port(broken, account=ACCOUNT_OK, preflight=True)
    failed = port.fill_mode(fire=copy.deepcopy(FIRE), leg=_yes_leg(), cfg=CFG,
                            book=TAKER_BOOK, client=object())
    assert failed["taker"] is False and failed["reason"] == "yes_price_unknown", failed
    # (f) a transport without the read-only hook is equally fail-closed
    port = _live_port(_StubTransport(), account=ACCOUNT_OK, preflight=True)
    nohook = port.fill_mode(fire=copy.deepcopy(FIRE), leg=_yes_leg(), cfg=CFG,
                            book=TAKER_BOOK, client=object())
    assert nohook["taker"] is False and nohook["reason"] == "yes_price_unknown", nohook


def test_live_taker_order_construction():
    """Taker: post_only=False, OrderType.FAK, price NOT pressed down, cap still binding."""
    client = _taker_client(matched="6", price="0.83")
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        result = v2_transport.execute_leg(
            client, token_id="TOK_YES", side="BUY", price="0.83", size="10",
            book={"best_ask": "0.83", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, taker=True, cap="0.90", sleep=lambda _s: None, poll_attempts=2,
            audit_path=log, audit_extra={"taker_gate": "yes_band", "yes_price": "0.83"})
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert result["ok"] is True and result["order_mode"] == "taker", result
    assert result["order_type"] == "FAK" and result["fill_and_kill"] is True, result
    assert result["limit_price"] == "0.83" and result["clamped"] is False, result
    assert result["filled_shares"] == Decimal("6") and result["cost"] == Decimal("4.98"), result
    assert result["avg_price"] == Decimal("0.83") and result["unfilled"] == Decimal("4"), result
    assert result["voided_shares"] == Decimal("4"), result
    posts = [call for call in client.calls if call[0] == "post_order"]
    assert len(posts) == 1, client.calls
    assert posts[0][2] == "FAK" and posts[0][3] is False, posts[0]
    # the taker is signed through the **market** API (never through ``create_order``, whose maker
    # amount the venue rejects for a market order as ``400 invalid amounts``)
    assert client.calls[0][0] == "create_market_order", client.calls
    assert "create_order" not in [call[0] for call in client.calls], client.calls
    assert posts[0][1] == "SIGNED-MARKET-V2", posts[0]
    args = client.calls[0][1]
    assert Decimal(str(args.price)) == Decimal("0.83"), vars(args)
    # 10 shares @ 0.83 = 8.30 USDC — a BUY market order carries USDC, not a share count
    assert Decimal(str(args.amount)) == Decimal("8.30"), vars(args)
    assert not hasattr(args, "size"), vars(args)
    assert result["order_api"] == "market" and result["market_amount"] == "8.30", result
    assert result["amount_unit"] == "USDC", result
    # FAK is terminal: the remainder is voided, no cancel (and no false residual risk)
    assert not [call for call in client.calls if call[0] == "cancel_orders"], client.calls
    assert result["residual_risk"] is False, result
    intent = lines[0]
    assert intent["action"] == "intent" and intent["params"]["order_mode"] == "taker", intent
    assert intent["params"]["order_type"] == "FAK" and intent["params"]["post_only"] is False, intent
    assert intent["params"]["order_api"] == "market", intent
    assert intent["params"]["amount"] == "8.30" and intent["params"]["amount_unit"] == "USDC", intent
    assert intent["params"]["taker_gate"] == "yes_band", intent
    assert intent["params"]["yes_price"] == "0.83", intent

    # a misaligned price is refused rather than silently moved (tick alignment is a red line)
    misaligned = _StubV2Client()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        off = v2_transport.execute_leg(misaligned, token_id="TOK_YES", side="BUY", price="0.835",
                                       size="10",
                                       book={"best_ask": "0.83", "tick_size": "0.01"},
                                       gates=GATES_OK, taker=True, cap="0.90", audit_path=log,
                                       sleep=lambda _s: None)
    assert off["ok"] is False and off["status"] == "price_not_on_tick", off
    assert misaligned.calls == [], misaligned.calls

    # an SDK build without FAK must refuse — never fall back to a resting GTC
    no_fak = _StubV2Client()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        import py_clob_client_v2.clob_types as _types          # the stubbed module
        saved = _types.OrderType
        try:
            class _NoFak:
                GTC = "GTC"

            _types.OrderType = _NoFak
            log = Path(tmp) / "audit.jsonl"
            refused = v2_transport.execute_leg(no_fak, token_id="TOK_YES", side="BUY", price="0.83",
                                               size="10", book=TAKER_BOOK, gates=GATES_OK, taker=True,
                                               cap="0.90", audit_path=log, sleep=lambda _s: None)
            lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        finally:
            _types.OrderType = saved
    assert refused["ok"] is False and refused["status"] == "no_fak_order_type", refused
    assert no_fak.calls == [], no_fak.calls
    assert [line["reason"] for line in lines] == ["no_fak_order_type"], lines


def test_live_taker_market_amount_precision():
    """The 400 that broke the live taker (2026-09-12): market-order ``amount`` precision.

    Real failure — BUY 8.19 shares @ 0.61 went out through the *limit* shape with maker amount
    ``4.9959`` USDC and the venue answered: ``invalid amounts, the market buy orders maker amount
    supports a max accuracy of 2 decimals, taker amount a max of 4 decimals``.  The taker is now
    signed through ``create_market_order`` with a floored ``amount``:

    * BUY  ⇒ USDC nominal, **≤ 2 decimals** and never above the leg's own notional;
    * SELL ⇒ share count, **≤ 4 decimals** and never above the position size;
    * both ⇒ ``post_order(signed, FAK, post_only=False)`` — and ``create_order`` never called.
    """
    def _places(dec: Decimal) -> int:
        return -dec.as_tuple().exponent          # decimals actually carried by the amount

    # (a) the pure helper, on the exact numbers of the failed live fire
    real = v2_transport.market_amount(side="BUY", limit="0.61", shares="8.19")
    assert real == Decimal("4.99"), real
    assert _places(real) <= 2 and real < Decimal("8.19") * Decimal("0.61"), real
    assert v2_transport.market_amount(side="SELL", limit="0.61", shares="12.345678") == \
        Decimal("12.3456")
    assert v2_transport.market_amount(side="HOLD", limit="0.61", shares="1") is None
    assert v2_transport.market_amount(side="BUY", limit="0", shares="1") is None
    assert v2_transport.market_amount(side="BUY", limit="abc", shares="1") is None

    # (b) BUY through execute_leg: amount is USDC, ≤ 2 dp, ≤ notional
    buy = _taker_client(matched="8.18", price="0.61")
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        out = v2_transport.execute_leg(
            buy, token_id="TOK_YES", side="BUY", price="0.61", size="8.19",
            book={"best_ask": "0.61", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, taker=True, cap="0.90", poll_attempts=2, sleep=lambda _s: None,
            audit_path=log)
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    signed = [call for call in buy.calls if call[0] == "create_market_order"]
    assert len(signed) == 1, buy.calls
    args = signed[0][1]
    assert args.side == "BUY" and str(args.order_type) == "FAK", vars(args)
    assert Decimal(str(args.price)) == Decimal("0.61"), vars(args)
    amount = Decimal(str(args.amount))
    assert amount == Decimal("4.99") and _places(amount) <= 2, vars(args)
    assert amount <= Decimal("8.19") * Decimal("0.61"), amount
    assert not hasattr(args, "size"), vars(args)
    assert [call[2:] for call in buy.calls if call[0] == "post_order"] == [("FAK", False)], buy.calls
    assert "create_order" not in [call[0] for call in buy.calls], buy.calls
    assert out["order_api"] == "market" and out["market_amount"] == "4.99", out
    assert out["amount_unit"] == "USDC", out
    intent = lines[0]["params"]
    assert intent["order_api"] == "market" and intent["amount"] == "4.99", intent
    assert intent["amount_unit"] == "USDC" and intent["order_type"] == "FAK", intent
    assert intent["post_only"] is False, intent

    # (c) SELL through execute_leg: amount is a share count, ≤ 4 dp, ≤ the size asked for
    sell = _taker_client(matched="12.3456", price="0.61")
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        out_sell = v2_transport.execute_leg(
            sell, token_id="TOK_YES", side="SELL", price="0.61", size="12.345678",
            book={"best_bid": "0.61", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, taker=True, cap="0.90", poll_attempts=2, sleep=lambda _s: None,
            audit_path=log)
        sell_lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
    assert out_sell["ok"] is True, out_sell
    s_args = [call for call in sell.calls if call[0] == "create_market_order"][0][1]
    s_amount = Decimal(str(s_args.amount))
    assert s_args.side == "SELL", vars(s_args)
    assert s_amount == Decimal("12.3456") and _places(s_amount) <= 4, vars(s_args)
    assert s_amount <= Decimal("12.345678"), s_amount
    assert [call[2:] for call in sell.calls if call[0] == "post_order"] == [("FAK", False)], sell.calls
    assert "create_order" not in [call[0] for call in sell.calls], sell.calls
    assert out_sell["market_amount"] == "12.3456" and out_sell["amount_unit"] == "shares", out_sell
    assert sell_lines[0]["params"]["amount_unit"] == "shares", sell_lines[0]

    # (d) an amount that floors to 0 is refused with nothing signed and nothing sent
    tiny = _taker_client()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        refused = v2_transport.execute_leg(
            tiny, token_id="TOK_YES", side="BUY", price="0.01", size="0.004",
            book={"best_ask": "0.01", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, taker=True, cap="0.90", poll_attempts=2, sleep=lambda _s: None,
            audit_path=log)
        refused_lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                         if line.strip()]
    assert refused["ok"] is False and refused["status"] == "amount_below_precision", refused
    assert tiny.calls == [], tiny.calls
    assert [line["reason"] for line in refused_lines] == ["amount_below_precision"], refused_lines

    # (e) fail closed (mutation check): no market API ⇒ refuse, never fall back to the limit shape
    class _NoMarketApi(_StubV2Client):
        create_market_order = None

    no_api = _NoMarketApi()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        no_api_out = v2_transport.execute_leg(
            no_api, token_id="TOK_YES", side="BUY", price="0.61", size="8.19",
            book={"best_ask": "0.61", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, taker=True, cap="0.90", poll_attempts=2, sleep=lambda _s: None,
            audit_path=log)
        no_api_lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
    assert no_api_out["ok"] is False and no_api_out["status"] == "no_market_order_api", no_api_out
    assert no_api.calls == [], no_api.calls
    assert [line["reason"] for line in no_api_lines] == ["no_market_order_api"], no_api_lines

    # ... and the same when the SDK build has no ``MarketOrderArgs`` at all
    no_args = _taker_client(matched="8.18", price="0.61")
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        import py_clob_client_v2.clob_types as _types
        saved_types = (_types.MarketOrderArgsV2, _types.MarketOrderArgs)
        log = Path(tmp) / "audit.jsonl"
        try:
            _types.MarketOrderArgsV2 = None
            _types.MarketOrderArgs = None
            no_args_out = v2_transport.execute_leg(
                no_args, token_id="TOK_YES", side="BUY", price="0.61", size="8.19",
                book={"best_ask": "0.61", "tick_size": "0.01", "neg_risk": True},
                gates=GATES_OK, taker=True, cap="0.90", poll_attempts=2, sleep=lambda _s: None,
                audit_path=log)
        finally:
            _types.MarketOrderArgsV2, _types.MarketOrderArgs = saved_types
    assert no_args_out["status"] == "no_market_order_api", no_args_out
    assert no_args.calls == [], no_args.calls
    # ... while the maker path is untouched: it still signs through ``create_order``
    maker = _StubV2Client()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        made = v2_transport.execute_leg(
            maker, token_id="TOK_NO", side="BUY", price="0.599", size="10",
            book={"best_ask": "0.60", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, sleep=lambda _s: None, poll_attempts=2,
            audit_path=Path(tmp) / "a.jsonl")
    kinds = [call[0] for call in maker.calls]
    assert made["ok"] is True and kinds.count("create_order") == 1, maker.calls
    assert "create_market_order" not in kinds, maker.calls
    assert made["order_api"] == "limit" and made["market_amount"] is None, made


def test_live_taker_fill_accounting():
    """Partial and zero-fill FAK replies book exactly what the venue reported."""
    partial = _taker_client(matched="6", price="0.83",
                            trades=[{"orderID": "ORD-1", "size": "4", "price": "0.83"},
                                    {"orderID": "ORD-1", "size": "2", "price": "0.83"}])
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        part = v2_transport.execute_leg(partial, token_id="TOK_YES", side="BUY", price="0.83",
                                        size="10", book=TAKER_BOOK, gates=GATES_OK, taker=True,
                                        cap="0.90", audit_path=Path(tmp) / "a.jsonl",
                                        sleep=lambda _s: None, poll_attempts=2)
    assert part["filled_shares"] == Decimal("6") and part["unfilled"] == Decimal("4"), part
    assert part["voided_shares"] == Decimal("4") and part["cost"] == Decimal("4.98"), part
    assert part["residual_risk"] is False and not [c for c in partial.calls if c[0] == "cancel_orders"]

    killed = _taker_client(status="cancelled", matched="0", price="0.83")
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        none_filled = v2_transport.execute_leg(killed, token_id="TOK_YES", side="BUY", price="0.83",
                                               size="10", book=TAKER_BOOK, gates=GATES_OK, taker=True,
                                               cap="0.90", audit_path=Path(tmp) / "a.jsonl",
                                               sleep=lambda _s: None, poll_attempts=2)
    assert none_filled["ok"] is True and none_filled["status"] == "cancelled", none_filled
    assert none_filled["filled_shares"] == ZERO and none_filled["cost"] == ZERO, none_filled
    # nb: ``poll_fill`` reports the order's own price as ``avg_price`` when there is no trade at
    # all (pre-existing behaviour, untouched); with zero filled shares the cost stays 0
    assert none_filled["avg_price"] == Decimal("0.83"), none_filled
    assert none_filled["unfilled"] == Decimal("10"), none_filled
    assert none_filled["voided_shares"] == Decimal("10"), none_filled
    assert none_filled["residual_risk"] is False, none_filled
    assert not [c for c in killed.calls if c[0] == "cancel_orders"], killed.calls

    # ... and the port reports the same numbers to the engine ladder (remaining stays 10 ⇒ that
    # leg books no position, exactly like paper's no-fill)
    transport = _TakerStub(results=[dict(none_filled)])
    port = _live_port(transport, account=ACCOUNT_OK, preflight=True)
    via_port = port.match(leg=_yes_leg("0.83"), book=TAKER_BOOK, limit=Decimal("0.83"),
                          shares=Decimal("10"), fire=_yes_fire("0.83"), cfg=CFG)
    assert via_port["filled_shares"] == ZERO and via_port["unfilled"] == Decimal("10"), via_port
    assert via_port["cost"] == ZERO and via_port["order_mode"] == "taker", via_port


def test_live_taker_fak_no_match_clean_cancellation():
    """When Polymarket kills an FAK order with no match, status is cancelled and residual_risk is False."""
    class _FakKilledClient(_StubV2Client):
        def post_order(self, signed, order_type, post_only=False):
            self.calls.append(("post_order", signed, str(order_type), post_only))
            exc = Exception("400 Bad Request")
            exc.error_msg = {
                "error": "no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.",
                "orderID": "0xdeadbeef1234"
            }
            raise exc

    client = _FakKilledClient()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        result = v2_transport.execute_leg(
            client, token_id="TOK_YES", side="BUY", price="0.83", size="10",
            book={"best_ask": "0.83", "tick_size": "0.01", "neg_risk": True},
            gates=GATES_OK, taker=True, cap="0.90", sleep=lambda _s: None,
            audit_path=log, audit_extra={"taker_gate": "yes_band", "yes_price": "0.83"})
    assert result["ok"] is True and result["order_id"] == "0xdeadbeef1234", result
    assert result["status"] == "cancelled" and result["filled_shares"] == ZERO, result
    assert result["unfilled"] == Decimal("10") and result["voided_shares"] == Decimal("10"), result
    assert result["residual_risk"] is False and result["fill_and_kill"] is True, result


def test_live_taker_gates_not_bypassable():
    """A band-eligible take is impossible without the gates *and* without a live preflight."""
    client = _StubV2Client()
    with tempfile.TemporaryDirectory() as tmp, _stub_v2_lib():
        log = Path(tmp) / "audit.jsonl"
        for bad in (None, {}, {"ok": True}, {"ok": True, "checks": {"cli_flag": True}},
                    {"ok": True, "checks": {"cli_flag": True, "env_flag": True,
                                            "confirm_phrase": False}}):
            try:
                v2_transport.execute_leg(client, token_id="TOK_YES", side="BUY", price="0.83",
                                         size="10", book=TAKER_BOOK, gates=bad, taker=True,
                                         cap="0.90", audit_path=log, sleep=lambda _s: None)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"taker with gates={bad!r} must be refused")
        assert client.calls == [], f"nothing signed or sent: {client.calls}"
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [line["reason"] for line in lines] == ["gates_missing"] * 5, lines

    # no preflight ⇒ no order, even with the YES leg inside the band
    port = _live_port(_StubTransport(), account=ACCOUNT_OK)
    blocked = port.match(leg=_yes_leg("0.83"), book=TAKER_BOOK, limit=Decimal("0.83"),
                         shares=Decimal("10"), fire=_yes_fire("0.83"), cfg=CFG)
    assert blocked["status"] == "live_not_preflighted" and blocked["filled_shares"] == ZERO, blocked

    # a *denied* preflight leaves no client behind, so a later match cannot slip through
    transport = _StubTransport()
    port = _live_port(transport, account={"usdc_balance": 1.0, "open_orders": 0, "positions": [],
                                          "positions_value_usdc": 0.0})
    pre = port.preflight(fire={**FIRE, "budget_usdc": "12"}, cfg={"fire_budget_usdc": 12})
    assert pre["ok"] is False and pre["reason"] == "risk_gate:budget_exceeds_balance", pre
    after = port.match(leg=_yes_leg("0.83"), book=TAKER_BOOK, limit=Decimal("0.83"),
                       shares=Decimal("10"), fire=_yes_fire("0.83"), cfg=CFG)
    assert after["status"] == "live_not_preflighted", after
    assert not [call for call in transport.calls if call[0] == "execute_leg"], transport.calls


def test_live_taker_limits_and_band_fail_closed():
    """check_limits/risk_gate still gate the fire; an unusable band refuses instead of resting."""
    # (a) the cumulative / per-fire caps are the pre-existing ones — the band cannot widen them
    transport = _StubTransport()
    port = _live_port(transport, account={"usdc_balance": 51.0, "open_orders": 0, "positions": [],
                                          "positions_value_usdc": 0.0},
                      limits={"fire_budget_usdc": "12", "max_open_positions": "10",
                              "max_capital_usdc": "50"})
    over_budget = port.preflight(fire={**FIRE, "budget_usdc": "13"}, cfg={"fire_budget_usdc": 13})
    assert over_budget["ok"] is False and over_budget["reason"] == "limits:notional_exceeds_fire_budget", over_budget
    crowded = port.preflight(fire={**FIRE, "budget_usdc": "12"},
                             cfg={"fire_budget_usdc": 12})
    assert crowded["ok"] is True, crowded
    assert submit.check_limits(notional_usdc="12", fire_budget_usdc="12", committed_usdc="45",
                               max_capital_usdc="50")["reason"] == submit.LIMIT_CAPITAL_CAP
    # the guard is the shared function — untouched by the take rule
    assert v2_transport.submit.check_limits is submit.check_limits

    # (b) the band comes from cfg (flat or strategy) and defaults to 0.48/0.90
    default_band = port_mod.yes_price_band({})
    assert default_band["ok"] and (default_band["lo"], default_band["hi"]) == (Decimal("0.48"), Decimal("0.90"))
    nested = port_mod.yes_price_band({"strategy": {"yes_min_ask": "0.50", "yes_max_ask": "0.99"}})
    assert (nested["lo"], nested["hi"]) == (Decimal("0.50"), Decimal("0.99"))
    flat_wins = port_mod.yes_price_band({"yes_min_ask": "0.10", "yes_max_ask": "0.20",
                                         "strategy": {"yes_min_ask": "0.50", "yes_max_ask": "0.99"}})
    assert (flat_wins["lo"], flat_wins["hi"]) == (Decimal("0.10"), Decimal("0.20"))
    # a real config carries the band under cfg["strategy"] — read it and take inside it
    real = json.loads((ROOT / "config" / "yes2re_reversal.json").read_text(encoding="utf-8"))
    real_band = port_mod.yes_price_band(real)
    assert real_band == {"ok": True, "lo": Decimal("0.48"), "hi": Decimal("0.9"),
                        "detail": "(0.48, 0.9]"}, real_band
    # ... and the band it yields is what actually decides: 0.60 in, 0.30/0.95 out
    shifted = {"strategy": {"yes_min_ask": "0.30", "yes_max_ask": "0.60"}}
    assert _decide("0.60", cfg=shifted)["taker"] is True
    assert _decide("0.30", cfg=shifted)["reason"] == "yes_price_below_band"
    assert _decide("0.65", cfg=shifted)["reason"] == "yes_price_above_band"

    # (c) a present-but-illegal band refuses the leg (never a downgrade to a resting order)
    for bad in ({"yes_max_ask": "abc"}, {"yes_min_ask": "0"}, {"yes_min_ask": "0.95", "yes_max_ask": "0.90"},
                {"strategy": {"yes_min_ask": "NaN", "yes_max_ask": "0.9"}},
                {"strategy": {"yes_min_ask": "-0.1", "yes_max_ask": "0.9"}},
                {"strategy": {"yes_max_ask": "1.5", "yes_min_ask": "0.48"}}):
        got = _decide("0.83", cfg=bad)
        assert got["taker"] is False and got["reason"] == "yes_band_unparsed", (bad, got)
        assert got["order_mode"] == "skip" and got["yes_price"] is None, (bad, got)


def test_live_taker_decision_alone_is_not_audited():
    """L-1: a pure decision writes NO audit row; a real take still lands on intent/submit."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "live_events.jsonl"
        log.write_text("", encoding="utf-8")
        saved = submit.AUDIT_PATH
        submit.AUDIT_PATH = log
        try:
            transport = _TakerStub(yes_ask="0.83")
            port = _live_port(transport, account=ACCOUNT_OK, preflight=True)
            port.audit_path = log
            # every shape of pure decision — in band, below, above, NO leg, uncapped YES leg,
            # YES leg with an empty book — must leave the log exactly as it was
            for px in ("0.83", "0.30", "0.95"):
                port.fill_mode(fire=_yes_fire(px), leg=_yes_leg(px), cfg=CFG,
                               book=TAKER_BOOK, client=object())
                port.match(leg=_yes_leg(px), book=TAKER_BOOK, limit=Decimal("0.83"),
                           shares=Decimal("10"), fire=_yes_fire(px), cfg=CFG)
            port.match(leg=_no_leg(), book=NO_BOOK, limit=Decimal("0.62"), shares=Decimal("10"),
                       fire=_yes_fire("0.83"), cfg=CFG)
            port.match(leg=_yes_leg("0.83", cap=None), book=TAKER_BOOK, limit=Decimal("0.83"),
                       shares=Decimal("10"), fire=_yes_fire("0.83"), cfg=CFG)
            port.match(leg=_yes_leg("0.83"), book=NO_BOOK, limit=Decimal("0.83"),
                       shares=Decimal("10"), fire=_yes_fire("0.83"), cfg=CFG)
            assert log.read_text(encoding="utf-8").strip() == "", log.read_text(encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = submit._audit_summary(as_json=True)
            summary = json.loads(out.getvalue())
            assert rc == 0 and summary["records"] == 0, summary
            assert summary["actions"] == {} and summary["real_submits"] == 0, summary
            assert "fill_mode" not in summary["actions"], summary

            # a REAL take (stub SDK) still records intent + submit, carrying the decision fields
            client = _taker_client(matched="10", price="0.83")
            with _stub_v2_lib():
                taken = v2_transport.execute_leg(
                    client, token_id="TOK_YES", side="BUY", price="0.83", size="10",
                    book=TAKER_BOOK, gates=GATES_OK, taker=True, cap="0.90",
                    sleep=lambda _s: None, poll_attempts=2, audit_path=log,
                    audit_extra={"taker_gate": "yes_band", "taker_gate_ok": True,
                                 "yes_price": "0.83", "yes_price_source": "leg_best_ask",
                                 "fill_mode": "yes_band", "leg": "buy_yes_new"})
            assert taken["ok"] is True, taken
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                submit._audit_summary(as_json=True)
            summary = json.loads(out.getvalue())
        finally:
            submit.AUDIT_PATH = saved
    assert summary["records"] == 2 and summary["actions"] == {"intent": 1, "submit": 1}, summary
    assert "fill_mode" not in summary["actions"], summary
    assert summary["real_submits"] == 1 and summary["submit_order_ids"] == ["ORD-1"], summary


def test_live_fire_intent_is_leg_level():
    """End to end through the engine: each leg is judged on its own evidence, nothing is passive.

    ``_r_cycle._paper_fire`` is unchanged (the paper path is byte-identical); this proves the
    *live* side of that same call: the YES leg's own ladder rung and the port band agree exactly
    (``send_fak`` ⇔ ask ∈ (0.48, 0.90]), the NO leg takes on its own ask independently, and a leg
    that is not eligible produces **no order at all**.
    """
    yes_book = {"best_ask": "0.83", "tick_size": "0.01", "neg_risk": True,
                "asks": [{"price": "0.83", "size": "50"}], "bids": [{"price": "0.80", "size": "100"}]}
    books = {**BOOKS, "TOK_YES": yes_book}

    def _fire_run(yes_ask, *, books_by_token=None):
        fire = copy.deepcopy(FIRE)
        fire["legs"][1]["cap"] = "0.90"            # the shipped YES cap (yes_max_ask)
        fire["yes_ask"] = str(yes_ask)
        transport = _TakerStub()
        port = port_mod.LivePort(transport, env=_live_env(), gates=GATES_OK,
                                 limits={"fire_budget_usdc": "20", "max_open_positions": "12",
                                         "max_capital_usdc": "500"},
                                 account_reader=lambda client: ACCOUNT_OK, sleep=lambda _s: None)
        port_mod.reset_cache()
        port_mod._CACHE[port_mod.LIVE] = port
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = _r_state.load_config(str(ROOT / "config" / "yes2re_reversal.json"),
                                       env={"YES2RE_MODE": "live"})
        state: dict = {"paper_initial_capital_usdc": 600.0}
        with contextlib.ExitStack() as stack:
            stack.enter_context(_patched(_r_cycle,
                                         book_cache=lambda: copy.deepcopy(books_by_token or books),
                                         log_event=lambda path, payload: None))
            stack.enter_context(_patched(port_mod.creds_mod, load_env_file=lambda: _live_env()))
            position, ladlog = _r_cycle._paper_fire(cfg, state, fire, NOW)
        port_mod.reset_cache()
        calls = [call[1] for call in transport.calls if call[0] == "execute_leg"]
        return cfg, position, ladlog, calls

    cfg, position, ladlog, calls = _fire_run("0.83")
    assert cfg["mode"] == "live" and cfg["strategy"]["yes_min_ask"] == "0.48", cfg["strategy"]
    assert position is not None and position["key"] == FIRE["key"], position
    by_token = {kw["token_id"]: kw for kw in calls}
    assert set(by_token) == {"TOK_NO", "TOK_YES"}, calls
    yes_kw, no_kw = by_token["TOK_YES"], by_token["TOK_NO"]
    # the YES leg (its own ask 0.83, inside the band): one FAK take, never clamped, cap forwarded
    assert yes_kw["taker"] is True and yes_kw["post_only"] is False and yes_kw["clamp"] is False, yes_kw
    assert yes_kw["cap"] == Decimal("0.90"), yes_kw
    assert yes_kw["audit_extra"]["taker_gate"] == "yes_band", yes_kw["audit_extra"]
    assert yes_kw["audit_extra"]["yes_price"] == "0.83", yes_kw["audit_extra"]
    assert yes_kw["audit_extra"]["fill_mode"] == "yes_band", yes_kw["audit_extra"]
    # the NO leg is judged independently — it takes on its own ask, not because the band is open
    assert no_kw["taker"] is True and no_kw["post_only"] is False, no_kw
    assert no_kw["audit_extra"]["taker_gate"] == "no_leg_ask", no_kw["audit_extra"]
    assert no_kw["cap"] == Decimal("0.65"), no_kw

    # the YES ladder rung and the port band agree exactly, and an ineligible YES ask produces
    # no order at all (never a resting order)
    def _yes_probe(ask):
        probe = {**books, "TOK_YES": {**yes_book, "best_ask": ask,
                                      "asks": [{"price": ask, "size": "50"}]}}
        _cfg, _pos, lad, out = _fire_run("0.83", books_by_token=probe)
        statuses = [i["status"] for i in lad if i.get("leg") == "buy_yes_new"]
        return (statuses[0] if statuses else None), [kw for kw in out if kw["token_id"] == "TOK_YES"]

    for ask, want in (("0.40", False), ("0.48", False), ("0.4801", True), ("0.8999", True),
                      ("0.90", True), ("0.9001", False)):
        status, yes_calls = _yes_probe(ask)
        if want:
            assert status == "send_fak" and len(yes_calls) >= 1, (ask, status, yes_calls)
            assert all(kw["taker"] is True for kw in yes_calls), (ask, yes_calls)
        else:
            assert status != "send_fak", (ask, status)
            assert yes_calls == [], (ask, yes_calls)

    # a leg whose book is empty is skipped as no_book — and still nothing passive is ever sent
    bare = {**BOOKS, "TOK_NO": NO_BOOK, "TOK_YES": NO_BOOK}
    _cfg, _pos, lad, out = _fire_run("0.83", books_by_token=bare)
    assert out == [], out
    assert {i["status"] for i in lad} <= {"no_book"}, lad
    assert all(kw["post_only"] is False for kw in out), out


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
    ("port: missing v2 SDK ⇒ live_deps_missing (F3)", test_port_missing_v2_sdk_fails_closed),
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
    ("live taker: YES band (0.48, 0.90] matrix", test_live_taker_yes_band_matrix),
    ("live taker: leg scope independent (YES band vs NO book)", test_live_leg_scope_is_independent),
    ("live taker: bad ask quote ⇒ ask_out_of_range (not no_book)", test_live_ask_quote_out_of_range_is_not_no_book),
    ("live taker: no passive fallback anywhere", test_live_no_passive_fallback_anywhere),
    ("live taker: cap required + bounded (L-2)", test_live_taker_cap_required_and_bounded),
    ("live taker: absolute price bounds (L-3)", test_live_taker_price_bounds),
    ("live taker: evidence chain (leg → fire → re-quote)", test_live_taker_evidence_chain),
    ("live taker: order construction (FAK, no clamp)", test_live_taker_order_construction),
    ("live taker: market amount precision (USDC 2dp / shares 4dp)", test_live_taker_market_amount_precision),
    ("live taker: partial/zero fill accounting", test_live_taker_fill_accounting),
    ("live taker: gates/preflight not bypassable", test_live_taker_gates_not_bypassable),
    ("live taker: FAK no match clean cancellation", test_live_taker_fak_no_match_clean_cancellation),
    ("live taker: limits kept, band fail-closed", test_live_taker_limits_and_band_fail_closed),
    ("live taker: pure decision adds no audit row (L-1)", test_live_taker_decision_alone_is_not_audited),
    ("live taker: engine fire path is leg-level", test_live_fire_intent_is_leg_level),
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
