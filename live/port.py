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
    (``re_execution.paper_match_fak``), ``live`` → a real CLOB v2 order reconciled
    to its real fill.
``fund(state, cfg, fire, total_cost)``
    book the cost in the one ledger the engine already owns (``paper_capital.reserve``) —
    shared by both modes so accounting stays identical.

No strategy branch lives here: the port never decides *whether*, *which leg* or *how much*.
``get_port`` refuses (machine-readable reason, never a silent downgrade to paper) when live is
requested without its gates.

LIVE take rule (operator instruction, 2026-09-11; corrected 2026-09-12)
---------------------------------------------------------------------
Live used to be passive-only: every ``send_fak`` ladder intent became a ``post_only`` order that
by construction could never fill, while paper filled it by walking the asks.  The operator's rule
is now **leg-level independent permission, and never a passive fallback** — the second half is the
critical safety semantics: a passive order on a fake breakout fills against the bid and buys a
bucket that is going to zero (observed on 2026-09-11: Toronto YES → 0.001, Warsaw −69%).

* **YES leg** (``buy_yes_new`` / ``buy_yes_sleeve`` / ``outcome == "YES"``): takes with a **FAK**
  order **only while its own price sits inside ``(yes_min_ask, yes_max_ask]``** (cfg-driven,
  defaults 0.48 / 0.90, half-open: 0.48 excluded, 0.90 included).  The band is read from the
  *strategy's existing* ``yes_min_ask`` / ``yes_max_ask`` (flat ``cfg`` key or ``cfg["strategy"]``).
  **Outside the band — or with no usable price, or with an illegal band value — the leg is
  dropped: no order of any kind is sent.**  (A YES ask of 0.40 is a fake breakout below the
  floor; resting there would fill at the bid and buy a bucket that is dying.)
* **NO leg** (any non-YES leg): bounded by **its own cap** (the strategy's ``no_max_ask``, currently
  1.0 — see the note below) and by **book depth**.  It takes with **FAK** while the leg's book
  shows a resting ask; with **no ask** it is skipped as ``no_book``, and with a quote that is
  present but unusable (non-numeric, ``<= 0`` or ``> 1``) it is refused as ``ask_out_of_range``
  rather than mislabelled as an empty book.  It is **never** sent as a passive order either.
* **no passive fallback anywhere on the live path**: whenever a window / cap / book / price
  condition is not met the leg is refused or skipped — a resting ``post_only`` order is never
  produced by ``LivePort.match``.  (``live/v2_transport.execute_leg`` keeps its historical maker
  branch for the explicit diagnostics in ``live/smoke.py``; the trading path never uses it.)
* fail-closed, all of which mean **refuse, never downgrade**: a cap that is missing, unparseable,
  non-positive or ``> 1``; a YES band that is present but illegal; no YES price evidence at all; a
  YES price outside the band; a limit that is ``<= 0``, ``> cap`` or ``> 1``.  A cap of exactly
  ``1.0`` is accepted (it is the shipped ``no_max_ask``) but the absolute ``<= 1`` limit still
  binds, so a taker can never exceed the venue's maximum price.
* the YES price evidence is resolved, in order, from **this leg's own context**: the YES-leg ladder
  intent handed to ``match`` (``leg["best_ask"]``) → ``fire["ladder"]``'s YES row →
  ``fire["yes_ask"] / fire["yes_price"] / fire["yes_best_ask"]`` → one read-only re-quote of the
  fire's YES token through ``transport.refetch_book`` (cached for the lifetime of that exact fire
  dict, so later rungs of the same fire reuse it).  Remaining failure modes are ``yes_price_unknown``
  and ``fire_yes_price_read_failed``.
* the decision travels on the **existing** audit records: ``order_mode`` / ``taker_gate`` /
  ``yes_price`` / ``yes_price_source`` are merged into the mandatory ``intent``/``submit`` rows,
  so a pure decision (a ``match`` that sends nothing) adds **no** log line and the
  ``submit.py --summary`` action counts are not polluted.  ``match`` returns ``order_mode``
  (``taker``/``skip``) plus ``taker_gate``/``yes_price`` so the engine's ladder log shows why a
  leg did or did not go out.  Every other red line — triple gate, ``risk_gate``, ``check_limits``,
  tick alignment, minimum size, least-privilege sentinels, the append-only audit — is untouched.

.. note:: ``config/yes2re_reversal.json`` ships ``no_max_ask = "1.0"`` while ``AGENTS.md``
   documents the NO cap as ``0.65``.  The operator's instruction is "everything else unchanged",
   so 1.0 stays; the discrepancy is reported rather than silently "fixed".  With ``cap = 1.0`` the
   only ceiling on a NO take is the absolute ``<= 1`` bound.
"""
from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
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

# --------------------------------------------------------------------- LIVE taker rule
#: how a leg meets the book (recorded as ``order_mode`` in the audit log / ladder log)
MAKER = "maker"                 # kept for the diagnostics path (live/smoke.py) — never the trading path
TAKER = "taker"
SKIP = "skip"                   # this leg produced no order at all (refused / no book)
#: audit labels of the two independent leg gates
GATE_YES_BAND = "yes_band"      # YES leg: its own price inside (yes_min_ask, yes_max_ask]
GATE_NO_LEG_ASK = "no_leg_ask"  # non-YES leg: a resting ask exists in this leg's own book
#: YES-leg names in a fire spec — pure data mirror of the strategy's leg names
YES_LEG_NAMES = ("buy_yes_new", "buy_yes_sleeve")
#: band defaults when cfg carries no yes_min_ask / yes_max_ask
DEFAULT_YES_MIN_ASK = Decimal("0.48")
DEFAULT_YES_MAX_ASK = Decimal("0.90")

#: machine-readable reasons behind one leg's take/skip decision
FILL_TAKER = "yes_band"                    # YES leg inside (lo, hi] ⇒ FAK
FILL_TAKER_NO = "no_leg_ask"               # non-YES leg with a resting ask ⇒ FAK
FILL_SKIP_NO_BOOK = "no_book"              # this leg's book has no resting ask ⇒ skip
FILL_REFUSE_ASK_BAD = "ask_out_of_range"   # a quote IS there but unusable (<= 0 / > 1 / NaN) ⇒ refuse
FILL_REFUSE_BELOW = "yes_price_below_band"
FILL_REFUSE_ABOVE = "yes_price_above_band"
FILL_REFUSE_UNKNOWN = "yes_price_unknown"
FILL_REFUSE_BAND_BAD = "yes_band_unparsed"
FILL_REFUSE_NO_CAP = "taker_cap_missing"   # no explicit, usable cap ⇒ refuse (L-2)

ONE = Decimal("1")


def _band_number(value) -> Decimal | None:
    """Parse one band bound; ``None`` when it is absent or unusable (caller fails closed)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    if not out.is_finite() or out <= ZERO or out > 1:
        return None
    return out


def _band_raw(cfg: dict | None, name: str):
    """Read a strategy parameter from ``cfg`` — flat first, then ``cfg["strategy"]``."""
    if not isinstance(cfg, dict):
        return None
    value = cfg.get(name)
    if value is not None:
        return value
    strategy = cfg.get("strategy")
    if isinstance(strategy, dict):
        return strategy.get(name)
    return None


def _cap_number(value) -> Decimal | None:
    """One leg's ask cap (positive and finite); ``None`` when absent/unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    if not out.is_finite() or out <= ZERO:
        return None
    return out


def taker_cap_number(value) -> Decimal | None:
    """The cap a take is allowed to lean on: an explicit ``0 < cap <= 1``.

    ``None`` when the leg carries no cap, an unparseable one, a non-positive one, or one above the
    venue maximum (``> 1``).  A missing/invalid cap must never authorise an aggressive order, and
    the transport refuses ``taker=True`` without a usable one (L-2).  ``cap == 1.0`` is accepted
    because that is the shipped ``no_max_ask``; with it, the absolute ``<= 1`` limit is what binds.
    """
    out = _cap_number(value)
    if out is None or out > ONE:
        return None
    return out


def is_yes_leg(leg: dict | None) -> bool:
    """Is this the fire's YES leg?  Pure data mirror of the strategy's leg names/outcomes.

    The two legs are judged **independently** (operator correction, 2026-09-12): only the YES leg
    is bound by the ``(yes_min_ask, yes_max_ask]`` band, while a non-YES leg is bound by its own
    cap and its own book depth.
    """
    if not isinstance(leg, dict):
        return False
    return (str(leg.get("leg")) in YES_LEG_NAMES
            or str(leg.get("outcome") or "").upper() == "YES")


def ask_state_of(book) -> dict:
    """Classify this leg's own quote: ``{"present": bool, "ask": Decimal | None, "raw": Any}``.

    ``present=False`` means the leg's book genuinely carries **no** resting ask (``best_ask``
    missing, ``None`` or blank) ⇒ the caller skips the leg as ``no_book``.

    ``present=True`` with ``ask is None`` means a quote *is* there but is not a usable price
    (non-numeric, ``<= 0`` or ``> 1``) ⇒ the caller **refuses** it as ``ask_out_of_range``.
    Reporting a corrupt/out-of-range quote as ``no_book`` would send the operator hunting for a
    missing feed instead of a bad one (audit LOW, 2026-09-12).  Pure: reads nothing but ``book``.
    """
    if not isinstance(book, dict):
        return {"present": False, "ask": None, "raw": None}
    raw = book.get("best_ask")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"present": False, "ask": None, "raw": raw}
    return {"present": True, "ask": _band_number(raw), "raw": raw}


def best_ask_of(book) -> Decimal | None:
    """This leg's own best ask from a normalized book dict (``None`` when there is no *usable* ask).

    Thin wrapper over :func:`ask_state_of` (same contract as before it grew a reason), kept because
    callers and tests use it directly.  Use ``ask_state_of`` when the *reason* matters.
    """
    return ask_state_of(book)["ask"]


def yes_price_band(cfg: dict | None) -> dict:
    """The YES band that permits taking: ``(yes_min_ask, yes_max_ask]``.

    Absent ⇒ the defaults (0.48 / 0.90).  Present but unparseable/out of range ⇒ ``ok=False``
    and the caller **must not** take (fail closed).  Pure: reads nothing but ``cfg``.
    """
    lo_raw = _band_raw(cfg, "yes_min_ask")
    hi_raw = _band_raw(cfg, "yes_max_ask")
    lo = DEFAULT_YES_MIN_ASK if lo_raw is None else _band_number(lo_raw)
    hi = DEFAULT_YES_MAX_ASK if hi_raw is None else _band_number(hi_raw)
    if lo is None or hi is None or lo >= hi:
        return {"ok": False, "lo": None, "hi": None,
                "detail": (f"yes band unusable (yes_min_ask={lo_raw!r}, yes_max_ask={hi_raw!r}) "
                           f"— failing closed to passive")}
    return {"ok": True, "lo": lo, "hi": hi,
            "detail": f"({lo}, {hi}]"}


def in_yes_band(price, lo, hi) -> bool:
    """Half-open band test: ``lo < price <= hi`` (0.48 excluded, 0.90 included)."""
    px = _band_number(price)
    if px is None:
        return False
    try:
        low = Decimal(str(lo))
        high = Decimal(str(hi))
    except (InvalidOperation, AttributeError, ValueError):
        return False
    if not (low.is_finite() and high.is_finite()) or low >= high:
        return False
    return bool(low < px <= high)


def _yes_leg_of(fire: dict) -> dict:
    """The fire's YES leg spec (``buy_yes_new`` / ``buy_yes_sleeve``), if it has one."""
    for leg in (fire or {}).get("legs") or []:
        if is_yes_leg(leg):
            return leg
    return {}


def yes_leg_price(fire: dict | None, *, leg: dict | None = None) -> dict:
    """Resolve **this YES leg's own** price and where it came from (pure).

    Order of evidence, closest to the leg first: the YES-leg ladder intent handed to ``match``
    (``leg["best_ask"]``) → the fire's own ladder log YES row → an explicit
    ``yes_ask``/``yes_price``/``yes_best_ask`` field.  A ``None`` price is not an error, it is
    "no evidence ⇒ drop the leg" (the caller may still try one read-only re-quote).
    """
    fire = fire if isinstance(fire, dict) else {}
    if isinstance(leg, dict) and is_yes_leg(leg):
        px = _band_number(leg.get("best_ask"))
        if px is not None:
            return {"price": px, "source": "leg_best_ask"}
    for row in fire.get("ladder") or []:
        if not isinstance(row, dict):
            continue
        if is_yes_leg(row):
            px = _band_number(row.get("best_ask"))
            if px is not None:
                return {"price": px, "source": "fire_ladder"}
    for field in ("yes_ask", "yes_price", "yes_best_ask"):
        px = _band_number(fire.get(field))
        if px is not None:
            return {"price": px, "source": f"fire_{field}"}
    return {"price": None, "source": "no_fire_yes_price"}


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
        #: fire-level YES-price memo (see ``_fire_yes_price``) — keyed by the exact fire dict
        self._band_fire: dict | None = None
        self._band_ctx: dict | None = None

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
            self._client = None
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
            # fail closed: a denied gate leaves no client behind, so a later ``match`` cannot
            # slip an order through a preflight that said no (matters now that orders may cross)
            self._client = None
            return {"ok": False, "reason": f"risk_gate:{gate['reason']}", "detail": gate["detail"],
                    "stage": "risk_gate", "account": _slim(account)}
        limits = submit.check_limits(
            notional_usdc=budget,
            fire_budget_usdc=self.limits.get("fire_budget_usdc"),
            committed_usdc=account.get("positions_value_usdc") or 0,
            max_capital_usdc=self.limits.get("max_capital_usdc"),
        )
        if not limits["ok"]:
            self._client = None
            return {"ok": False, "reason": f"limits:{limits['reason']}", "detail": limits["detail"],
                    "stage": "limits", "account": _slim(account)}
        return {"ok": True, "reason": REASON_OK, "detail": limits["detail"],
                "account": _slim(account), "risk_gate": gate, "limits": limits}

    # ---------------------------------------------------------------- fill mode (taker rule)
    def _refetch_yes_price(self, fire: dict, client) -> tuple[Decimal | None, str]:
        """Read-only re-quote of this fire's YES token (one call per fire; never a write)."""
        token = fire.get("new_yes_token") or _yes_leg_of(fire).get("token_id")
        fetch = getattr(self.transport, "refetch_book", None)
        if not token or not callable(fetch):
            return None, "no_fire_yes_price"
        try:
            book = fetch(client, str(token))
        except Exception:  # noqa: BLE001 - a quote failure only means "drop this leg"
            return None, "fire_yes_price_read_failed"
        if not isinstance(book, dict):
            return None, "fire_yes_price_read_failed"
        px = _band_number(book.get("best_ask"))
        return (px, "refetch_book_yes_leg") if px is not None else (None, "fire_yes_price_read_failed")

    def _fire_yes_price(self, fire: dict, leg: dict | None, client) -> dict:
        """The fire's YES price, resolved once per fire (memoised on this exact fire dict)."""
        got = yes_leg_price(fire, leg=leg)
        if got["price"] is not None:
            self._band_fire, self._band_ctx = fire, {"yes_price": got["price"], "source": got["source"]}
            return self._band_ctx
        if self._band_fire is fire and self._band_ctx is not None:
            memo = dict(self._band_ctx)
            memo["source"] = f"{memo['source']}_memo"
            return memo
        price, source = self._refetch_yes_price(fire, client)
        if price is not None:
            self._band_fire, self._band_ctx = fire, {"yes_price": price, "source": source}
            return self._band_ctx
        return {"yes_price": None, "source": source}

    def fill_mode(self, *, fire: dict | None, leg: dict | None, cfg: dict | None,
                  book=None, client=None) -> dict:
        """Decide, for **this leg alone**, whether a FAK order goes out — or nothing at all.

        Operator rule (corrected 2026-09-12): **leg-level independent permission and never a
        passive fallback.**  A YES leg takes only while its own price is inside
        ``(yes_min_ask, yes_max_ask]`` (defaults 0.48 / 0.90); a non-YES leg takes while its own
        cap and own book allow it.  Every other outcome is a **refusal/skip**, never a resting
        ``post_only`` order — a passive order on a fake breakout fills at the bid and buys a
        bucket that is going to zero.

        Fail-closed, in order: unusable cap (missing / unparseable / ``<= 0`` / ``> 1``) ⇒ refuse;
        illegal band (for any leg, so a corrupt config stands the whole fire down) ⇒ refuse; no
        resting ask at all in this leg's own book ⇒ ``no_book`` skip; an ask that is *present but
        unusable* (non-numeric / ``<= 0`` / ``> 1``) ⇒ ``ask_out_of_range`` refuse — a bad quote is
        not an empty book; for a YES leg only, no price evidence or a price outside the band ⇒
        refuse.  Pure decision (plus the optional read-only YES re-quote); no order is placed and
        nothing is mutated except this port's per-fire memo.
        """
        fire = fire if isinstance(fire, dict) else {}
        leg = leg if isinstance(leg, dict) else {}
        band = yes_price_band(cfg)
        is_yes = is_yes_leg(leg)
        gate = GATE_YES_BAND if is_yes else GATE_NO_LEG_ASK
        base = {"taker": False, "order_mode": SKIP, "taker_gate": gate,
                "yes_price": None, "source": "n/a", "cap": None,
                "lo": band["lo"], "hi": band["hi"], "leg": leg.get("leg")}
        cap = taker_cap_number(leg.get("cap"))
        if cap is None:
            return {**base, "reason": FILL_REFUSE_NO_CAP,
                    "detail": (f"leg {leg.get('leg')!r} carries no usable cap "
                               f"(cap={leg.get('cap')!r}; need 0 < cap <= 1) — refuse")}
        base["cap"] = cap
        if not band["ok"]:
            return {**base, "reason": FILL_REFUSE_BAND_BAD, "source": "cfg_yes_band",
                    "detail": band["detail"]}
        quote = ask_state_of(book)
        if not quote["present"]:
            return {**base, "reason": FILL_SKIP_NO_BOOK, "source": "leg_book",
                    "detail": (f"leg {leg.get('leg')!r} has no resting ask in its own book — "
                               f"no_book (no order is sent)")}
        if quote["ask"] is None:
            # a quote IS on the book but is not a usable price: calling that "no_book" would
            # point the operator at a missing feed instead of a corrupt one.
            return {**base, "reason": FILL_REFUSE_ASK_BAD, "source": "leg_book_bad_quote",
                    "detail": (f"leg {leg.get('leg')!r} quotes an unusable ask "
                               f"({quote['raw']!r}; need 0 < ask <= 1) — ask_out_of_range, "
                               f"refuse (no order is sent, never a passive order)")}
        ask = quote["ask"]
        base["ask"] = ask
        if not is_yes:
            # non-YES leg: its own cap and its own book depth are the whole rule
            return {**base, "taker": True, "order_mode": TAKER, "reason": FILL_TAKER_NO,
                    "source": "leg_book_ask", "detail": (f"NO leg ask {ask} <= cap {cap} — take "
                                                         f"(own cap + book depth)")}
        evidence = self._fire_yes_price(fire, leg, client)
        px = evidence["yes_price"]
        source = evidence["source"]
        if px is None:
            return {**base, "reason": FILL_REFUSE_UNKNOWN, "source": source,
                    "detail": "no YES price evidence — refuse (never a passive order)"}
        base["yes_price"] = px
        base["source"] = source
        if in_yes_band(px, band["lo"], band["hi"]):
            return {**base, "taker": True, "order_mode": TAKER, "reason": FILL_TAKER,
                    "detail": f"YES {px} in ({band['lo']}, {band['hi']}] — take ({source})"}
        below = px <= band["lo"]
        return {**base, "reason": FILL_REFUSE_BELOW if below else FILL_REFUSE_ABOVE,
                "detail": (f"YES {px} {'<=' if below else '>'} band "
                           f"({band['lo']}, {band['hi']}] — refuse, no order sent ({source})")}

    # ---------------------------------------------------------------- fill
    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        """One **take-or-nothing** order through the v2 transport; real fills come back.

        A leg either goes out as an aggressive **FAK** take (YES leg inside its band; non-YES leg
        with a resting ask inside its cap) or it does not go out at all — ``match`` never places a
        passive/``post_only`` order (see the module docstring).  The decision, its gate and the
        price evidence are returned as ``order_mode`` (``taker``/``skip``) / ``taker_gate`` /
        ``yes_price`` and — because a pure decision must not grow the audit log — travel on the
        existing ``intent``/``submit`` rows (``audit_extra``), not on a row of their own.
        """
        client = getattr(self, "_client", None)
        if client is None:
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": "live_not_preflighted", "source": LIVE,
                    "detail": "preflight must run before any order"}
        ctx = self.fill_mode(fire=fire, leg=leg, cfg=cfg, book=book, client=client)
        audit_extra = {"taker_gate": ctx["taker_gate"], "taker_gate_ok": ctx["taker"],
                       "yes_price": str(ctx["yes_price"]) if ctx["yes_price"] is not None else None,
                       "yes_price_source": ctx["source"], "fill_mode": ctx["reason"],
                       "leg": leg.get("leg")}
        if not ctx["taker"]:
            # take-or-nothing: a leg that is out of band / uncapped / unpriced / has no book is
            # dropped here.  No order of ANY kind is sent — the historical passive fallback is gone.
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": ctx["reason"], "order_id": None, "residual_risk": False,
                    "limit_price": None, "clamped": False, "detail": ctx["detail"],
                    "order_mode": SKIP, "taker_gate": ctx["taker_gate"],
                    "yes_price": ctx["yes_price"], "fill_and_kill": False, "source": LIVE}
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
            post_only=False,
            clamp=False,
            taker=True,
            cap=ctx["cap"],
            poll_attempts=self.poll_attempts,
            poll_sleep=self.poll_sleep,
            sleep=self.sleep,
            audit_path=self.audit_path,
            audit_extra=audit_extra,
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
                "order_mode": result.get("order_mode") or TAKER,
                "taker_gate": ctx["taker_gate"],
                "yes_price": ctx["yes_price"],
                "fill_and_kill": bool(result.get("fill_and_kill")),
                "source": LIVE}

    def describe(self) -> dict:
        return {"mode": LIVE, "transport": "live.v2_transport (py-clob-client-v2)",
                "released": list(getattr(self.transport, "RELEASE_WRITE_METHODS", ())),
                "gates": self.gates.get("checks"), "limits": self.limits,
                "order_modes": [TAKER, SKIP], "passive_fallback": False,
                "taker_gates": [GATE_YES_BAND, GATE_NO_LEG_ASK],
                "taker_scope": "leg_level_independent", "yes_legs": list(YES_LEG_NAMES)}


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
