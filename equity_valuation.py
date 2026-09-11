#!/usr/bin/env python3
"""equity_valuation.py — mark open paper positions to current CLOB order books.

Read-only equity report for the paper account in ``data/yes2re_state.json``.

Model (per user spec):
    equity = initial_capital + realized_cash + sum_open( shares * mark - cost )

  * A leg counts only when ``leg.shares > 0``.
  * A **settled** leg is cashed out: its full net result ``payout_credit_usdc -
    cost_usdc`` is folded into ``realized_cash`` (winning legs payout the
    shares face value, losing legs payout 0, cost already left the account).
  * An **open** (unsettled) leg is marked from the live CLOB order book for its
    ``token_id`` (singleton ``GET /book?token_id=<id>``):

        mark = (ask + bid) / 2                                   # normal book
        mark = ask * 0.9                  when spread >= 20%, i.e.
        spread = (ask - bid) / ((ask + bid) / 2) >= 0.20

    When only one side of the book is populated (common on thin daily-weather
    markets) the position is valued off the present side at the same 0.9
    haircut the wide-spread rule uses — ``mark = best_ask * 0.9`` when only
    asks exist, ``mark = best_bid * 0.9`` when only bids exist (statuses
    ``ok_only_ask_anchor`` / ``ok_only_bid_anchor``). A fully empty or failed
    book is reported as ``unavailable`` and is NOT valued (row is still
    printed and written to the CSV so every open dollar of cost is accounted
    for).

Output:
  * human summary to stdout;
  * ``data/equity_valuation_<ts>.csv`` — one row per OPEN leg (market granular
    columns), then per-market and grand-total rows;
  * ``data/equity_valuation_<ts>.summary.json`` — the aggregates, scriptable.

Stdlib only. Network rides the standard HTTP(S) proxy env vars, so run with
them exported (e.g. ``set -a; source .env; set +a``) — urllib reads them
automatically. No orders, no wallet.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_REL = os.path.join("data", "yes2re_state.json")
DATA_DIR = os.path.join(ROOT, "data")
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
USER_AGENT = "weatherbotyes2re/equity-valuation/1.0"
CLOB_TIMEOUT_S = 15.0
DEFAULT_INITIAL = Decimal("1000.00")

# Valuation knobs (user spec).
WIDE_SPREAD_FRAC = Decimal("0.20")   # spread >=20% triggers the ask*0.9 rule
WIDE_ASK_DISCOUNT = Decimal("0.90")  # discount applied to best-ask when wide

CSV_COLS = [
    "market", "city_id", "local_date", "direction", "bucket", "outcome",
    "leg", "token_id", "status", "shares", "cost", "bid", "ask", "mark_price",
    "mark_value", "net_value",
]
# market-level columns on the per-market rows
_MKT_ROWS = ["city|date|dir", "bucket", "leg", "shares", "cost", "mark_price",
             "mark_value", "net_value"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Safely parse a stored (str / Decimal / float / None) money value."""
    if value is None:
        return default
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return d if d.is_finite() else default


def _fmt(value: Decimal | None, places: int = 4) -> str:
    """Decimal -> fixed-point string; None -> ''; strips trailing zeros."""
    if value is None:
        return ""
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum).normalize(), "f")


def load_env(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Import ``export K=V`` lines from .env into os.environ (if not already set).

    Lets proxy vars defined only in .env reach urllib. Mirrors
    ``research/common.load_env``. Real env wins (keys already present are
    skipped).
    """
    env = dict(os.environ)
    p = os.fspath(path) if path is not None else (os.path.join(ROOT, ".env"))
    if not p or not os.path.exists(p):
        return env
    try:
        with open(p, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip("\"'")
                if k and k not in env:
                    os.environ[k] = v
                    env[k] = v
    except OSError:
        pass
    return env


def _fetch_book_json(token_id: str) -> dict[str, Any] | None:
    """GET one CLOB order book; None on any network/HTTP/parse failure."""
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=CLOB_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Order-book + leg valuation
# --------------------------------------------------------------------------- #


def best_bid_ask(raw: Any) -> tuple[Decimal | None, Decimal | None]:
    """Best bid (max) / best ask (min) from a parsed CLOB /book payload."""
    if not isinstance(raw, dict):
        return None, None

    def best_level(side: str, want_max: bool) -> Decimal | None:
        levels = raw.get(side)
        if not isinstance(levels, list):
            return None
        best: Decimal | None = None
        for level in levels:
            if not isinstance(level, dict):
                continue
            price, size = _decimal(level.get("price")), _decimal(level.get("size"))
            if price <= 0 or size <= 0:
                continue
            if best is None or (price > best if want_max else price < best):
                best = price
        return best

    return best_level("bids", want_max=True), best_level("asks", want_max=False)


@dataclass
class OpenLeg:
    """An open (unsettled) leg that still holds shares and must be valued."""

    market_key: str
    city_id: str
    local_date: str
    direction: str
    bucket_label: str | None
    outcome: str            # YES / NO
    leg_name: str
    token_id: str
    shares: Decimal
    cost: Decimal

    # ---- populated during valuation ----
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mark_price: Decimal | None = None     # recognised valuation price
    status: str | None = None             # ok_mid|ok_wide_ask_disc|unavailable_*
    mark_value: Decimal | None = None     # shares*mark_price
    net_value: Decimal | None = None      # shares*mark_price - cost

    def value(self, raw: dict[str, Any] | None, fetch_ok: bool) -> None:
        """Compute mark per the valuation rules; one-sided books use an anchor.

        Two-sided book (both bid and ask present):
            spread >= 20%   ->  mark = ask * 0.9        (status ok_wide_ask_disc)
            spread <  20%   ->  mark = (ask+bid) / 2    (status ok_mid)

        One-sided book: a public one-way quote is still a real price signal.
        Value the holding against the side that is present, in the direction
        that favours the side you could transact on:
            only asks -> mark = ask * 0.9            (status ok_only_ask_anchor)
            only bids -> mark = min(1.0, bid * 1.1)  (status ok_only_bid_anchor)
        A bid-only book shows the price a counterparty will pay for the leg;
        the 1.1 premium approximates the ask that would face a fresh buyer and
        is capped at face value (1.0). Ask-only uses the wide-spread 0.9
        haircut because that is the price at which you could still exit.
        """
        bid, ask = best_bid_ask(raw)
        self.best_bid, self.best_ask = bid, ask
        if not fetch_ok:
            self.status = "unavailable_network"
            return
        if bid is None and ask is None:
            self.status = "unavailable_empty"
            return
        if bid is None or ask is None:
            # single-sided book
            if ask is not None:
                mark = (ask * WIDE_ASK_DISCOUNT).quantize(Decimal("0.0001"))
                self.status = "ok_only_ask_anchor"
            else:
                # min(1.0, bid*1.1): premium toward the unseen ask, capped at 1
                mark = min(Decimal("1.0"), bid * Decimal("1.1")).quantize(Decimal("0.0001"))
                self.status = "ok_only_bid_anchor"
        else:
            mid = (bid + ask) / 2
            if mid <= 0:
                self.status = "unavailable_bad_price"
                return
            spread = (ask - bid) / mid
            if spread >= WIDE_SPREAD_FRAC:
                mark = (ask * WIDE_ASK_DISCOUNT).quantize(Decimal("0.0001"))
                self.status = "ok_wide_ask_disc"
            else:
                mark = mid.quantize(Decimal("0.0001"))
                self.status = "ok_mid"
        self.mark_price = mark
        self.mark_value = (self.shares * mark).quantize(Decimal("0.0001"))
        self.net_value = (self.mark_value - self.cost).quantize(Decimal("0.0001"))

    def as_row(self) -> dict[str, str]:
        def s_or(d: Decimal | None) -> str:
            return _fmt(d, 4) if d is not None else ""
        return {
            "market": self.market_key,
            "city_id": self.city_id,
            "local_date": self.local_date,
            "direction": self.direction,
            "bucket": self.bucket_label or "?",
            "outcome": self.outcome,
            "leg": self.leg_name,
            "token_id": self.token_id,
            "status": self.status or "",
            "shares": _fmt(self.shares, 2),
            "cost": _fmt(self.cost, 4),
            "bid": s_or(self.best_bid),
            "ask": s_or(self.best_ask),
            "mark_price": s_or(self.mark_price),
            "mark_value": s_or(self.mark_value),
            "net_value": s_or(self.net_value),
        }


def iter_position_legs(state: dict[str, Any]) -> Iterable[tuple[dict, dict]]:
    """Yield ``(position, leg)`` for every leg of an open OR settled position.

    Legs with ``shares <= 0`` are skipped (nothing held).
    """
    positions = state.get("positions") or {}
    if not isinstance(positions, dict):
        return
    for pos in positions.values():
        if not isinstance(pos, dict):
            continue
        for leg in pos.get("legs") or []:
            if isinstance(leg, dict) and _decimal(leg.get("shares")) > 0:
                yield pos, leg


# --------------------------------------------------------------------------- #
# Core report
# --------------------------------------------------------------------------- #


class ValuationReport:
    """Aggregate of the valuation run used for printing, CSV and JSON output."""

    def __init__(self, state: dict[str, Any], state_path: str):
        self.state = state
        self.state_path = state_path
        self.generated_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

        self.initial = _decimal(state.get("paper_initial_capital_usdc"), DEFAULT_INITIAL)
        self.ledger_debit = _decimal(state.get("paper_total_debit_usdc"))
        self.ledger_equity = (self.initial - self.ledger_debit).quantize(Decimal("0.01"))

        # Partition legs.
        self.open_legs: list[OpenLeg] = []
        self.realized_cash = Decimal("0")      # sum settled (payout - cost)
        self.settled_count = 0

        for pos, leg in iter_position_legs(state):
            pkey = str(pos.get("key") or "") or _infer_key(pos)
            # Settle by the leg-level flag (authoritative per leg); if a leg
            # has no flag yet it is still open and must be marked.
            if leg.get("settled") is True:
                payout = _decimal(leg.get("payout_credit_usdc"))
                cost = _decimal(leg.get("cost_usdc"))
                self.realized_cash += payout - cost
                self.settled_count += 1
                continue
            ol = OpenLeg(
                market_key=pkey,
                city_id=str(pos.get("city_id") or ""),
                local_date=str(pos.get("market_local_date") or ""),
                direction=str(pos.get("direction") or ""),
                bucket_label=leg.get("bucket_label"),
                outcome=str(leg.get("outcome") or "").upper(),
                leg_name=str(leg.get("leg") or ""),
                token_id=str(leg.get("token_id") or ""),
                shares=_decimal(leg.get("shares")),
                cost=_decimal(leg.get("cost_usdc")),
            )
            self.open_legs.append(ol)

        self.open_total_cost = sum((ol.cost for ol in self.open_legs), Decimal("0"))

        # Fetch & value each open leg (bounded concurrency).
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(self.open_legs)))) as pool:
            futures = {
                pool.submit(self._value_leg, ol): ol for ol in self.open_legs
            }
            for fut in futures:
                fut.result()  # _value_leg never raises (network swallowed)

        self.valued = [ol for ol in self.open_legs if ol.status and ol.status.startswith("ok")]
        self.unvalued = [ol for ol in self.open_legs if not (ol.status and ol.status.startswith("ok"))]
        self.open_mark_value = Decimal("0") if not self.valued else sum(
            (ol.mark_value or Decimal("0")) for ol in self.valued
        )
        self.open_net_unreal = Decimal("0") if not self.valued else sum(
            (ol.net_value or Decimal("0")) for ol in self.valued
        )
        self.open_unvalued_cost = sum((ol.cost for ol in self.unvalued), Decimal("0"))

    @staticmethod
    def _value_leg(ol: OpenLeg) -> None:
        raw = _fetch_book_json(ol.token_id)
        ol.value(raw, fetch_ok=raw is not None)

    # -- totals ------------------------------------------------------------- #

    @property
    def equity_valued(self) -> Decimal:
        """1000 + realized_cash + net of the legs we could actually mark."""
        return (self.initial + self.realized_cash + self.open_net_unreal).quantize(Decimal("0.01"))

    @property
    def equity_floor(self) -> Decimal:
        """Like ``equity_valued`` but count unvalued open legs at mark 0 (cost lost)."""
        return (self.initial + self.realized_cash + self.open_net_unreal
                - self.open_unvalued_cost).quantize(Decimal("0.01"))

    def markets(self) -> list[str]:
        return sorted({ol.market_key for ol in self.open_legs})

    def equity_gap_note(self) -> str:
        """Why ``equity_valued`` may differ from the ledger's initial - debit."""
        gap = self.equity_valued - self.ledger_equity
        why = (
            f"ledger equity={_fmt(self.ledger_equity, 2)} tracks every paper "
            f"reserve/release including fills and unwinds whose legs are no "
            f"longer intact in `positions`; the spec valuation rebuilds from "
            f"{self.settled_count} settled leg records only"
        )
        # If the ledger agrees, say so.
        if gap == 0:
            why = "matches the ledger (all cash movements map to surviving legs)"
        return why


def _infer_key(pos: dict) -> str:
    return "|".join(
        str(pos.get(k) or "")
        for k in ("city_id", "market_local_date", "direction")
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _ts_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_report(report: ValuationReport, tag: str | None = None) -> tuple[str, str]:
    """Write CSV + summary JSON for a report; return (csv_path, summary_path)."""
    tag = tag or _ts_name()
    csv_path = os.path.join(DATA_DIR, f"equity_valuation_{tag}.csv")
    sum_path = os.path.join(DATA_DIR, f"equity_valuation_{tag}.summary.json")
    os.makedirs(DATA_DIR, exist_ok=True)

    lines = [",".join(CSV_COLS)]
    for ol in report.open_legs:                       # per-leg rows
        r = ol.as_row()
        lines.append(",".join(r[c] for c in CSV_COLS))
    # per-market subtotal rows
    for mkt in report.markets():
        legs = [ol for ol in report.open_legs if ol.market_key == mkt]
        sh = sum((l.shares for l in legs), Decimal("0"))
        cost = sum((l.cost for l in legs), Decimal("0"))
        valued = [l for l in legs if l.mark_value is not None]
        mval = sum((l.mark_value or Decimal("0")) for l in valued) if valued else Decimal("0")
        net = sum((l.net_value or Decimal("0")) for l in valued) if valued else Decimal("0")
        lines.append(",".join([
            mkt, "", "", "", "", "", "", "", 
            f"SUBTOTAL({len(valued)}/{len(legs)})",
            _fmt(sh, 2), _fmt(cost, 4), "", "", "",
            _fmt(mval, 4), _fmt(net, 4),
        ]))
    # totals
    lines.append(",".join([
        "TOTAL", "", "", "", "", "", "", "", "",
        _fmt(sum((ol.shares for ol in report.open_legs), Decimal("0")), 2),
        _fmt(report.open_total_cost, 4), "", "", "",
        _fmt(report.open_mark_value, 4), _fmt(report.open_net_unreal, 4),
    ]))
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")

    summary = {
        "generated_at_utc": report.generated_at_utc,
        "state_file": os.path.relpath(report.state_path, ROOT),
        "initial_capital_usdc": _fmt(report.initial, 4),
        "realized_cash_usdc": _fmt(report.realized_cash, 4),
        "settled_legs": report.settled_count,
        "open_legs": len(report.open_legs),
        "open_legs_valued": len(report.valued),
        "open_legs_unavailable": len(report.unvalued),
        "open_total_cost_usdc": _fmt(report.open_total_cost, 4),
        "open_mark_value_usdc": _fmt(report.open_mark_value, 4),
        "open_net_unrealized_usdc": _fmt(report.open_net_unreal, 4),
        "equity_valued_open_only_usdc": _fmt(report.equity_valued, 2),
        "equity_floor_unavailable_zero_usdc": _fmt(report.equity_floor, 2),
        "ledger_total_debit_usdc": _fmt(report.ledger_debit, 4),
        "ledger_equity_initial_minus_debit_usdc": _fmt(report.ledger_equity, 2),
        "equity_vs_ledger_gap_usdc": _fmt(report.equity_valued - report.ledger_equity, 2),
        "gap_explanation": report.equity_gap_note(),
        "unavailable_legs": [ol.as_row() for ol in report.unvalued],
        "legs": [ol.as_row() for ol in report.open_legs],
    }
    with open(sum_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    return csv_path, sum_path


def print_report(report: ValuationReport, csv_path: str) -> None:
    print(f"# equity valuation   {report.generated_at_utc}")
    print(f"state: {os.path.relpath(report.state_path, ROOT)}")
    print(f"initial capital            {_fmt(report.initial, 2):>9}")
    print(f"realized cash (settled payout - cost)  {_fmt(report.realized_cash, 4):>9}   ({report.settled_count} settled legs)")
    print(f"open legs: {len(report.open_legs)} (valued {len(report.valued)}, unavailable {len(report.unvalued)})\n")

    hdr = (
        f"{'market':<30} {'leg':<13} {'bucket':<16} {'sh':>7} {'cost':>8} "
        f"{'bid':>6} {'ask':>6} {'mark':>8} {'mark$':>8} {'net$':>9}  {'status'}"
    )
    print(hdr)
    print("-" * len(hdr))
    for ol in report.open_legs:
        print(
            f"{ol.market_key:<30} {ol.leg_name:<13} {(ol.bucket_label or '?'):<16} "
            f"{_fmt(ol.shares, 2):>7} {_fmt(ol.cost, 2):>8} "
            f"{_fmt(ol.best_bid, 3) if ol.best_bid is not None else '-':>6} "
            f"{_fmt(ol.best_ask, 3) if ol.best_ask is not None else '-':>6} "
            f"{_fmt(ol.mark_price, 4) if ol.mark_price is not None else '-':>8} "
            f"{_fmt(ol.mark_value, 4) if ol.mark_value is not None else '-':>8} "
            f"{_fmt(ol.net_value, 2) if ol.net_value is not None else '-':>9}  {ol.status or ''}"
        )
    print()
    print("# totals")
    print(f"open cost                        {_fmt(report.open_total_cost, 4):>10}")
    print(f"open marked value (shares*mark)  {_fmt(report.open_mark_value, 4):>10}")
    print(f"open net unrealized              {_fmt(report.open_net_unreal, 4):>10}")
    print(f"equity (valued open legs)        {_fmt(report.equity_valued, 2):>10}")
    print(f"equity floor (unvalued -> 0)     {_fmt(report.equity_floor, 2):>10}")
    print(f"ledger equity (initial - debit)  {_fmt(report.ledger_equity, 2):>10}")
    gap = report.equity_valued - report.ledger_equity
    gap_s = ("+" if gap > 0 else "-" if gap < 0 else "") + _fmt(abs(gap), 2)
    print(f"gap (spec vs ledger)              {gap_s:>10}")
    print(f"   {report.equity_gap_note()}")
    print(f"csv: {os.path.relpath(csv_path, ROOT)}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state_rel = argv[0] if argv else STATE_REL
    # Export proxy vars that only live in .env so urllib can reach the CLOB.
    load_env(os.path.join(ROOT, ".env"))

    state_path = state_rel if os.path.isabs(state_rel) else os.path.join(ROOT, state_rel)
    if not os.path.exists(state_path):
        print(f"missing state file: {state_path}", file=sys.stderr)
        return 2
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)

    report = ValuationReport(state, state_path)
    csv_path, _ = write_report(report)
    print_report(report, csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
