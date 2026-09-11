#!/usr/bin/env python3
"""Standalone paper simulator for weatherbotyes2re."""
from __future__ import annotations
import argparse, json, time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from re_execution import paper_match_fak, plan_fire_cycle, size_legs
from reversal_strategy import (
    HIGH_FIRE_LOCAL_HOUR_END,
    HIGH_FIRE_LOCAL_START,
    LOW_FIRE_LOCAL_END,
    LOW_FIRE_LOCAL_START,
    hour_ok,
    iso_utc,
    maybe_arm_or_fire,
    ensure_re_state,
    prune_stale_sessions,
    session_key,
)
from consensus_tracker import ConsensusTracker
TZ = "Asia/Shanghai"

PAPER_CFG = {
    "require_consensus_filter": True,
    "consensus_min_samples": 3,
    "consensus_window_seconds": 7200,
    "consensus_min_lead": "0.01",
    "allow_market_consensus_reference": True,
    "no_max_ask": "0.65",
    "yes_max_ask": "0.48",
}


def make_buckets():
    out = []
    for t in range(28, 36):
        out.append({"bucket_id": f"h{t}", "lo": float(t), "hi": float(t+1), "no_token_id": f"NO-{t}", "yes_token_id": f"YES-{t}"})
    return out


def make_city():
    return {"city_id": "shanghai", "icao": "ZSPD", "timezone": TZ}


def make_book(ask, depth=8.0, tick=0.01, levels=4):
    asks = []
    px = Decimal(str(ask)); step = Decimal(str(tick)); sz = Decimal(str(depth))
    for i in range(levels):
        asks.append({"price": str(px + step*i), "size": str(sz/(i+1))})
    return {"best_ask": asks[0]["price"], "tick_size": str(tick), "asks": asks}


def seed_consensus_rank1(tracker: ConsensusTracker, city, date, direction, top_bucket_id="h31", now=None):
    now = now or datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    for i in range(40):
        t = now - timedelta(minutes=40 - i)
        tracker.record(city["city_id"], date, direction, top_bucket_id, best_ask=0.55, best_bid=0.52, ask_depth=10, now_utc=t)
        for bid, ask in (("h30", 0.22), ("h32", 0.18), ("h33", 0.12)):
            tracker.record(city["city_id"], date, direction, bid, best_ask=ask, best_bid=max(0.01, ask - 0.03), ask_depth=3, now_utc=t)


def apply_scramble(books, broken_no, new_yes, step):
    no_book = books.get(broken_no)
    if no_book and no_book.get("best_ask"):
        books[broken_no] = make_book(min(float(no_book["best_ask"])+0.04*step, 0.90), depth=max(1.0, 6.0-step*2))
    if new_yes and new_yes in books and books[new_yes].get("best_ask"):
        books[new_yes] = make_book(min(float(books[new_yes]["best_ask"])+0.06*step, 0.90), depth=max(0.5, 4.0-step*1.5))


def run_fire_window(fire, books, budget_usdc, now, scramble=True):
    remaining = size_legs(fire, budget_usdc)
    fills = {name: {"shares": Decimal("0"), "cost": Decimal("0")} for name in remaining}
    log = []
    for step, elapsed in [(0,0),(1,1600),(2,4100),(3,8100)]:
        if scramble and step>0:
            apply_scramble(books, fire.get("broken_no_token"), fire.get("new_yes_token"), step)
        for intent in plan_fire_cycle(fire, books, remaining, now+timedelta(milliseconds=elapsed), elapsed):
            log.append(intent)
            if intent.get("status") != "send_fak":
                continue
            match = paper_match_fak(books.get(intent["token_id"]) or {}, Decimal(intent["limit_price"]), Decimal(intent["shares"]))
            fills[intent["leg"]]["shares"] += match["filled_shares"]
            fills[intent["leg"]]["cost"] += match["cost"]
            remaining[intent["leg"]] = match["unfilled"]
            intent["fill"] = {"filled": str(match["filled_shares"]), "avg": str(match["avg_price"]) if match["avg_price"] is not None else None, "unfilled": str(match["unfilled"])}
    return fills, remaining, log


def scenario_one_bucket_fill():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(window_seconds=7200, min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    books={"NO-31": make_book(0.42, depth=10), "YES-32": make_book(0.28, depth=8)}
    for t in range(28, 36):
        books[f"YES-{t}"] = make_book(0.55 if t == 31 else 0.15, depth=5)
    actions=[]
    for temp, offset in ((30.2,0),(30.8,30),(31.4,60),(32.1,90)):
        t=now+timedelta(seconds=offset)
        actions.extend(maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, temp, t, t, books, PAPER_CFG, tracker))
    fire=next((a for a in actions if a.get("action_type")=="re_fire"), None)
    if fire is None:
        return {"name":"one_bucket_fill","actions":[a.get("action_type") for a in actions],"ok":False,"error":"no_fire","reasons":[a.get("reason") for a in actions]}
    fills, leftover, log = run_fire_window(fire, books, Decimal("20"), now+timedelta(seconds=90))
    return {"name":"one_bucket_fill","actions":[a["action_type"] for a in actions],"fire_jump":fire["jump"],"fills":{k:{kk:str(vv) for kk,vv in v.items()} for k,v in fills.items()},"leftover":{k:str(v) for k,v in leftover.items()},"send_faks":sum(1 for x in log if x.get("status")=="send_fak"),"ok":fills["buy_no_broken"]["shares"]>0}


def scenario_jump_must_be_one():
    # T4: jump != 1 is noise — skip the WHOLE basket, no YES-only multi-bucket
    # fire. Arm first (temp inside the reference bucket), then break by 2
    # buckets -> re_skip jump_must_be_one, no re_fire.
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 30.5, now, now, {}, PAPER_CFG, tracker)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 33.2, now+timedelta(minutes=1), now+timedelta(minutes=1), {}, PAPER_CFG, tracker)
    types=[a["action_type"] for a in actions]
    return {"name":"jump_must_be_one","types":types,
            "ok":"re_fire" not in types and any(a.get("reason")=="jump_must_be_one" for a in actions)}


def scenario_stale_market_date():
    # 2026-09-06 regression: chicago|2026-09-05|low at 05:00Z (local midnight
    # rollover). prune deletes yesterday's fired marker every cycle, but the
    # TTL'd rules cache still lists the 09-05 rule — the same breach obs
    # re-fires every cycle (~$230/min burn). Guard: market_local_date that is
    # no longer the city's local today must skip, never arm/fire.
    state={}
    city={"city_id":"chicago","icao":"KORD","timezone":"America/Chicago"}
    buckets=make_buckets()
    # 2026-09-06 05:00Z CDT = 00:00 (CDT=UTC-5),
    # so local date is ALREADY 09-06 while the market rule is dated 09-05.
    now=datetime(2026,9,6,5,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-05", "low", "h30", now)
    # low direction breach: obs 20.0 vs ref 21.0 (jump into h20)
    actions=maybe_arm_or_fire(state, city, "2026-09-05", "low", buckets, 21.0, 20.0, now, now, {}, PAPER_CFG, tracker)
    types=[a["action_type"] for a in actions]
    return {"name":"stale_market_date","types":types,
            "ok":"re_fire" not in types and "re_arm" not in types and
                 any(a.get("reason")=="stale_market_date" for a in actions)}


def scenario_tz_unresolvable_fail_closed():
    # 2026-09-06 guard hardening: the date guard must FAIL CLOSED when the
    # city's timezone is missing or unparseable — "today" cannot be verified,
    # so arm/fire are refused. (The hotfix's except branch set local_today =
    # market_local_date, silently disabling the guard — a breach would fire
    # on any city whose tz entry broke, re-opening the stale-date burn.)
    state={}
    city={"city_id":"chicago","icao":"KORD","timezone":"Not/AZone"}  # unparseable tz
    buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    types=[a["action_type"] for a in actions]
    reasons=[a.get("reason") for a in actions]
    guards=[a.get("guard") for a in actions]
    return {"name":"tz_unresolvable_fail_closed","types":types,"reasons":reasons,"guards":guards,
            "ok":"re_arm" not in types and "re_fire" not in types and
                 any(a.get("reason")=="stale_market_date" for a in actions)}


def scenario_missing_tz_fail_closed():
    # Same fail-closed contract for a city with NO timezone field at all.
    state={}
    city={"city_id":"chicago","icao":"KORD"}  # timezone missing
    buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    types=[a["action_type"] for a in actions]
    return {"name":"missing_tz_fail_closed","types":types,
            "ok":"re_arm" not in types and "re_fire" not in types and
                 any(a.get("reason")=="stale_market_date" for a in actions)}


def scenario_prune_keeps_open_fired():
    # 2026-09-06 root fix (part 2): prune must NOT delete a stale-date fired
    # marker while that session's own paper position is still open — the marker
    # is the one-fire dedupe credential and a stale rule from the TTL'd cache
    # would otherwise re-fire the same breach obs every cycle. Once the
    # position settles the marker becomes prunable again.
    city = make_city()
    city["timezone"] = "UTC"
    now = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)  # local date 2026-09-05
    stale_open = "shanghai|2026-09-04|high"     # stale date, position OPEN  -> keep fired
    stale_done = "shanghai|2026-09-04|low"      # stale date, position settled -> prune
    today_key = "shanghai|2026-09-05|high"      # today -> keep
    state = {
        "positions": {
            stale_open: {"settled": False},
            stale_done: {"settled": True},
            today_key: {"settled": False},
        },
        "weatherbotyes2re": {
            "armed": {},
            "fired": {stale_open: {"status": "fired"},
                      stale_done: {"status": "fired"},
                      today_key: {"status": "fired"}},
            "running_extremes": {}, "last_obs_time": {},
            "taf_forecasts": {}, "last_obs": {},
        },
    }
    removed = prune_stale_sessions(state, [city], now)
    fired = state["weatherbotyes2re"]["fired"]
    ok = (removed == 1 and stale_open in fired and today_key in fired
          and stale_done not in fired)
    return {"name": "prune_keeps_open_fired", "removed": removed,
            "kept_fired": sorted(fired), "ok": ok}


def scenario_stale_obs_no_fire():
    # 2026-09-03 window semantics: stale = obs older than 90 min (was 180 s).
    # 10-min-old obs is a NORMAL hourly-cadence gap and must be able to fire.
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now-timedelta(minutes=100), now, {}, PAPER_CFG, tracker)
    return {"name":"stale_obs_no_fire","types":[a["action_type"] for a in actions],"ok":all(a.get("action_type")!="re_fire" for a in actions)}


def scenario_morning_skip():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,3,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    return {"name":"morning_skip","types":[a["action_type"] for a in actions],"ok":all(a.get("action_type")!="re_fire" for a in actions)}


def scenario_cap_abort():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    books={"NO-31": make_book(0.80, depth=10), "YES-32": make_book(0.70, depth=8)}
    maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 30.5, now, now, {}, PAPER_CFG, tracker)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now+timedelta(minutes=1), now+timedelta(minutes=1), books, PAPER_CFG, tracker)
    fire=next(a for a in actions if a["action_type"]=="re_fire")
    fills, leftover, log = run_fire_window(fire, books, Decimal("20"), now, scramble=False)
    aborted=[x for x in log if x.get("status")=="abort_above_cap"]
    return {"name":"cap_abort_no_chase","aborted":len(aborted),"filled_no":str(fills.get("buy_no_broken",{}).get("shares",0)),"ok":fills.get("buy_no_broken",{}).get("shares",0)==0 and len(aborted)>=1}


def scenario_no_double_fire():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 30.5, now, now, {}, PAPER_CFG, tracker)
    a1=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now+timedelta(minutes=1), now+timedelta(minutes=1), {}, PAPER_CFG, tracker)
    a2=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.4, now+timedelta(minutes=2), now+timedelta(minutes=2), {}, PAPER_CFG, tracker)
    fires=[a for a in a1+a2 if a.get("action_type")=="re_fire"]
    return {"name":"no_double_fire","fires":len(fires),"second":[a.get("reason") for a in a2],"ok":len(fires)==1 and a2[0].get("reason")=="already_fired"}


def scenario_consensus_blocks_non_leader():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h32", now)
    maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 30.5, now, now, {}, PAPER_CFG, tracker)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now+timedelta(minutes=1), now+timedelta(minutes=1), {}, PAPER_CFG, tracker)
    return {
        "name": "consensus_blocks_non_leader",
        "types": [a.get("action_type") for a in actions],
        "reasons": [a.get("reason") for a in actions],
        "ok": all(a.get("action_type") != "re_fire" for a in actions)
        and any(a.get("reason") == "consensus_filter" for a in actions),
    }


def scenario_prune_stale():
    # Cross-day carryover / unknown-city / low-zombie cleanup of session caches.
    city = make_city()
    city["timezone"] = "UTC"  # scenario-local override; module TZ constant untouched
    now = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)  # local date 2026-09-05, local hour 15
    stale = {
        "armed": {
            "shanghai|2026-09-04|high": {"status": "armed"},  # yesterday -> prune
            "shanghai|2026-09-05|low": {"status": "armed"},   # today low zombie (hour 15 > 10) -> prune
            "atlantis|2026-09-05|high": {"status": "armed"},  # unknown city -> prune
        },
        "fired": {"shanghai|2026-09-04|high": {"status": "fired"}},  # yesterday -> prune
        "running_extremes": {"shanghai|2026-09-04|high": {"value": 32.0}},  # yesterday -> prune
        "last_obs_time": {"shanghai|2026-09-04|high": "2026-09-04T08:00:00Z"},  # yesterday -> prune
    }
    keep = {
        "armed": {
            "shanghai|2026-09-05|high": {"status": "armed"},  # today high -> keep
            "not-a-session-key": {"status": "armed"},          # malformed (not 3 | parts) -> keep
        },
        "fired": {"shanghai|2026-09-05|high": {"status": "fired"}},  # today -> keep
        "running_extremes": {},
        "last_obs_time": {},
    }
    state = {"weatherbotyes2re": {"taf_forecasts": {"shanghai|2026-09-05|high": {"keep": True}},
                                  "last_obs": {"shanghai|2026-09-05|high": {"keep": True}}}}
    for sec, kv in stale.items():
        state["weatherbotyes2re"][sec] = dict(kv)
    for sec, kv in keep.items():
        state["weatherbotyes2re"][sec].update(kv)
    removed = prune_stale_sessions(state, [city], now)
    tree = state["weatherbotyes2re"]
    stale_left = [(sec, k) for sec, kv in stale.items() for k in kv if k in tree[sec]]
    kept_missing = [k for k in ("shanghai|2026-09-05|high", "not-a-session-key") if k not in tree["armed"]] + \
        [k for k in ("shanghai|2026-09-05|high",) if k not in tree["fired"]]
    untouched = {"taf_forecasts": "shanghai|2026-09-05|high" in tree["taf_forecasts"],
                 "last_obs": "shanghai|2026-09-05|high" in tree["last_obs"]}
    ok = removed == 6 and not stale_left and not kept_missing and all(untouched.values())
    return {"name": "prune_stale", "removed": removed, "ok": ok,
            "stale_left": stale_left, "kept_missing": kept_missing, "untouched": untouched}


def scenario_open_position_blocks_refire():
    # T1 lock 2: an open (unsettled) paper position must block re-fire even if
    # the fired marker was lost (simulate prune having dropped it).
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    state["positions"]={"shanghai|2026-09-01|high": {"settled": False, "fires_at_utc": now.isoformat()}}
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    types=[a.get("action_type") for a in actions]
    return {"name":"open_position_blocks_refire","types":types,
            "ok":"re_fire" not in types and any(a.get("reason")=="open_position_already_exists" for a in actions)}


def scenario_same_obs_blocks_refire():
    # T1 lock 3: the exact obs_time that already fired must be rejected with
    # duplicate_obs_fired (read side of last_fire_obs).
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    tree=ensure_re_state(state)
    tree["last_fire_obs"]["shanghai|2026-09-01|high"]=iso_utc(now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    return {"name":"same_obs_blocks_refire","reasons":[a.get("reason") for a in actions],
            "ok":any(a.get("reason")=="duplicate_obs_fired" for a in actions)}


def scenario_restart_replay_fires_once():
    # T2 restart replay: state with ever_armed but no armed marker and no fired
    # (restart persisted pre-fire state) must still allow a single legit fire.
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    tree=ensure_re_state(state)
    tree["ever_armed"]["shanghai|2026-09-01|high"]=iso_utc(now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    fires=[a for a in actions if a.get("action_type")=="re_fire"]
    return {"name":"restart_replay_fires_once","fires":len(fires),
            "ok":len(fires)==1}


def scenario_unarmed_break_blocked():
    # T2 negative: a jump=1 break on a never-armed session is IDLE -> FIRED and
    # must be rejected with break_without_arm.
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {}, PAPER_CFG, tracker)
    types=[a.get("action_type") for a in actions]
    return {"name":"unarmed_break_blocked","types":types,
            "ok":any(a.get("reason")=="break_without_arm" for a in actions) and "re_fire" not in types}


def scenario_market_ref_fire_allowed():
    # 2026-09-08 operator decision: market-rank-1 reference MAY fire when the
    # stable consensus bucket is broken by a fresh METAR extreme (Paris 27°C /
    # Milan 32°C won exactly this way). No TAF needed. allow_market_ref_fire
    # defaults True; set false to fail closed again.
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)  # Shanghai 16:00 local, in window (12-18)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    # no TAF (taf_extreme=None): reference = market rank-1 mid (h31 = 31.5)
    a0=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, None, 31.0, now, now, {}, PAPER_CFG, tracker)
    t2=now+timedelta(minutes=2)
    a1=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, None, 32.4, t2, t2, {}, PAPER_CFG, tracker)
    tree=ensure_re_state(state)
    key="shanghai|2026-09-01|high"
    fires=[x for x in a1 if x.get("action_type")=="re_fire"]
    ok = (
        any(x.get("action_type")=="re_arm" for x in a0)
        and len(fires)==1
        and fires[0].get("ref_source")=="market_rank1"
        and key in tree["fired"]                    # fired marker written
        and key not in tree["armed"]                # armed cleared after fire
    )
    return {"name":"market_ref_fire_allowed",
            "reasons":[x.get("reason") for x in a1],
            "fires":len(fires),
            "ref_source":fires[0].get("ref_source") if fires else None,
            "ok":ok}


def scenario_high_late_evening_skip():
    # HIGH window is 12..18 local (operator 2026-09-08). hour 18 is allowed
    # (a late-afternoon peak tick); hour 19+ is off-window drift and must be
    # skipped. LOW window is 0..9.
    w = lambda h: hour_ok(h, 0, 0, 23, 0, 9)  # unused; keep clarity below
    win = (
        hour_ok("high", 12, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
        and hour_ok("high", 18, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
        and not hour_ok("high", 11, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
        and not hour_ok("high", 19, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
        and hour_ok("low", 0, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
        and hour_ok("low", 9, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
        and not hour_ok("low", 10, HIGH_FIRE_LOCAL_START, HIGH_FIRE_LOCAL_HOUR_END, LOW_FIRE_LOCAL_START, LOW_FIRE_LOCAL_END)
    )
    # end-to-end: an obs at local hour 19 (11:00Z Shanghai) on an armed TAF
    # session is rejected with hour_not_in_window, nothing fired.
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)  # Shanghai 16:00 local (in window)
    tracker = ConsensusTracker(min_samples=3)
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", now)
    a0=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 30.5, now, now, {}, PAPER_CFG, tracker)
    late=datetime(2026,9,1,11,0,tzinfo=timezone.utc)  # 19:00 Shanghai local -> hour 19
    a1=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, late, late, {}, PAPER_CFG, tracker)
    tree=ensure_re_state(state)
    blocked = (
        any(x.get("reason")=="hour_not_in_window" for x in a1)
        and not any(x.get("action_type")=="re_fire" for x in a1)
        and not tree["fired"]
    )
    return {"name":"high_late_evening_skip",
            "windows":win,
            "armed_first":any(x.get("action_type")=="re_arm" for x in a0),
            "reasons":[x.get("reason") for x in a1],
            "ok":win and blocked}


def run_scenarios():
    results=[]; failed=0
    for fn in (
        scenario_one_bucket_fill,
        scenario_jump_must_be_one,
        scenario_stale_obs_no_fire,
        scenario_morning_skip,
        scenario_cap_abort,
        scenario_no_double_fire,
        scenario_consensus_blocks_non_leader,
        scenario_open_position_blocks_refire,
        scenario_same_obs_blocks_refire,
        scenario_restart_replay_fires_once,
        scenario_unarmed_break_blocked,
        scenario_prune_stale,
        scenario_prune_keeps_open_fired,
        scenario_stale_market_date,
        scenario_tz_unresolvable_fail_closed,
        scenario_missing_tz_fail_closed,
        scenario_market_ref_fire_allowed,
        scenario_high_late_evening_skip,
        scenario_refire_closes_old_yes,
        scenario_refire_close_no_bid,
        scenario_refire_below_floor,
        scenario_refire_above_cap,
        scenario_refire_out_of_window,
        scenario_refire_persists_across_restart,
    ):
        r=fn(); results.append(r)
        if not r.get("ok"): failed += 1
    return results, failed


def live_loop(seconds, budget, tick):
    state={}; city=make_city(); buckets=make_buckets()
    tracker = ConsensusTracker(min_samples=3)
    base_local=datetime(2026,9,1,16,0,tzinfo=ZoneInfo(TZ))
    seed_consensus_rank1(tracker, city, "2026-09-01", "high", "h31", base_local.astimezone(timezone.utc))
    books={"NO-31": make_book(0.40, depth=12), "YES-32": make_book(0.30, depth=9)}
    for t in range(28, 36):
        books[f"YES-{t}"] = make_book(0.55 if t == 31 else 0.15, depth=5)
    journal=[]; fired_event=None; fill_result=None; temps=[]
    t0=time.time(); end=t0+seconds; step_i=0
    while time.time()<end:
        elapsed=time.time()-t0; frac=elapsed/max(seconds,1)
        temp = 30.4 if frac<0.25 else 30.9 if frac<0.45 else 31.2 if frac<0.55 else 32.15
        synth=(base_local+timedelta(seconds=elapsed)).astimezone(timezone.utc)
        actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, temp, synth, synth, books, PAPER_CFG, tracker)
        for a in actions:
            journal.append({"t":round(elapsed,2),"temp":temp,"action_type":a.get("action_type"),"reason":a.get("reason")})
            if a.get("action_type")=="re_fire" and fired_event is None:
                fired_event=a
                fill_result=run_fire_window(a, deepcopy(books), budget, synth, scramble=True)
        step_i += 1; temps.append(temp); time.sleep(tick)
    fills, leftover, log = fill_result if fill_result else ({}, {}, [])
    return {"seconds":seconds,"steps":step_i,"last_temp":temps[-1] if temps else None,"journal_types":[j.get("action_type") for j in journal],"fired":fired_event is not None,"jump":(fired_event or {}).get("jump"),"fills":{k:{kk:str(vv) for kk,vv in v.items()} for k,v in fills.items()} if fills else {},"leftover":{k:str(v) for k,v in leftover.items()} if leftover else {},"fak_intents":[x.get("status") for x in log],"positions":ensure_re_state(state)["fired"]}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--budget", type=float, default=20.0)
    p.add_argument("--tick", type=float, default=1.0)
    p.add_argument("--scenarios-only", action="store_true")
    args=p.parse_args()
    scenarios, failed = run_scenarios()
    out={"scenarios":scenarios,"scenario_failures":failed}
    if not args.scenarios_only:
        out["live_loop"]=live_loop(args.seconds, Decimal(str(args.budget)), args.tick)
    print(json.dumps(out, indent=2, default=str))
    if failed: raise SystemExit(1)


def make_fire_position(key, fire, fills, budget_usdc, now):
    """Position record shaped like ``_r_cycle._paper_fire`` output (used to
    seed fire #1's ledger row / build fire #2's row for the 追火 scenarios)."""
    legs = []
    for fleg in fire.get("legs", []):
        name = str(fleg.get("leg"))
        fl = fills.get(name) or {}
        shares = Decimal(str(fl.get("shares") or 0))
        if shares <= 0:
            continue
        legs.append({
            "leg": name,
            "token_id": fleg.get("token_id"),
            "side": fleg.get("side"),
            "outcome": fleg.get("outcome"),
            "cap": fleg.get("cap"),
            "notional_pct": fleg.get("notional_pct"),
            "cost_usdc": str(fl.get("cost") or 0),
            "shares": str(shares),
            "avg_price": str(fl.get("avg_price")) if fl.get("avg_price") is not None else None,
            "bucket_id": (fire.get("broken_bucket_id") if name == "buy_no_broken" else fire.get("new_bucket_id")),
            "bucket_lo": fleg.get("bucket_lo"),
            "bucket_hi": fleg.get("bucket_hi"),
            "bucket_label": fleg.get("bucket_label"),
            "settled": False,
            "leg_won": None,
        })
    return {
        "key": key,
        "kind": "reversal",
        "city_id": fire.get("city_id"),
        "icao": fire.get("icao"),
        "market_local_date": fire.get("market_local_date"),
        "local_fire_time": fire.get("local_fire_time"),
        "market_unit": fire.get("market_unit"),
        "direction": fire.get("direction"),
        "fires_at_utc": iso_utc(now),
        "ref_extreme": fire.get("ref_extreme"),
        "ref_source": fire.get("ref_source"),
        "running_extreme": fire.get("running_extreme"),
        "jump": fire.get("jump"),
        "budget_usdc": str(budget_usdc),
        "settled": False,
        "legs": legs,
    }



REFIRE_CFG = dict(PAPER_CFG)
REFIRE_CFG.update({"yes_max_ask": "0.9", "yes_min_ask": "0.48", "no_max_ask": "1.0"})


def make_tall_buckets():
    """Extended bucket ladder h29..h37 for the 追火 scenarios (make_buckets
    only goes to h35 — the third-break obs must still map to a bucket)."""
    out = []
    for t in range(29, 38):
        out.append({"bucket_id": f"h{t}", "lo": float(t), "hi": float(t + 1),
                    "no_token_id": f"NO-{t}", "yes_token_id": f"YES-{t}"})
    return out


def _set_debit(state, amount):
    state["paper_total_debit_usdc"] = float(Decimal(str(amount)).quantize(Decimal("0.00001")))


def _debit(state):
    return Decimal(str(state["paper_total_debit_usdc"] or 0))


def _low_rule_books(asks):
    """books dict (token -> ladder) for consensus sampling / fills."""
    return {tok: make_book(ask, depth=6) for tok, ask in asks.items()}


def _refire_setup():
    """Build a shanghai HIGH session about to fire #1.

    Rank-1 ladder h34 -> h35 -> h36 (each seedable cleanly: h34/h35 are NOT
    among the h30/h32/h33 buckets that ``seed_consensus_rank1`` double-records
    at identical timestamps, which would zero-weight their top samples).
    Fire #1 = obs 35.5 breaks h34 -> buys YES h35 (+ NO h34). Local times are
    Shanghai 13:00-17:00, inside the HIGH window 12-18.
    """
    cfg = REFIRE_CFG
    date = "2026-09-10"
    state = {"paper_initial_capital_usdc": 100.0, "paper_total_debit_usdc": 0.0,
             "entry_count": 0, "positions": {}, "weatherbotyes2re": {}}
    ensure_re_state(state)
    city = make_city()  # shanghai, UTC+8
    buckets = make_tall_buckets()
    key = session_key(city["city_id"], date, "high")
    tracker = ConsensusTracker(min_samples=3)
    t0 = datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)  # 13:00 local, hour 13
    seed_consensus_rank1(tracker, city, date, "high", "h34", t0)
    books34 = _low_rule_books({"YES-34": 0.55})
    a0 = maybe_arm_or_fire(state, city, date, "high", buckets, None, 34.4, t0, t0, books34, cfg, tracker)
    if not any(a.get("action_type") == "re_arm" for a in a0):
        raise AssertionError(f"no arm: {[a.get('reason') for a in a0]}")
    t1 = datetime(2026, 9, 10, 5, 20, tzinfo=timezone.utc)  # 13:20 local
    a1 = maybe_arm_or_fire(state, city, date, "high", buckets, None, 35.5, t1, t1, books34, cfg, tracker)
    fire1 = next((a for a in a1 if a.get("action_type") == "re_fire"), None)
    if fire1 is None:
        raise AssertionError(f"fire #1 missing: {[a.get('reason') for a in a1]}")
    if not (fire1.get("fire_no") == 1 and fire1.get("new_bucket_id") == "h35"
            and fire1.get("broken_bucket_id") == "h34"):
        raise AssertionError(f"bad fire1: no={fire1.get('fire_no')} nb={fire1.get('new_bucket_id')}")
    books1 = _low_rule_books({"NO-34": 0.40, "YES-35": 0.50})
    fills1, _, log1 = run_fire_window(fire1, books1, Decimal("20"), t1, scramble=False)
    if Decimal(str(fills1["buy_yes_new"]["shares"])) <= 0:
        raise AssertionError("fire #1 YES must fill")
    _set_debit(state, fills1["buy_no_broken"]["cost"] + fills1["buy_yes_new"]["cost"])
    state["positions"][key] = make_fire_position(key, fire1, fills1, Decimal("20"), t1)
    tree = ensure_re_state(state)
    if tree["fired"][key].get("fires") != 1:
        raise AssertionError(tree["fired"][key])
    return state, tracker, city, buckets, key, t1


def _refire_obs(tracker, state, city, buckets, key, cfg, date, at, obs, seed_top, seed_ask):
    """Eligible 追火 break: rank-1 ratcheted to ``seed_top``, fresh obs ``obs``
    one bucket beyond it (direction already implied by the market). Returns the
    strategy action list."""
    seed_consensus_rank1(tracker, city, date, "high", seed_top, at)
    return maybe_arm_or_fire(state, city, date, "high", buckets, None, obs, at, at,
                             _low_rule_books({f"YES-{seed_top[1:]}": seed_ask}), cfg, tracker)


def scenario_refire_closes_old_yes():
    """(a) mainline: peak keeps climbing; second break h35 (rank-1 ratcheted
    to h35) -> 追火 buys YES h36 AND the cycle liquidates fire #1's old YES
    h35 leg at best_bid; a third break (fires==2) takes no action."""
    cfg = REFIRE_CFG
    date = "2026-09-10"
    state, tracker, city, buckets, key, t1 = _refire_setup()
    old_yes = next(lg for lg in state["positions"][key]["legs"] if lg["outcome"] == "YES")
    if old_yes["bucket_id"] != "h35":
        return {"name": "refire_closes_old_yes", "ok": False, "error": "old_bucket",
                "old_bucket": old_yes["bucket_id"]}
    old_shares = Decimal(str(old_yes["shares"]))
    # --- fire #2 obs: 36.4 breaks h35 (consensus rank-1 ratcheted to h35) ---
    t2 = datetime(2026, 9, 10, 7, 40, tzinfo=timezone.utc)  # 15:40 local, in window
    a2 = _refire_obs(tracker, state, city, buckets, key, cfg, date, t2, 36.4, "h35", 0.55)
    fire2 = next((a for a in a2 if a.get("action_type") == "re_fire"), None)
    if fire2 is None:
        return {"name": "refire_closes_old_yes", "ok": False,
                "error": "no_fire2", "reasons": [a.get("reason") for a in a2],
                "types": [a.get("action_type") for a in a2]}
    legs2 = fire2.get("legs") or []
    leg_by = {str(l.get("leg")): l for l in legs2}
    if not (fire2.get("fire_no") == 2 and fire2.get("refire") is True
            and sorted(leg_by) == ["buy_no_broken", "buy_yes_new"]
            and leg_by["buy_no_broken"].get("outcome") == "NO"
            and leg_by["buy_no_broken"].get("token_id") == "NO-35"
            and leg_by["buy_yes_new"].get("outcome") == "YES"
            and leg_by["buy_yes_new"].get("token_id") == "YES-36"
            and fire2.get("new_bucket_id") == "h36"
            and fire2.get("broken_bucket_id") == "h35"
            and str(leg_by["buy_no_broken"].get("cap")) == str(cfg["no_max_ask"])
            and str(leg_by["buy_yes_new"].get("cap")) == str(cfg["yes_max_ask"])):
        # caps come from the same cfg keys as fire #1, so the refire NO/YES
        # legs are sized by size_legs exactly like fire #1's legs.
        return {"name": "refire_closes_old_yes", "ok": False,
                "error": "bad_fire2_legs", "legs": legs2, "fire_no": fire2.get("fire_no"),
                "new_bucket_id": fire2.get("new_bucket_id"), "broken": fire2.get("broken_bucket_id"),
                "refire": fire2.get("refire")}
    fills2, _, log2 = run_fire_window(fire2, _low_rule_books({"NO-35": 0.95, "YES-36": 0.52}), Decimal("20"), t2, scramble=False)
    if Decimal(str(fills2["buy_yes_new"]["shares"])) <= 0:
        return {"name": "refire_closes_old_yes", "ok": False, "error": "fire2_no_fill", "fills": fills2}
    if Decimal(str(fills2["buy_no_broken"]["shares"])) <= 0:
        return {"name": "refire_closes_old_yes", "ok": False, "error": "fire2_no_no_fill", "fills": fills2}
    _set_debit(state, _debit(state) + fills2["buy_yes_new"]["cost"] + fills2["buy_no_broken"]["cost"])
    pos2 = make_fire_position(key, fire2, fills2, Decimal("20"), t2)
    # --- cycle-side close: old YES h35 has a live bid of 0.08 ---
    from _r_cycle import record_refire
    import _r_globals
    bid = Decimal("0.08")
    _r_globals._BOOK_CACHE = {"YES-35": {"best_bid": str(bid), "bids": [{"price": str(bid), "size": "99"}]}}
    cfg_cycle = {"log_path": "/tmp/refire_close_old_yes.jsonl"}
    debit_before = _debit(state)
    record_refire(cfg_cycle, state, fire2, pos2, log2, t2)
    position = state["positions"][key]
    old_settled = next(
        lg for lg in position["legs"]
        if lg.get("outcome") == "YES" and lg.get("settled") and lg.get("bucket_id") == "h35"
    )
    refire_yes = next(
        lg for lg in position["legs"]
        if lg.get("outcome") == "YES" and not lg.get("settled") and lg.get("bucket_id") == "h36"
    )
    refire_no = next(
        lg for lg in position["legs"]
        if lg.get("outcome") == "NO" and not lg.get("settled") and lg.get("bucket_id") == "h35"
    )
    old_no_kept = next(
        lg for lg in position["legs"]
        if lg.get("outcome") == "NO" and not lg.get("settled") and lg.get("bucket_id") == "h34"
    )
    exp_proceeds = (bid * old_shares).quantize(Decimal("0.0001"))
    debit_after = _debit(state)
    checks = {
        "old_yes_settled": old_settled.get("settled") is True
        and old_settled.get("closed_by") == "refire_liquidation",
        "proceeds": Decimal(str(old_settled.get("close_proceeds_usdc"))) == exp_proceeds,
        "debit_released": debit_after == (debit_before - exp_proceeds),
        "refire_yes_merged": refire_yes is not None and Decimal(str(refire_yes["shares"])) > 0,
        "refire_no_merged": refire_no is not None and Decimal(str(refire_no["shares"])) > 0,
        "fires_is_two": ensure_re_state(state)["fired"][key].get("fires") == 2,
        "old_no_kept": old_no_kept is not None and not old_no_kept.get("settled"),
    }
    ok = all(checks.values())
    if not ok:
        return {"name": "refire_closes_old_yes", "ok": False, "checks": checks,
                "legs": [(lg.get("leg"), lg.get("outcome"), lg.get("bucket_id"), lg.get("settled"), lg.get("shares")) for lg in position["legs"]],
                "debit_before": str(debit_before),
                "debit_after": str(debit_after), "exp_proceeds": str(exp_proceeds)}
    # --- third break: fires==2 -> already_fired, no action ---
    t3 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    a3 = _refire_obs(tracker, state, city, buckets, key, cfg, date, t3, 37.3, "h36", 0.55)
    third = not any(a.get("action_type") == "re_fire" for a in a3) and any(a.get("reason") == "already_fired" for a in a3)
    return {"name": "refire_closes_old_yes", "ok": ok and third,
            "checks": checks, "third_break_blocked": third,
            "fires": ensure_re_state(state)["fired"][key].get("fires")}


def scenario_refire_close_no_bid():
    """(a) no-bid branch: fire #1's old YES h35 has NO book/bid when the 追火
    fills -> old leg written off at 0 (still marked settled), no cash release."""
    cfg = REFIRE_CFG
    date = "2026-09-10"
    state, tracker, city, buckets, key, t1 = _refire_setup()
    t2 = datetime(2026, 9, 10, 7, 40, tzinfo=timezone.utc)
    a2 = _refire_obs(tracker, state, city, buckets, key, cfg, date, t2, 36.4, "h35", 0.55)
    fire2 = next((a for a in a2 if a.get("action_type") == "re_fire"), None)
    if fire2 is None:
        return {"name": "refire_close_no_bid", "ok": False, "reasons": [a.get("reason") for a in a2]}
    # NO-35 (newly-broken bucket NO) has NO book here -> no_book branch; the
    # YES-36 leg fills. Old YES h35 has no bid -> written off at 0.
    fills2, _, log2 = run_fire_window(fire2, _low_rule_books({"YES-36": 0.52}), Decimal("20"), t2, scramble=False)
    no_no_fill = Decimal(str(fills2["buy_no_broken"]["shares"])) == 0
    yes_filled = Decimal(str(fills2["buy_yes_new"]["shares"])) > 0
    _set_debit(state, _debit(state) + fills2["buy_yes_new"]["cost"])
    pos2 = make_fire_position(key, fire2, fills2, Decimal("20"), t2)
    from _r_cycle import record_refire
    import _r_cycle as _cycle_mod
    import _r_globals
    _r_globals._BOOK_CACHE = {}  # old YES token: no book -> no bid -> write off 0
    # record_refire refreshes a missing old-token book once before closing —
    # stub the refresh (no network in the scenario) so it stays unavailable.
    _orig_refresh = _cycle_mod.refresh_books
    _cycle_mod.refresh_books = lambda cfg, toks, now: {}
    cfg_cycle = {"log_path": "/tmp/refire_close_no_bid.jsonl"}
    debit_before = _debit(state)
    try:
        record_refire(cfg_cycle, state, fire2, pos2, log2, t2)
    finally:
        _cycle_mod.refresh_books = _orig_refresh
    old_settled = next(
        lg for lg in state["positions"][key]["legs"]
        if lg.get("outcome") == "YES" and lg.get("settled") and lg.get("bucket_id") == "h35"
    )
    no_leg_no_book = any(
        i.get("leg") == "buy_no_broken" and i.get("status") == "no_book" for i in log2
    )
    checks = {
        "no_leg_tried_no_book": no_no_fill and no_leg_no_book,
        "yes_leg_filled": yes_filled,
        "old_yes_settled_at_zero": old_settled.get("settled") is True
        and Decimal(str(old_settled.get("close_proceeds_usdc") or 0)) == 0,
        "no_release": _debit(state) == debit_before,
        "bid_none": old_settled.get("bid_at_close") is None,
    }
    return {"name": "refire_close_no_bid", "ok": all(checks.values()), "checks": checks,
            "old_settled": old_settled}


def _refire_price_case(ask, label):
    cfg = REFIRE_CFG
    date = "2026-09-10"
    state, tracker, city, buckets, key, t1 = _refire_setup()
    t2 = datetime(2026, 9, 10, 7, 40, tzinfo=timezone.utc)
    a2 = _refire_obs(tracker, state, city, buckets, key, cfg, date, t2, 36.4, "h35", 0.55)
    fire2 = next((a for a in a2 if a.get("action_type") == "re_fire"), None)
    if fire2 is None:
        return {"name": label, "ok": False, "reasons": [a.get("reason") for a in a2]}
    # Only the YES-36 book is provided at the gate price; the NO-35 leg has no
    # book and the YES leg is gated (below floor / above cap) -> no fill.
    fills2, _, log2 = run_fire_window(fire2, _low_rule_books({"YES-36": ask}), Decimal("20"), t2, scramble=False)
    # no fill -> record_refire must NOT liquidate the old YES leg
    from _r_cycle import record_refire
    import _r_globals
    _r_globals._BOOK_CACHE = {"YES-35": {"best_bid": "0.08", "bids": [{"price": "0.08", "size": "99"}]}}
    pos2 = make_fire_position(key, fire2, fills2, Decimal("20"), t2)
    record_refire({"log_path": f"/tmp/refire_{label}.jsonl"}, state, fire2, pos2, log2, t2)
    refilled = Decimal(str(fills2.get("buy_yes_new", {}).get("shares") or 0))
    old_unsettled = next(
        lg for lg in state["positions"][key]["legs"]
        if lg.get("outcome") == "YES" and lg.get("bucket_id") == "h35"
    )["settled"] is not True
    return {"name": label, "ok": refilled == 0 and old_unsettled
            and ensure_re_state(state)["fired"][key].get("fires") == 2,
            "filled_shares": str(refilled),
            "ask": ask,
            "ladder_statuses": sorted({i.get("status") for i in log2}),
            "old_yes_kept_open": old_unsettled}


def scenario_refire_below_floor():
    """(b) price gate: 追火 YES ask below yes_min_ask (0.48) = break NOT
    confirmed -> refire leg never fills, old YES leg kept."""
    return _refire_price_case(0.30, "refire_below_floor")


def scenario_refire_above_cap():
    """(b) price gate: 追火 YES ask above yes_max_ask (0.9) -> FAK aborts,
    no chase, old YES leg kept."""
    return _refire_price_case(0.95, "refire_above_cap")


def scenario_refire_out_of_window():
    """(c) window gate: the second break arrives at local hour 19 (HIGH window
    ends 18) -> hour_not_in_window, no re_fire, fires stays 1 (slot kept)."""
    cfg = REFIRE_CFG
    date = "2026-09-10"
    state, tracker, city, buckets, key, t1 = _refire_setup()
    t2 = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)  # 19:00 local -> out of window
    a2 = _refire_obs(tracker, state, city, buckets, key, cfg, date, t2, 36.4, "h35", 0.55)
    fires2 = [a for a in a2 if a.get("action_type") == "re_fire"]
    reasons = [a.get("reason") for a in a2]
    ok = (not fires2 and "hour_not_in_window" in reasons
          and ensure_re_state(state)["fired"][key].get("fires") == 1)
    return {"name": "refire_out_of_window", "ok": ok,
            "types": [a.get("action_type") for a in a2], "reasons": reasons,
            "fires": ensure_re_state(state)["fired"][key].get("fires")}


def scenario_refire_persists_across_restart():
    """(d) fire_count persistence: a fires==1 key may 追火 once after a
    restart (state round-trip via deepcopy); fires==2 blocks everything. A
    LEGACY fired record (no fires field) is treated as 1 fire used."""
    cfg = REFIRE_CFG
    date = "2026-09-10"
    city = make_city()
    buckets = make_tall_buckets()
    key = session_key(city["city_id"], date, "high")

    def leg(leg_name, bucket, shares):
        return {"leg": leg_name, "token_id": f"YES-{bucket[1:]}", "side": "BUY",
                "outcome": "YES", "cap": "0.9", "notional_pct": "0.75",
                "cost_usdc": "5", "shares": str(shares), "avg_price": "0.5",
                "bucket_id": bucket, "settled": False, "leg_won": None}

    def run_stage(state, tracker, obs, at, seed_top, seed_ask):
        return _refire_obs(tracker, state, city, buckets, key, cfg, date, at, obs, seed_top, seed_ask)

    # --- modern: fire #1 -> fires==1; restart; eligible break -> fires==2 ---
    state = {"paper_initial_capital_usdc": 100.0, "paper_total_debit_usdc": 0.0,
             "entry_count": 0, "positions": {}, "weatherbotyes2re": {}}
    ensure_re_state(state)
    tracker = ConsensusTracker(min_samples=3)
    t0 = datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)
    seed_consensus_rank1(tracker, city, date, "high", "h34", t0)
    maybe_arm_or_fire(state, city, date, "high", buckets, None, 34.4, t0, t0,
                      _low_rule_books({"YES-34": 0.55}), cfg, tracker)
    t1 = datetime(2026, 9, 10, 5, 20, tzinfo=timezone.utc)
    a1 = run_stage(state, tracker, 35.5, t1, "h34", 0.55)
    fire1 = next(a for a in a1 if a.get("action_type") == "re_fire")
    c1 = fire1.get("fire_no") == 1 and ensure_re_state(state)["fired"][key].get("fires") == 1
    # restart (persisted blob round-trip)
    state2 = deepcopy(state)
    # a real fire #1 created an open position holding YES h35 — seed it (as
    # the cycle would have) so the 追火 eligibility can read fire #1's bucket.
    state2["positions"][key] = {
        "key": key, "settled": False, "direction": "high",
        "legs": [leg("buy_yes_new", "h35", 10), leg("buy_no_broken", "h34", 5)],
    }
    t2 = datetime(2026, 9, 10, 7, 40, tzinfo=timezone.utc)
    a2 = run_stage(state2, tracker, 36.4, t2, "h35", 0.55)
    fire2 = next((a for a in a2 if a.get("action_type") == "re_fire"), None)
    f2legs = sorted({str(l.get("leg")) for l in (fire2.get("legs") or [])}) if fire2 else []
    c2 = (fire2 is not None and fire2.get("fire_no") == 2 and fire2.get("refire") is True
          and f2legs == ["buy_no_broken", "buy_yes_new"]
          and ensure_re_state(state2)["fired"][key].get("fires") == 2)
    # third break after another restart: fires==2 -> already_fired
    state3 = deepcopy(state2)
    t3 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    a3 = run_stage(state3, tracker, 37.3, t3, "h36", 0.55)
    c3 = not any(a.get("action_type") == "re_fire" for a in a3) and any(a.get("reason") == "already_fired" for a in a3)
    # --- legacy record (no fires field) treated as 1 fire used: one 追火 ---
    stateL = {"paper_initial_capital_usdc": 100.0, "paper_total_debit_usdc": 0.0,
              "entry_count": 0, "positions": {}, "weatherbotyes2re": {}}
    ensure_re_state(stateL)
    ensure_re_state(stateL)["ever_armed"][key] = iso_utc(t1)
    ensure_re_state(stateL)["fired"][key] = {"status": "fired", "at_utc": iso_utc(t1),
                                              "jump": 1, "ref_source": "market_rank1"}
    stateL["positions"][key] = {"key": key, "settled": False, "direction": "high",
                                "legs": [leg("buy_yes_new", "h35", 10)]}
    trackerL = ConsensusTracker(min_samples=3)
    aL = run_stage(stateL, trackerL, 36.4, t2, "h35", 0.55)
    fireL = next((a for a in aL if a.get("action_type") == "re_fire"), None)
    fLlegs = sorted({str(l.get("leg")) for l in (fireL.get("legs") or [])}) if fireL else []
    c4 = (fireL is not None and fireL.get("fire_no") == 2
          and fLlegs == ["buy_no_broken", "buy_yes_new"]
          and ensure_re_state(stateL)["fired"][key].get("fires") == 2)
    return {"name": "refire_persists_across_restart",
            "ok": all([c1, c2, c3, c4]), "checks": {"c1": c1, "c2": c2, "c3": c3, "c4": c4},
            "types2": [a.get("action_type") for a in a2],
            "types3": [a.get("action_type") for a in a3],
            "reasons3": [a.get("reason") for a in a3],
            "typesL": [a.get("action_type") for a in aL]}


if __name__ == "__main__":
    main()
