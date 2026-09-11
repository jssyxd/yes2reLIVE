#!/usr/bin/env python3
"""Phase-1 live reconciliation — READ ONLY.

Reads: USDC balance + per-contract allowances (CLOB, L2 GET), open orders (CLOB,
L2 GET), positions (data-api GET), egress IP/country (ipinfo.io GET), then runs
the pure risk gate over the *real* numbers and the ``LIVE_*`` limits from ``.env``.

``usdc_balance`` reports the 6-decimal collateral balance; today that collateral is
**pUSD (Polymarket USD, ``0xc011a7e1…``)**, not USDC.e. Same 6 dp, so the value is
correct — only the field name is legacy (kept stable on purpose; see N1 in the audit).

There is no order-placement path in this module (nor anywhere in ``live/``);
``tests_live.py`` asserts that with a static scan.

Usage:
    /home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py
    ... live/reconcile.py --json
    ... live/reconcile.py --out data/live_reconcile.json

Exit codes: 0 = collected, 2 = fail-closed (bad creds / network / anything raised).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # `python3.13 live/reconcile.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from live import clob_client, creds as creds_mod, risk_gate
else:  # `python3.13 tests_live.py` / `import live.reconcile`
    from . import clob_client, creds as creds_mod, risk_gate

DEFAULT_OUT = Path("data/live_reconcile.json")

#: live limits read from .env (keys only — never values of credentials)
LIMIT_KEYS = {
    "fire_budget_usdc": "LIVE_FIRE_BUDGET_USDC",
    "max_open_positions": "LIVE_MAX_OPEN_POSITIONS",
    "max_capital_usdc": "LIVE_MAX_CAPITAL_USDC",
}

MIN_POSITION_VALUE = 0.1
MAX_UINT_APPROX = 2 ** 200
POSITION_FIELDS = (
    "asset", "conditionId", "outcome", "outcomeIndex", "size", "avgPrice",
    "curPrice", "currentValue", "cashPnl", "redeemable", "negativeRisk",
    "title", "slug", "eventSlug",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_allowance(raw):
    """uint256 allowance → ``"max"`` / USDC float / ``0``."""
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return raw
    if amount >= MAX_UINT_APPROX:
        return "max"
    if amount == 0:
        return 0
    return round(amount / 10 ** clob_client.USDC_DECIMALS, 6)


def _slim_position(row: dict) -> dict:
    out = {"currentValue": _num(row.get("currentValue"), 0.0) or 0.0}
    for field in POSITION_FIELDS:
        if field in row:
            out[field] = row[field] if field in ("asset", "conditionId", "outcome", "title",
                                                "slug", "eventSlug") else _num(row[field], row[field])
    return out


def _apply_proxy_env(env: dict) -> None:
    """Expose .env proxy settings to urllib/httpx (both read process env)."""
    for key in ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = (env.get(key) or "").strip()
        if value:
            os.environ.setdefault(key, value)


def _limits(env: dict) -> dict:
    return {name: env.get(key) or None for name, key in LIMIT_KEYS.items()}


def _skeleton(env: dict) -> dict:
    """Empty report with every declared field present (the fail-closed shape)."""
    return {
        "ok": False,
        "reason": None,
        "ts_utc": _now_iso(),
        "mode": str(env.get("YES2RE_MODE") or "").strip() or None,
        "api_ok": False,
        "usdc_balance": None,
        "allowances": {},
        "open_orders": 0,
        "positions": [],
        "positions_raw_count": 0,
        "positions_value_usdc": None,
        "risk_gate": None,
        "limits": _limits(env),
        "egress_ip": None,
        "egress_country": None,
        "egress_org": None,
    }

def collect(env: dict | None = None, *, timeout: int = 25) -> dict:
    """Gather the full read-only snapshot. Never raises — failure ⇒ ``ok: false``.

    A non-mapping ``env`` returns the same fail-closed shape instead of raising.
    """
    if env is not None and not isinstance(env, dict):
        report = _skeleton({})
        report["reason"] = "env: not a mapping"
        return report
    env = env if env is not None else creds_mod.load_env_file()
    report = _skeleton(env)
    try:
        _apply_proxy_env(env)
        clob_client.force_ipv4()
        creds = creds_mod.validate_creds(env)

        client = clob_client.build_client(creds)
        balance_allowance = clob_client.get_balance_allowance(client)
        raw_balance = (balance_allowance or {}).get("balance")
        if raw_balance is None:
            raise RuntimeError("balance-allowance response carried no balance field")
        report["usdc_balance"] = round(int(raw_balance) / 10 ** clob_client.USDC_DECIMALS, 6)
        report["allowances"] = {
            addr: _fmt_allowance(value)
            for addr, value in sorted((balance_allowance.get("allowances") or {}).items())
        }

        report["open_orders"] = len(clob_client.get_open_orders(client) or [])
        report["api_ok"] = True

        positions = clob_client.fetch_positions(creds["funder_address"], timeout=timeout)
        report["positions_raw_count"] = len(positions)
        kept = [_slim_position(p) for p in positions
                if (_num((p or {}).get("currentValue"), 0.0) or 0.0) > MIN_POSITION_VALUE]
        report["positions"] = kept
        report["positions_value_usdc"] = round(sum(p["currentValue"] for p in kept), 6)

        report["risk_gate"] = risk_gate.evaluate(
            usdc_balance=report["usdc_balance"],
            open_positions=len(kept),
            committed_usdc=report["positions_value_usdc"],
            fire_budget_usdc=report["limits"]["fire_budget_usdc"],
            max_open_positions=report["limits"]["max_open_positions"],
            max_capital_usdc=report["limits"]["max_capital_usdc"],
        )

        try:  # egress identity is informational: never fail the whole run for it
            egress = clob_client.fetch_egress_info(timeout=min(timeout, 15))
            report["egress_ip"] = egress.get("ip")
            report["egress_country"] = egress.get("country")
            report["egress_org"] = egress.get("org")
        except Exception:
            pass

        report["ok"] = True
    except Exception as exc:  # fail closed, but keep the partial snapshot
        report["ok"] = False
        try:
            report["reason"] = creds_mod.sanitize(f"{type(exc).__name__}: {exc}", env)
        except Exception:  # a hand-built env with non-string values must still not raise,
            # and its value must not be echoed — so the raw detail is withheld.
            report["reason"] = f"{type(exc).__name__}: <detail withheld: non-string value in env>"
    return report


def human_summary(report: dict) -> str:
    """Readable one-screen summary. Contains no credential values."""
    lines = [
        f"ok={report.get('ok')} api_ok={report.get('api_ok')} mode={report.get('mode')} ts={report.get('ts_utc')}",
    ]
    if not report.get("ok"):
        lines.append(f"reason={report.get('reason')}")
    balance = report.get("usdc_balance")
    lines.append(f"usdc_balance={balance if balance is not None else 'n/a'}")
    allowances = report.get("allowances") or {}
    if allowances:
        maxed = sum(1 for v in allowances.values() if v == "max")
        lines.append(f"allowances={len(allowances)} contracts (max={maxed})")
        for addr, value in allowances.items():
            lines.append(f"  {addr} -> {value}")
    lines.append(f"open_orders={report.get('open_orders')}")
    positions = report.get("positions") or []
    lines.append(
        f"positions={len(positions)} value_usdc={report.get('positions_value_usdc')} "
        f"(currentValue > {MIN_POSITION_VALUE}; raw rows={report.get('positions_raw_count')})"
    )
    for pos in positions[:10]:
        lines.append(
            f"  {str(pos.get('title'))[:44]:44s} {pos.get('outcome')} "
            f"size={pos.get('size')} cur={pos.get('curPrice')} value={pos.get('currentValue')}"
        )
    if len(positions) > 10:
        lines.append(f"  … {len(positions) - 10} more")
    gate = report.get("risk_gate")
    if gate:
        lines.append(f"risk_gate allow={gate.get('allow')} reason={gate.get('reason')} — {gate.get('detail')}")
    else:
        lines.append("risk_gate=n/a (not evaluated — see reason above)")
    limits = report.get("limits") or {}
    lines.append(
        "limits: budget={fire_budget_usdc} max_open={max_open_positions} max_capital={max_capital_usdc}".format(**limits)
    )
    egress = report.get("egress_ip")
    lines.append(
        f"egress={egress} ({report.get('egress_country')}) {report.get('egress_org') or ''}".rstrip()
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-1 live reconciliation (read-only)")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"report path (default {DEFAULT_OUT})")
    parser.add_argument("--timeout", type=int, default=25, help="per-request timeout seconds")
    args = parser.parse_args(argv)

    report = collect(timeout=args.timeout)

    out_path = Path(args.out)
    try:
        if out_path.parent != Path(""):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        wrote = f"wrote {out_path}"
    except OSError as exc:
        wrote = f"could not write {out_path}: {exc}"

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(human_summary(report))
        print(wrote)

    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
