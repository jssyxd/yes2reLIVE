#!/usr/bin/env python3
"""15-minute "why no fill" reverse-trace analysis for the yes2re paper runner.

Called by cron every 15 minutes (alongside yes2re_recorder.py). It answers,
in order:
  1. Did any trade signal (fire_attempt/fire) fire in the window?
  2. If a fire happened but filled 0: what did the FAK ladder actually decide?
     (abort_above_cap / no_book / abort_timeout / missing_token / send_fak)
  3. If no fire happened: layer-by-layer reverse trace —
     a. system health: cycle_error / book_fetch_failed / metar_fetch_failed /
        rules refresh failures / settle_failed / ws health / runner alive
     b. strategy: are sessions armed? skip reason distribution
        (no_reference_extreme / consensus_filter / hour_not_in_window /
        stale_obs / duplicate_obs_time / jump_gt_one)
     c. data freshness: METAR ages, book cache size/age
  4. Classify the dominant "blocking layer" and write a verdict line.

Output: appends human-readable analysis to data/analysis.log and writes the
latest machine-readable JSON to data/analysis_latest.json.

Stdlib only.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOW_S = 16 * 60  # ~one cron tick back (+buffer)


def load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_events(path: Path, since: str) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts_utc", "") >= since:
                out.append(e)
    return out


def fmt_ts(ts: str) -> str:
    return ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else str(ts)


def main() -> int:
    cfg = load(ROOT / "config" / "yes2re_reversal.json")
    health = load(ROOT / cfg.get("health_path", "data/yes2re_health.json"))
    state = load(ROOT / cfg.get("state_path", "data/yes2re_state.json"))
    log_path = ROOT / cfg.get("log_path", "data/yes2re_events.jsonl")
    data_dir = ROOT / "data"

    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=WINDOW_S)).isoformat().replace("+00:00", "Z")
    evs = read_events(log_path, cutoff)
    n = len(evs)
    if n == 0:
        # no events at all in window -> possible system stall
        verdict = "SYSTEM_STALL: no runner events in window — check process/health"
        line = f"[{now_utc()}] ANALYZE {verdict}"
        with (data_dir / "analysis.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        (data_dir / "analysis_latest.json").write_text(
            json.dumps({"ts_utc": now_utc(), "window_events": 0, "verdict": verdict}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(line)
        return 0

    types = Counter(e.get("type") for e in evs)
    fires = [e for e in evs if e.get("type") in ("fire_attempt", "fire")]
    arms = [e for e in evs if e.get("type") == "arm"]
    skips = [e for e in evs if e.get("type") in ("skip", "skip_yes", "disarm")]
    skip_reasons = Counter((e.get("type"), e.get("reason")) for e in skips)
    bugs = [e for e in evs if e.get("type") in
            ("cycle_error", "book_fetch_failed", "metar_fetch_failed", "settle_failed", "fire_insufficient_capital")]

    feed = health.get("feed") or {}
    rules = feed.get("rules") or {}
    metar = feed.get("metar") or {}
    books = feed.get("books") or {}
    ws = feed.get("websocket_market") or {}
    tree = state.get("weatherbotyes2re") or {}
    armed_keys = list((tree.get("armed") or {}).keys())
    fired_keys = list((tree.get("fired") or {}).keys())
    positions = state.get("positions") or {}
    open_pos = sum(1 for p in positions.values() if not p.get("settled"))

    lines: list[str] = []
    verdict = ""
    filled_any = False
    for f in fires:
        fills = f.get("fills") or {}
        ladder = f.get("ladder") or []
        leg_sum = {k: {"sh": v.get("shares"), "cost": v.get("cost")} for k, v in fills.items()}
        total_sh = sum(float((v or {}).get("shares") or 0) for v in fills.values())
        if total_sh > 0:
            filled_any = True
        lines.append(
            f"  FIRE {fmt_ts(f.get('ts_utc'))} key={f.get('key')} jump={f.get('jump')} "
            f"ref={f.get('ref_source')} fills={json.dumps(leg_sum, ensure_ascii=False)}"
        )
        if ladder:
            lines.append(f"    ladder[{len(ladder)}]:")
            for it in ladder[-8:]:
                lines.append(
                    f"      t={it.get('elapsed_ms')}ms {it.get('leg')} status={it.get('status')} "
                    f"ask={it.get('best_ask')} cap={it.get('cap')} limit={it.get('limit_price')}"
                )
        else:
            lines.append("    ladder: (none recorded — pre-ladder-audit code)")

    # --- reverse trace when nothing filled ---
    bug_counts = Counter((b.get("type"), str(b.get("error"))[:60]) for b in bugs)
    if bugs:
        lines.append("  SYSTEM bugs in window:")
        for (bt, be), c in bug_counts.items():
            lines.append(f"    {bt} x{c}  e.g. {be}")

    if fires and not filled_any:
        last = fires[-1]
        ladder = last.get("ladder") or []
        statuses = Counter(it.get("status") for it in ladder)
        if statuses:
            top = statuses.most_common(1)[0][0]
            if top == "no_book":
                # ladder found no resting ask in the in-memory cached book.
                # Two causes: (a) genuinely one-sided/no-ask market at that
                # moment, or (b) book cache missing the token (refresh/latency
                # bug). Distinguish by whether the token's book was cached at
                # fire time — when the whole ladder is no_book for every leg
                # the fire spec tokens are usually absent from cache.
                verdict = (
                    "NO_FILL_NO_BOOK: FAK found no resting ask in cached book for all ladder ticks. "
                    "Likely one-sided/no-ask market (YES already 0.999, nobody sells NO/YES) or token "
                    "missing from the book refresh set — see note below"
                )
                if any(str(it.get("note")) == "no_resting_ask_in_ladder" for it in ladder):
                    verdict += " | cache ladder present but ask=None (one-sided market)"
            elif top == "abort_above_cap":
                verdict = "NO_FILL_ABORT_ABOVE_CAP: market ask was above cap at every ladder tick (cap no longer a factor after 1.0 — means ask hit 1.0/not tradable, or book repriced)"
            elif top == "abort_timeout":
                verdict = "NO_FILL_ABORT_TIMEOUT: ladder exhausted 8s budget without fillable ask"
            elif top == "missing_token":
                verdict = "NO_FILL_MISSING_TOKEN: token id missing from fire spec (strategy/rule mapping bug)"
            else:
                verdict = f"NO_FILL ladder statuses={dict(statuses)}"
        else:
            verdict = "NO_FILL unknown: fire has no ladder detail (old event or pre-audit code)"
    elif not fires:
        # No trade signal in window: find the blocking layer
        if bug_counts:
            verdict = "NO_SIGNAL: system bugs present (" + ", ".join(f"{b}x{c}" for (b, _), c in bug_counts.items()) + ")"
        else:
            # 1) real break attempt blocked by consensus?
            cf = [e for e in skips if e.get("reason") == "consensus_filter"]
            if cf:
                last_cf = cf[-1]
                cons = last_cf.get("consensus") or {}
                why = cons.get("reason") or "?"
                jump = last_cf.get("jump")
                verdict = (
                    f"SIGNAL_BLOCKED_CONSENSUS: real break attempt jump={jump} on "
                    f"{last_cf.get('key')} was stopped by consensus filter ({why}, "
                    f"n_samples={cons.get('n_samples')})"
                )
                # strategy-order observation: consensus gate runs before the
                # jump policy, so oversized jumps (jump>1, market ref) are
                # reported as consensus_filter instead of the more accurate
                # jump_too_large_for_ref. Harmless (both block) but misleading
                # audit — flag it for the strategy owner.
                if jump is not None and int(jump) > 1:
                    lines.append(
                        f"  STRATEGY-NOTE: jump={jump}>1 was blocked by consensus_filter before "
                        "jump policy could classify it as jump_too_large_for_ref "
                        "(order: consensus gate precedes jump policy) — audit labels may mislead"
                    )
            elif not armed_keys and not fired_keys:
                verdict = "NO_SIGNAL: nothing armed — strategy never reached arm (check reference/consensus/METAR)"
            else:
                sr = {k: v for k, v in skip_reasons.items()}
                dom = max(((r, c) for (_, r), c in sr.items() if r not in ("duplicate_obs_time",)), default=(None, 0), key=lambda x: x[1])
                if dom[0]:
                    verdict = f"NO_SIGNAL: armed={len(armed_keys)} but dominant skip={dom[0]} x{dom[1]}"
                else:
                    verdict = f"NO_SIGNAL: armed={len(armed_keys)} only duplicate_obs_time skips (waiting for new METAR extreme)"
    else:
        verdict = f"FILLED: {sum(1 for f in fires if f.get('type')=='fire')} fire(s) with shares>0"

    # health snapshot line
    lines.insert(0, (
        f"  health: armed={len(armed_keys)} fired={len(fired_keys)} open={open_pos} "
        f"capital={health.get('remaining_capital_usdc')} rules={rules.get('count')} "
        f"rules_fail={json.dumps(rules.get('failures') or {}, ensure_ascii=False)} "
        f"metar_ok={metar.get('cities_ok')}/49 books={books.get('cached_tokens')} "
        f"ws={ws.get('connected')} evs_window={n}"
    ))
    if skip_reasons:
        lines.append("  skips: " + json.dumps({f"{k[0]}:{k[1]}": v for k, v in skip_reasons.most_common(8)}, ensure_ascii=False))

    body = "\n".join(lines)
    block = f"[{now_utc()}] ANALYZE -> {verdict}\n{body}"
    with (data_dir / "analysis.log").open("a", encoding="utf-8") as fh:
        fh.write(block + "\n")
    latest = {
        "ts_utc": now_utc(),
        "window_events": n,
        "event_types": dict(types),
        "fires": [{"ts": f.get("ts_utc"), "key": f.get("key"), "jump": f.get("jump"),
                   "fills": f.get("fills"), "ladder_statuses": dict(Counter(i.get("status") for i in (f.get("ladder") or [])))} for f in fires],
        "skip_reasons": {f"{k[0]}:{k[1]}": v for k, v in skip_reasons.items()},
        "bugs": dict(bug_counts),
        "armed_count": len(armed_keys),
        "verdict": verdict,
    }
    (data_dir / "analysis_latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
