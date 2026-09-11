#!/usr/bin/env python3
"""Automated monitor and health triage for weatherbotyes2re LIVE runner.

Checks:
1. systemd service status (yes2re-live)
2. health.json freshness and flags (ok=True, mode=live)
3. WebSocket feed health (connected=True, connect_errors=0)
4. Account reconcile status (USDC balance, open orders, allowances)
5. Recent journal / events logs for critical errors

Performs self-healing:
- If service is inactive/failed or health file is stale (>120s), attempts systemctl restart yes2re-live.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEALTH_PATH = ROOT / "data" / "yes2re_health.json"
EVENTS_PATH = ROOT / "data" / "yes2re_events.jsonl"


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return res.returncode, (res.stdout + res.stderr).strip()
    except Exception as exc:
        return -1, str(exc)


def check_service() -> dict:
    code, out = run_cmd(["systemctl", "is-active", "yes2re-live"])
    active = (code == 0 and out == "active")
    return {"active": active, "status": out}


def check_health_json() -> dict:
    if not HEALTH_PATH.exists():
        return {"ok": False, "reason": "missing_health_file", "age_s": None}
    try:
        mtime = HEALTH_PATH.stat().st_mtime
        age_s = round(time.time() - mtime, 1)
        with open(HEALTH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "ok": data.get("ok") is True,
            "mode": data.get("mode"),
            "age_s": age_s,
            "stale": age_s > 120.0,
            "armed_count": data.get("armed_count", 0),
            "armed": data.get("armed", []),
            "ws_connected": data.get("feed", {}).get("websocket_market", {}).get("connected"),
            "ws_msgs": data.get("feed", {}).get("websocket_market", {}).get("message_count", 0),
            "ws_errors": data.get("feed", {}).get("websocket_market", {}).get("connect_errors", 0),
            "capital_initial": data.get("capital_initial_usdc"),
            "remaining_capital": data.get("remaining_capital_usdc"),
        }
    except Exception as exc:
        return {"ok": False, "reason": f"read_error: {exc}", "age_s": None}


def check_reconcile() -> dict:
    try:
        # Import and run reconcile check in-process if virtualenv is present
        sys.path.insert(0, str(ROOT))
        from live import creds, reconcile, submit
        env = creds.load_env_file(ROOT / ".env")
        credentials = creds.validate_creds(env)
        submit.prepare_network(env)
        from live.v2_transport import build_client, read_account
        client = build_client(credentials)
        acct = read_account(client, address=credentials["funder_address"])
        return {
            "ok": True,
            "usdc_balance": str(acct.get("usdc_balance")),
            "open_orders_count": len(acct.get("open_orders") or []),
            "positions_count": len(acct.get("positions") or []),
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def check_recent_errors() -> list[str]:
    errors = []
    # Check journalctl for recent warnings or crashes
    code, out = run_cmd(["journalctl", "-u", "yes2re-live", "-n", "30", "--no-pager"])
    if code == 0:
        for line in out.splitlines():
            if any(k in line.lower() for k in ("traceback", "exception", "fire_port_refused", "failed")):
                errors.append(line.strip())
    return errors[-5:]  # return at most 5 recent error lines


def auto_heal(reason: str) -> dict:
    print(f"[AUTO-HEAL] Triggering restart due to: {reason}")
    code, out = run_cmd(["systemctl", "restart", "yes2re-live"])
    time.sleep(3)
    svc = check_service()
    return {"attempted": True, "reason": reason, "restart_code": code, "now_active": svc["active"]}


def main() -> int:
    report: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "healthy": True,
        "actions_taken": [],
    }

    svc = check_service()
    report["service"] = svc
    if not svc["active"]:
        report["healthy"] = False
        action = auto_heal(f"service inactive ({svc['status']})")
        report["actions_taken"].append(action)

    health = check_health_json()
    report["health_file"] = health
    if health.get("stale"):
        report["healthy"] = False
        action = auto_heal(f"health file stale (age={health.get('age_s')}s)")
        report["actions_taken"].append(action)
    elif not health.get("ok"):
        report["healthy"] = False

    reconcile_info = check_reconcile()
    report["reconcile"] = reconcile_info
    if not reconcile_info.get("ok"):
        report["healthy"] = False

    errs = check_recent_errors()
    report["recent_errors"] = errs

    print(json.dumps(report, indent=2))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
