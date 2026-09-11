#!/usr/bin/env python3
"""Execution **port** — one strategy, two fill channels.

Why a port and not a second bot: the operator's rule is that live and paper must share the
strategy, the logic and the infrastructure, differing **only** in how a fill happens. So the
engine keeps doing exactly what it does today (data pull, arm/fire decision, windows, consensus
filter, sleeve, leg sizing, state schema, events, health, settlement) and calls this port for
the two things that are genuinely channel-specific:

``preflight(fire, cfg)``
    may this fire proceed at all? (paper: always yes; live: real balance/positions + the
    ``LIVE_*`` hard caps through ``risk_gate`` / ``check_limits``)
``match(leg, book, limit, shares)``
    turn one ladder intent into a fill: ``paper`` → the in-memory FAK matcher
    (``re_execution.paper_match_fak``), ``live`` → a real CLOB v2 post-only order reconciled
    to its real fill.
``fund(state, cfg, fire, total_cost)``
    book the cost in the one ledger the engine already owns (``paper_capital.reserve``) —
    shared by both modes so accounting stays identical.

No strategy branch lives here: the port never decides *whether*, *which leg* or *how much*.
``get_port`` refuses (machine-readable reason, never a silent downgrade to paper) when live is
requested without its gates.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python3.13 live/...py` — make relative imports work
    __package__ = "live"  # `python3.13 live/port.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import re_execution
    from paper_capital import reserve

    from live import creds as creds_mod, reconcile, risk_gate, submit
else:  # `python3.13 tests_port.py` / `import live.port`
    import re_execution
    from paper_capital import reserve

    from . import creds as creds_mod, reconcile, risk_gate, submit

ZERO = Decimal("0")
PAPER = "paper"
LIVE = "live"

#: live gates, service-side analogues of the CLI flags (all deliberately absent from .env)
ENV_ENABLE_SUBMIT = "YES2RE_LIVE_ENABLE_SUBMIT"
ENV_CONFIRM = "YES2RE_LIVE_CONFIRM"

REASON_OK = "ok"
REASON_UNKNOWN_MODE = "unknown_mode"
REASON_LIVE_DEPS = "live_deps_missing"
REASON_LIVE_DISABLED = "live_port_disabled"


class PortRefused(RuntimeError):
    """The requested port cannot be handed out — the caller must stand down (never downgrade)."""

    def __init__(self, reason: str, detail: str = "", mode: str = LIVE):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.mode = mode


class ExecutionPort:
    """The contract. Implementations differ only in how a fill is obtained."""

    mode = "abstract"

    def preflight(self, *, fire: dict, cfg: dict) -> dict:
        raise NotImplementedError

    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        raise NotImplementedError

    def fund(self, *, state: dict, cfg: dict, fire: dict, total_cost: Decimal) -> dict:
        """Book the filled cost in the engine ledger (shared by every mode)."""
        if total_cost <= ZERO:
            return {"ok": True, "reason": "nothing_filled", "detail": "no fill to fund"}
        booked = reserve(state, total_cost)
        if booked is None:
            return {"ok": False, "reason": "fire_insufficient_capital",
                    "detail": f"cannot reserve {total_cost} from the ledger"}
        return {"ok": True, "reason": REASON_OK, "detail": f"reserved {booked}",
                "reserved": str(booked)}

    def describe(self) -> dict:
        return {"mode": self.mode}


class PaperPort(ExecutionPort):
    """Existing behaviour, byte for byte: in-memory capped FAK against the warmed ladder."""

    mode = PAPER

    def preflight(self, *, fire: dict, cfg: dict) -> dict:
        return {"ok": True, "reason": REASON_OK, "detail": "paper: no live gates"}

    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        match = re_execution.paper_match_fak(book, limit, shares)
        return {"filled_shares": match["filled_shares"], "avg_price": match["avg_price"],
                "cost": match["cost"], "unfilled": match["unfilled"], "status": "paper_fak",
                "source": PAPER}

    def describe(self) -> dict:
        return {"mode": PAPER, "matcher": "re_execution.paper_match_fak",
                "ledger": "paper_capital.reserve"}


class LivePort(ExecutionPort):
    """Real fills through the CLOB v2 transport (post-only GTC, reconciled to real fills)."""

    mode = LIVE

    def __init__(self, transport, *, env: dict, gates: dict, limits: dict | None = None,
                 poll_attempts: int = 6, poll_sleep: float = 1.0, sleep=None,
                 account_reader=None, audit_path=None):
        self.transport = transport
        self.env = env or {}
        self.gates = gates
        self.limits = limits or {}
        self.poll_attempts = poll_attempts
        self.poll_sleep = poll_sleep
        self.sleep = sleep
        self.account_reader = account_reader
        self.audit_path = audit_path
        self._last_account: dict | None = None

    # ---------------------------------------------------------------- preflight
    def _read_account(self, client, credentials) -> dict:
        if self.account_reader is not None:
            return self.account_reader(client)
        return self.transport.read_account(client, address=credentials["funder_address"])

    def preflight(self, *, fire: dict, cfg: dict) -> dict:
        """Real balance/positions/caps must all agree before a single order is sent."""
        try:
            credentials = creds_mod.validate_creds(self.env)
            submit.prepare_network(self.env)
            client = self.transport.build_client(credentials)
            account = self._read_account(client, credentials)
        except Exception as exc:  # noqa: BLE001 - fail closed, never guess
            return {"ok": False, "reason": "live_account_unreadable",
                    "detail": f"{type(exc).__name__}: {exc}", "stage": "account"}
        self._last_account = account
        self._client = client
        budget = Decimal(str(fire.get("budget_usdc") or cfg.get("fire_budget_usdc") or 0))
        gate = risk_gate.evaluate(
            usdc_balance=account.get("usdc_balance"),
            open_positions=len(account.get("positions") or []),
            committed_usdc=account.get("positions_value_usdc") or 0,
            fire_budget_usdc=self.limits.get("fire_budget_usdc"),
            max_open_positions=self.limits.get("max_open_positions"),
            max_capital_usdc=self.limits.get("max_capital_usdc"),
        )
        if not gate["allow"]:
            return {"ok": False, "reason": f"risk_gate:{gate['reason']}", "detail": gate["detail"],
                    "stage": "risk_gate", "account": _slim(account)}
        limits = submit.check_limits(
            notional_usdc=budget,
            fire_budget_usdc=self.limits.get("fire_budget_usdc"),
            committed_usdc=account.get("positions_value_usdc") or 0,
            max_capital_usdc=self.limits.get("max_capital_usdc"),
        )
        if not limits["ok"]:
            return {"ok": False, "reason": f"limits:{limits['reason']}", "detail": limits["detail"],
                    "stage": "limits", "account": _slim(account)}
        return {"ok": True, "reason": REASON_OK, "detail": limits["detail"],
                "account": _slim(account), "risk_gate": gate, "limits": limits}

    # ---------------------------------------------------------------- fill
    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        """One passive post-only order through the v2 transport; real fills come back."""
        client = getattr(self, "_client", None)
        if client is None:
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": "live_not_preflighted", "source": LIVE,
                    "detail": "preflight must run before any order"}
        token_id = leg.get("token_id")
        result = self.transport.execute_leg(
            client,
            token_id=str(token_id),
            side=str(leg.get("side") or "BUY"),
            price=limit,
            size=shares,
            book=book,
            tick=leg.get("tick") or (book or {}).get("tick_size") if isinstance(book, dict) else None,
            neg_risk=bool((book or {}).get("neg_risk")) if isinstance(book, dict) else None,
            gates=self.gates,
            post_only=True,
            poll_attempts=self.poll_attempts,
            poll_sleep=self.poll_sleep,
            sleep=self.sleep,
            audit_path=self.audit_path,
        )
        return {"filled_shares": result.get("filled_shares") or ZERO,
                "avg_price": result.get("avg_price"),
                "cost": result.get("cost") or ZERO,
                "unfilled": result.get("unfilled") if result.get("unfilled") is not None else shares,
                "status": result.get("status"),
                "order_id": result.get("order_id"),
                "residual_risk": bool(result.get("residual_risk")),
                "limit_price": result.get("limit_price"),
                "clamped": bool(result.get("clamped")),
                "detail": result.get("detail", ""),
                "source": LIVE}

    def describe(self) -> dict:
        return {"mode": LIVE, "transport": "live.v2_transport (py-clob-client-v2)",
                "released": list(getattr(self.transport, "RELEASE_WRITE_METHODS", ())),
                "gates": self.gates.get("checks"), "limits": self.limits}


def _slim(account: dict) -> dict:
    return {key: account.get(key) for key in
            ("usdc_balance", "open_orders", "positions_value_usdc")}


def live_limits(env: dict) -> dict:
    """``LIVE_*`` hard caps from the environment (same keys the read-only layer uses)."""
    return {name: env.get(key) or None for name, key in reconcile.LIMIT_KEYS.items()}


_CACHE: dict[str, ExecutionPort] = {}


def port_status(cfg: dict, env: dict | None = None, *, enable_submit: bool | None = None,
                confirm: str | None = None) -> dict:
    """Read-only: which port would be handed out and, for live, which gate is missing."""
    env = env if env is not None else creds_mod.load_env_file()
    mode = str((cfg or {}).get("mode") or PAPER).strip().lower()
    if mode == PAPER:
        return {"mode": PAPER, "ok": True, "reason": REASON_OK, "detail": "paper port available"}
    if mode != LIVE:
        return {"mode": mode, "ok": False, "reason": REASON_UNKNOWN_MODE,
                "detail": f"unknown mode {mode!r} (want paper/live)"}
    enable = (env.get(ENV_ENABLE_SUBMIT) == "1") if enable_submit is None else bool(enable_submit)
    gates = submit.gate_status(enable_submit=enable, env=env,
                               confirm=env.get(ENV_CONFIRM) if confirm is None else confirm)
    return {"mode": LIVE, "ok": gates["ok"], "reason": gates["ok"] and REASON_OK or gates["reason"],
            "detail": gates["detail"], "gates": gates, "limits": live_limits(env)}


def get_port(cfg: dict, env: dict | None = None, *, enable_submit: bool | None = None,
             confirm: str | None = None, transport=None, port: ExecutionPort | None = None) -> ExecutionPort:
    """Hand out the port for ``cfg['mode']``.

    ``paper`` → :class:`PaperPort` (no gates, no third-party imports).
    ``live``  → :class:`LivePort`, but only when **all three** service gates are satisfied
    (``YES2RE_LIVE_ENABLE_SUBMIT=1``, ``LIVE_SUBMIT_ENABLED=1``, today's confirm phrase);
    otherwise :class:`PortRefused` — the caller stands the fire down, it never falls back to
    paper silently.
    """
    if port is not None:
        return port
    mode = str((cfg or {}).get("mode") or PAPER).strip().lower()
    if mode == PAPER:
        return _CACHE.setdefault(PAPER, PaperPort())
    if mode != LIVE:
        raise PortRefused(REASON_UNKNOWN_MODE, f"unknown mode {mode!r} (want paper/live)", mode=mode)

    env = env if env is not None else creds_mod.load_env_file()
    status = port_status(cfg, env, enable_submit=enable_submit, confirm=confirm)
    if not status["ok"]:
        raise PortRefused(status["reason"], status["detail"])
    if LIVE in _CACHE:
        return _CACHE[LIVE]
    if transport is None:                     # lazy: the paper path never imports the v2 SDK
        try:
            from . import v2_transport as transport  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise PortRefused(REASON_LIVE_DEPS, f"{type(exc).__name__}: {exc}") from None
    available = getattr(transport, "sdk_available", None)
    if callable(available) and not available():
        # precise reason instead of discovering the missing SDK mid-preflight (still fail-closed)
        raise PortRefused(REASON_LIVE_DEPS, getattr(transport, "V2_HINT", "py-clob-client-v2 missing"))
    live = LivePort(transport, env=env, gates=status["gates"], limits=status["limits"])
    _CACHE[LIVE] = live
    return live


def reset_cache() -> None:
    """Forget cached ports (tests / operator re-configuration)."""
    _CACHE.clear()


def _demo() -> None:
    paper = get_port({"mode": "paper"}, env={})
    assert isinstance(paper, PaperPort) and paper.mode == PAPER
    assert paper.preflight(fire={}, cfg={})["ok"] is True
    assert paper.match(leg={}, book={"asks": [{"price": "0.50", "size": "10"}]},
                       limit=Decimal("0.50"), shares=Decimal("2"))["filled_shares"] == Decimal("2")
    assert PaperPort().fund(state={}, cfg={}, fire={}, total_cost=ZERO)["ok"] is True
    try:
        get_port({"mode": "live"}, env={})
    except PortRefused as exc:
        assert exc.reason == submit.GATE_FLAG, exc.reason
    else:
        raise AssertionError("live without gates must be refused")
    try:
        get_port({"mode": "warp"}, env={})
    except PortRefused as exc:
        assert exc.reason == REASON_UNKNOWN_MODE
    else:
        raise AssertionError("unknown mode must be refused")
    print("port demo OK")


if __name__ == "__main__":
    _demo()
