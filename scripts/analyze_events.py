#!/usr/bin/env python3
"""Analyze why no orders were fired in data/yes2re_events.jsonl."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = ROOT / "data" / "yes2re_events.jsonl"

def main():
    event_types = Counter()
    skip_reasons = Counter()
    consensus_reasons = Counter()
    arm_keys = Counter()
    firings = []
    
    total_lines = 0
    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            total_lines += 1
            try:
                ev = json.loads(line)
                t = ev.get("type")
                event_types[t] += 1
                if t == "skip":
                    r = ev.get("reason")
                    skip_reasons[r] += 1
                    if r == "consensus_filter":
                        c = ev.get("consensus") or {}
                        consensus_reasons[c.get("reason")] += 1
                elif t == "arm":
                    arm_keys[ev.get("key")] += 1
                elif t in ("fire", "fire_attempt", "fire_port_refused"):
                    firings.append(ev)
            except Exception:
                pass

    print(f"Total events processed: {total_lines}")
    print("\n--- Event Distribution ---")
    for k, v in event_types.most_common():
        print(f"  {k:25s}: {v}")

    print("\n--- Skip Reasons ---")
    for k, v in skip_reasons.most_common():
        print(f"  {k:25s}: {v}")

    print("\n--- Consensus Filter Breakdown ---")
    for k, v in consensus_reasons.most_common():
        print(f"  {k:25s}: {v}")

    print("\n--- Top 10 Armed Sessions ---")
    for k, v in arm_keys.most_common(10):
        print(f"  {k:30s}: {v}")

    print("\n--- Candidate Breaches (Consensus Filtered or Non-Jump-1) ---")
    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
                if ev.get("type") == "skip" and ev.get("reason") in (
                    "consensus_filter", "jump_must_be_one", "already_fired",
                    "break_not_confirmed", "break_without_arm"
                ):
                    key = ev.get("key")
                    reason = ev.get("reason")
                    jump = ev.get("jump")
                    cons = (ev.get("consensus") or {}).get("reason")
                    ts = ev.get("ts_utc")
                    print(f"  {ts} | {key:30s} | {reason:20s} | jump={str(jump):5s} | cons={str(cons)}")
            except Exception:
                pass

    print(f"\n--- Firing Events ({len(firings)}) ---")
    for f in firings:
        t = f.get("type")
        k = f.get("key")
        ts = f.get("ts_utc")
        print(f"  [{ts}] {t:12s} {k}")
        if t == "fire":
            fills = f.get("fills", {})
            print(f"    fills: {fills}")
            ladder = f.get("ladder", [])
            for rung in ladder:
                r_leg = rung.get("leg")
                r_st = rung.get("status")
                r_note = rung.get("note")
                r_ask = rung.get("best_ask")
                r_cap = rung.get("cap")
                r_fl = rung.get("floor")
                r_ms = rung.get("elapsed_ms")
                print(f"    rung @{r_ms}ms: leg={r_leg} status={r_st} ask={r_ask} floor={r_fl} cap={r_cap} note={r_note}")


    STATE_PATH = ROOT / "data" / "yes2re_state.json"
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as sf:
            st = json.load(sf)
        fired = st.get("weatherbotyes2re", {}).get("fired", {})
        print(f"\n--- Fired Keys in State Blob ({len(fired)}) ---")
        for k, v in fired.items():
            print(f"  {k:35s}: {v}")
        armed = st.get("weatherbotyes2re", {}).get("armed", {})
        print(f"\n--- Currently Armed in State Blob ({len(armed)}) ---")
        HEALTH_PATH = ROOT / "data" / "yes2re_health.json"
        metar_tel = {}
        if HEALTH_PATH.exists():
            with open(HEALTH_PATH, "r", encoding="utf-8") as hf:
                metar_tel = json.load(hf).get("feed", {}).get("metar", {}).get("per_icao", {})

        # Load contract_cities.json to map city -> icao
        CITIES_PATH = ROOT / "config" / "contract_cities.json"
        city_icao = {}
        if CITIES_PATH.exists():
            with open(CITIES_PATH, "r", encoding="utf-8") as cf:
                city_icao = {c["city_id"]: c.get("icao") for c in json.load(cf)}

        for k, v in armed.items():
            cid = k.split("|")[0]
            direction = k.split("|")[2] if len(k.split("|")) > 2 else ""
            icao = city_icao.get(cid, "")
            obs = metar_tel.get(icao, {})
            temp_c = obs.get("temp_c")
            ref = v.get("ref_extreme")
            icao_str = str(icao or "")
            print(f"  {k:30s} | icao={icao_str:4s} | current_temp={str(temp_c):5s}°C | ref={str(ref):5s} | dir={direction}")

        pos = st.get("positions", {})
        print(f"\n--- Open Positions in State Blob ({len(pos)}) ---")
        for k, v in pos.items():
            print(f"  {k}: {json.dumps(v, indent=2)}")

    print("\n--- Non-duplicate Events for Shenzhen ---")
    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if "shenzhen|2026-09-11|high" in line and "duplicate" not in line:
                print(f"  {line.strip()[:300]}")


if __name__ == "__main__":
    main()





