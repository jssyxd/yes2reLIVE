#!/usr/bin/env python3
"""15-minute recorder for the yes2re paper deployment (this folder).

Every 15 minutes (cron) it appends:
  - one run-status row     -> data/yes2re_status.csv
  - human log line         -> data/yes2re_run.log
  - new trade/bug events   -> data/trades.csv + data/yes2re_run.log

Stdlib only. Safe to run concurrently (O_APPEND single lines).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURSOR = ROOT / "data" / ".recorder_cursor"


def load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main() -> int:
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = load(ROOT / "config" / "yes2re_reversal.json")
    health = load(ROOT / cfg.get("health_path", "data/yes2re_health.json"))
    state = load(ROOT / cfg.get("state_path", "data/yes2re_state.json"))
    log_path = ROOT / cfg.get("log_path", "data/yes2re_events.jsonl")
    feed = health.get("feed") or {}
    rules = feed.get("rules") or {}
    metar = feed.get("metar") or {}
    books = feed.get("books") or {}
    ws = feed.get("websocket_market") or {}
    positions = state.get("positions") or {}
    open_pos = sum(1 for p in positions.values() if not p.get("settled"))

    # ---- 1) status CSV row ----
    status_csv = data_dir / "yes2re_status.csv"
    if not status_csv.exists():
        status_csv.write_text(
            "ts_utc,armed_count,fired_count,open_positions,entry_count,"
            "capital_initial_usdc,remaining_capital_usdc,debit_usdc,"
            "rules_count,rules_failures,metar_ok,books_cached,ws_connected,ok\n",
            encoding="utf-8",
        )
    row = (
        f"{now_utc()},{health.get('armed_count')},{health.get('fired_count')},{open_pos},"
        f"{state.get('entry_count', 0)},{health.get('capital_initial_usdc')},"
        f"{health.get('remaining_capital_usdc')},{health.get('debit_usdc')},"
        f"{rules.get('count')},\"{json.dumps(rules.get('failures') or {}, ensure_ascii=False)}\","
        f"{metar.get('cities_ok')},{books.get('cached_tokens')},{ws.get('connected')},"
        f"{health.get('ok')}\n"
    )
    append(status_csv, row)

    # ---- 2) incremental events since last cursor ----
    offset = 0
    if CURSOR.exists():
        try:
            offset = int(CURSOR.read_text().strip() or 0)
        except ValueError:
            offset = 0
    events: list[dict] = []
    if log_path.exists():
        size = log_path.stat().st_size
        if offset > size:
            offset = 0
        with log_path.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        CURSOR.write_text(str(size))

    # ---- 3) trades.csv (append-only trade rows) ----
    trades_csv = data_dir / "trades.csv"
    if not trades_csv.exists():
        trades_csv.write_text(
            "ts_utc,type,key,city,icao,direction,jump,ref_source,leg,shares,cost_usdc,avg_price,details\n",
            encoding="utf-8",
        )

    def esc(v):
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"' if ("," in s or '"' in s or "\n" in s) else s

    log_file = data_dir / "yes2re_run.log"
    run_lines = [
        f"[{now_utc()}] STATUS armed={health.get('armed_count')} fired={health.get('fired_count')} "
        f"open={open_pos} entries={state.get('entry_count', 0)} "
        f"capital_initial={health.get('capital_initial_usdc')} remaining={health.get('remaining_capital_usdc')} "
        f"rules={rules.get('count')} rules_fail={json.dumps(rules.get('failures') or {}, ensure_ascii=False)} "
        f"metar_ok={metar.get('cities_ok')} books_cached={books.get('cached_tokens')} "
        f"ws_connected={ws.get('connected')} ok={health.get('ok')}"
    ]

    for ev in events:
        t = ev.get("type")
        if t in ("fire", "fire_attempt"):
            fills = json.dumps(ev.get("fills") or {}, ensure_ascii=False)
            for leg in (ev.get("fills") or {}):
                fl = (ev.get("fills") or {}).get(leg) or {}
                append(trades_csv, (
                    f"{now_utc()},fire,{esc(ev.get('key'))},{esc(ev.get('city_id'))},{esc(ev.get('icao'))},"
                    f"{esc(ev.get('direction'))},{esc(ev.get('jump'))},{esc(ev.get('ref_source'))},"
                    f"{esc(leg)},{esc(fl.get('shares'))},{esc(fl.get('cost'))},{esc(fl.get('avg'))},{esc(fills)}"
                ))
            run_lines.append(
                f"[{now_utc()}] TRADE fire key={ev.get('key')} jump={ev.get('jump')} "
                f"ref_source={ev.get('ref_source')} fills={fills}"
            )
        elif t == "position_settled":
            append(trades_csv, (
                f"{now_utc()},settle,{esc(ev.get('position_key'))},,"
                f",,,,,{esc(ev.get('leg'))},{esc(ev.get('shares'))},{esc(ev.get('payout_credit_usdc'))},,"
                f"{esc(json.dumps(ev, ensure_ascii=False, default=str))}"
            ))
            run_lines.append(
                f"[{now_utc()}] SETTLE key={ev.get('position_key')} leg={ev.get('leg')} "
                f"won={ev.get('leg_won')} payout={ev.get('payout_credit_usdc')}"
            )
        elif t in ("cycle_error", "book_fetch_failed", "metar_fetch_failed", "settle_failed", "ws_start_failed"):
            run_lines.append(f"[{now_utc()}] BUG {t} {ev.get('error')}")
    append(log_file, "\n".join(run_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
