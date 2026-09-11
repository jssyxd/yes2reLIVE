"""METAR-vs-TAF one-bucket reversal with long-horizon consensus filter.

IDLE -> ARMED -> FIRED -> COOLDOWN

Fire only when:
  1) running extreme breaks the reference extreme by exactly one bucket
     (reference = TAF TX/TN when present, else the stable market rank-1
     consensus bucket — both may fire; allow_market_ref_fire=false closes
     the market-ref path if London-EGLC-class losses ever recur)
  2) obs is fresh (new obs_time, age <= require_fresh_obs_seconds)
  3) local hour in fire window (HIGH 12..18 local, LOW 0..9)
  4) broken bucket was long-horizon market consensus (1-2h TWAP rank-1)

A session may fire AT MOST TWICE (2026-09-09 operator decision, warsaw
2026-09-09 low 17->16->15 double break): the first breach, then ONE "追火"
when a fresh obs breaks ONE bucket further AFTER the reference (TAF AMD or
consensus rank-1) has ratcheted onto the bucket fire #1 bought YES in. The
追火腿 is STRUCTURALLY SYMMETRIC with fire #1 (2026-09-09 operator reversal):
buy_no_broken on the newly-broken bucket (the bucket fire #1 holds YES in —
its NO is ~1 but still tried, capped/sized exactly like fire #1's NO leg)
PLUS buy_yes_new on the new bucket. On the second fire the cycle additionally
sells fire #1's old-bucket YES leg at best_bid (``_r_cycle.record_refire``);
breaks past fire #2 take no further action.

NO leg on broken bucket is the main trade; YES on new bucket is optional and smaller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any
from zoneinfo import ZoneInfo

from consensus_tracker import ConsensusTracker, DEFAULT_TRACKER
from _r_state import _SECTIONS as STATE_SECTIONS

ARM_C = 1.0
MAX_BUCKET_JUMP = 1
MAX_FIRES_PER_SESSION = 2  # one fire + one 追火 (2026-09-09 double-break rule)
NO_MAX_ASK = Decimal("0.85")
YES_MAX_ASK = Decimal("0.48")
NO_NOTIONAL_PCT = Decimal("0.75")
YES_NOTIONAL_PCT = Decimal("0.25")
# Fire-window closing 2026-09-08: interval windows, not single edges.
# HIGH fires only in local 13:00-<end> (peak afternoons); LOW only in local
# 01:00-09:00 (predawn/dawn). Rationale + incidents in CHANGELOG 2026-09-08.
# IANA-local fire windows (2026-09-08 operator decision): HIGH peaks form
# in the afternoon 12:00-18:00 local; LOW forms 00:00-09:00 local. Anything
# outside is off-window drift, never the peak-tick reversal we sell. The
# window is deliberately close to when the daily extreme settles.
HIGH_FIRE_LOCAL_START = 12
HIGH_FIRE_LOCAL_HOUR_END = 18
LOW_FIRE_LOCAL_START = 0
LOW_FIRE_LOCAL_END = 9
REQUIRE_FRESH_OBS_SECONDS = 180  # legacy absolute-age gate — deprecated 2026-09-03 (see OBS_* window below)
OBS_MAX_LOOKBACK_SECONDS = 5400  # 90 min sanity: obs older than this = stale feed, do not fire
OBS_MAX_FUTURE_SECONDS = 900     # 15 min sanity: US AWS stations publish ~7 min EARLY; >15 min ahead = bad stamp
CONSENSUS_WINDOW_SECONDS = 7200  # 2h default; config can set 3600
CONSENSUS_MIN_SAMPLES = 20
CONSENSUS_MIN_LEAD = Decimal("0.03")
BREAK_CONFIRM_MARGIN_F = 1.0  # F-market boundary-confirmation margin (whole °F), 2026-09-07
ZERO = Decimal("0")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bucket_contains(bucket: dict[str, Any], value: float) -> bool:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    return (lo is None or value >= float(lo)) and (hi is None or value < float(hi))


def bucket_label(bucket: dict[str, Any] | None, unit: str = "C") -> str | None:
    """Human-readable temperature range of a bucket, e.g. 'between 30-31°C'.

    Prefers the bucket's own ``label`` (the original Gamma question wording);
    falls back to a lo/hi rendering for synthetic buckets that only carry
    numeric bounds. Returns None for a missing/empty bucket."""
    if not bucket:
        return None
    lbl = bucket.get("label")
    if lbl:
        return str(lbl)
    lo = bucket.get("lo")
    hi = bucket.get("hi")
    u = ("°" + str(unit or "C").upper()) if unit else ""
    if lo is not None and hi is not None:
        return f"{lo}-{hi}{u}"
    if lo is not None:
        return f">={lo}{u}"
    if hi is not None:
        return f"<{hi}{u}"
    return f"?{u}"


def _local_fire_time(city: dict[str, Any], now_utc: datetime) -> str | None:
    """Fire timestamp in the city's own timezone (for readable position rows)."""
    try:
        tz = city.get("timezone")
        if not tz:
            return None
        return now_utc.astimezone(ZoneInfo(tz)).isoformat()
    except Exception:
        return None


def ensure_re_state(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.setdefault("weatherbotyes2re", {})
    for name in STATE_SECTIONS:
        tree.setdefault(name, {})
    return tree


def mid_value(bucket: dict[str, Any]) -> float:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2.0
    if lo is not None:
        return float(lo)
    if hi is not None:
        return float(hi)
    return 0.0


def ordered_buckets(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(buckets, key=mid_value)


def find_bucket(buckets: list[dict[str, Any]], value: float) -> dict[str, Any] | None:
    for b in buckets:
        if bucket_contains(b, value):
            return b
    return None


def bucket_index(ordered: list[dict[str, Any]], bucket: dict[str, Any] | None) -> int | None:
    if bucket is None:
        return None
    bid = str(bucket.get("bucket_id") or bucket.get("id") or "")
    for i, b in enumerate(ordered):
        if str(b.get("bucket_id") or b.get("id") or "") == bid:
            return i
        if b is bucket:
            return i
    return None


def session_key(city_id: str, market_local_date: str, direction: str) -> str:
    return f"{city_id}|{market_local_date}|{direction}"


def fires_used(rec: dict[str, Any] | None) -> int:
    """Real fires a session has already used, read from its ``fired`` record.

    A record without the ``fires`` counter is either pre-2026-09-09 state or a
    one-shot skip lock. Migration decision: treat it as ONE fire used so an
    old fired key may still 追火 once (a lock-only key has no open YES leg, so
    the re-fire eligibility below keeps it inert in practice).
    """
    if not rec:
        return 0
    if "fires" in rec:
        try:
            return max(0, int(rec["fires"] or 0))
        except (TypeError, ValueError):
            return 1
    return 1


def refire_target_index(
    state: dict[str, Any],
    key: str,
    direction: str,
    ordered: list[dict[str, Any]],
    run_b: dict[str, Any] | None,
) -> int | None:
    """Index (in ``ordered``) of fire #1's YES bucket when the current running
    extreme sits strictly beyond it in the direction of travel — else None.

    The session's open paper position (the ledger is the source of truth for
    what a fire actually bought) is where fire #1's YES leg is read. None
    means: no open position, no un-settled YES leg with shares, an unknown
    bucket, or an obs that did not LEAVE that bucket — a same-bucket drift
    must stay ``already_fired`` (see no_double_fire), never re-fire.
    """
    i_run = bucket_index(ordered, run_b)
    if i_run is None:
        return None
    pos = (state.get("positions") or {}).get(key)
    if pos is None or pos.get("settled"):
        return None
    for lg in pos.get("legs") or []:
        if lg.get("settled"):
            continue
        if str(lg.get("outcome") or "").upper() != "YES":
            continue
        try:
            if Decimal(str(lg.get("shares") or 0)) <= ZERO:
                continue
        except Exception:  # noqa: BLE001
            continue
        b1 = lg.get("bucket_id")
        if not b1:
            return None
        i1 = None
        for i, b in enumerate(ordered):
            if str(b.get("bucket_id") or b.get("id") or "") == str(b1):
                i1 = i
                break
        if i1 is None:
            return None
        if (direction == "high" and i_run > i1) or (direction == "low" and i_run < i1):
            return i1
        return None
    return None


def prune_stale_sessions(state: dict[str, Any], cities: list[dict[str, Any]], now_utc: datetime | None = None) -> int:
    """Drop expired armed/fired/running_extremes/last_obs_time session entries.

    Pure, deterministic, never raises; only the six date-scoped sections
    above are mutated (taf_forecasts / last_obs / other state content are
    untouched).
    A session key has the shape ``city_id|market_local_date|direction`` (see
    session_key). Removal rules:

      1. Non-today date: market-local date != the city's local today
         (cross-day carryover from a previous market day) -> delete — EXCEPT a
         ``fired`` marker whose session still has an open paper position. That
         marker is the session's one-fire dedupe credential: between a city's
         local-midnight rollover and the next rules-TTL refresh the cache can
         still feed the old-date rule, and dropping the marker while its
         position is open re-opened the 2026-09-06 re-fire loop (same breach
         obs -> already_fired miss -> re-fire -> cash reserved with no leg to
         settle). The marker is kept until the position settles.

      2. Unknown city: city_id no longer present in the registry -> delete
         (defensive: registration table shrank).
      3. low zombie (armed only): a ``low`` session whose city local hour is
         already past LOW_FIRE_LOCAL_END — the strategy low window can never
         fire it, so the armed entry would pin the run loop to fast-poll
         forever -> delete.

    Malformed keys (not exactly 3 ``|``-separated parts) are kept as-is, and
    an unparseable timezone for a known city makes that city's keys skipped,
    both without raising. Returns the total number of deleted entries.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    tree = ensure_re_state(state)
    positions = state.get("positions") or {}
    by_id = {c.get("city_id"): c for c in cities if c.get("city_id") is not None}
    removed = 0
    for section in ("armed", "fired", "running_extremes", "last_obs_time", "ever_armed", "last_fire_obs"):
        section_state = tree.get(section)
        if not isinstance(section_state, dict):
            continue
        for key in list(section_state):
            parts = key.split("|")
            if len(parts) != 3:
                continue  # malformed key — defensive: never delete
            city_id, market_local_date, direction = parts
            city = by_id.get(city_id)
            if city is None:
                del section_state[key]
                removed += 1
                continue
            tz_name = city.get("timezone")
            if not isinstance(tz_name, str) or not tz_name:
                continue  # cannot localize — skip this city, never raise
            try:
                local_dt = now_utc.astimezone(ZoneInfo(tz_name))
            except Exception:
                continue  # bad tz entry — skip this city, never raise
            if market_local_date != local_dt.date().isoformat():
                if section == "fired":
                    pos = positions.get(key)
                    if pos is not None and not pos.get("settled"):
                        continue  # keep the one-fire dedupe credential until the position settles
                del section_state[key]
                removed += 1
                continue
            if section == "armed" and direction == "low" and local_dt.hour > LOW_FIRE_LOCAL_END:
                del section_state[key]
                removed += 1
    return removed


def update_running_extreme(state, city_id, market_local_date, direction, temp: float, now_utc: datetime):
    tree = ensure_re_state(state)
    key = session_key(city_id, market_local_date, direction)
    rec = tree["running_extremes"].get(key) or {"value": None, "obs_count": 0}
    prev = rec.get("value")
    if direction == "high":
        new_val = temp if prev is None else max(float(prev), temp)
    else:
        new_val = temp if prev is None else min(float(prev), temp)
    rec["value"] = new_val
    rec["obs_count"] = int(rec.get("obs_count") or 0) + 1
    rec["updated_at_utc"] = iso_utc(now_utc)
    tree["running_extremes"][key] = rec
    return rec


def hour_ok(direction, local_hour, high_start, high_hour_end, low_start, low_end) -> bool:
    """Inclusive local-hour window gate (2026-09-08 interval semantics).

    HIGH: high_start <= local_hour <= high_hour_end (13..cfg ceiling — e.g.
    17; a 18:25-local London-class break is hour 18 and is rejected).
    LOW:  low_start  <= local_hour <= low_end  (default 1..9).
    Fire away from the window in which the daily extreme actually forms —
    a break observed at 02:00 (mexico-city low) or 03:00 (SF high, off-window)
    is off-peak drift, not the capped peak-tick reversal this strategy sells.
    """
    if direction == "high":
        return local_hour >= high_start and local_hour <= high_hour_end
    return local_hour >= low_start and local_hour <= low_end


def obs_is_fresh(obs_time_utc: datetime | None, now_utc: datetime, max_age: int) -> bool:
    if obs_time_utc is None:
        return False
    return (now_utc.astimezone(timezone.utc) - obs_time_utc.astimezone(timezone.utc)).total_seconds() <= max_age


def is_new_obs_time(state: dict[str, Any], key: str, obs_time_utc: datetime | None) -> bool:
    """Reject duplicate pushes of the same observation timestamp."""
    if obs_time_utc is None:
        return False
    tree = ensure_re_state(state)
    prev = tree["last_obs_time"].get(key)
    stamp = iso_utc(obs_time_utc)
    if prev == stamp:
        return False
    tree["last_obs_time"][key] = stamp
    return True


def reference_extreme_from_consensus(
    tracker: ConsensusTracker,
    city_id: str,
    market_local_date: str,
    direction: str,
    ordered: list[dict[str, Any]],
    now_utc: datetime,
    window_seconds: int,
) -> tuple[float | None, dict[str, Any] | None, str]:
    """When TAF missing: use long-horizon rank-1 bucket mid as reference extreme."""
    ranks = tracker.rank_buckets(city_id, market_local_date, direction, now_utc, window_seconds)
    if not ranks:
        return None, None, "no_consensus"
    top_id, twap, _ = ranks[0]
    for b in ordered:
        if str(b.get("bucket_id") or b.get("id") or "") == top_id:
            return mid_value(b), b, "market_rank1"
    return None, None, "rank1_unmapped"


def maybe_arm_or_fire(
    state: dict[str, Any],
    city: dict[str, Any],
    market_local_date: str,
    direction: str,
    buckets: list[dict[str, Any]],
    taf_extreme: float | None,
    observed_temp: float | None,
    obs_time_utc: datetime | None,
    now_utc: datetime,
    books_by_token: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    consensus_tracker: ConsensusTracker | None = None,
) -> list[dict[str, Any]]:
    """Main hook: call on every new METAR observation timestamp.

    Always samples books into consensus_tracker when provided so the
    long-horizon rank filter has data before a break.
    """
    actions: list[dict[str, Any]] = []
    if observed_temp is None:
        return actions
    cfg = config or {}
    arm_c = float(cfg.get("arm_c", ARM_C))
    max_jump = int(cfg.get("max_bucket_jump", MAX_BUCKET_JUMP))
    fresh_s = int(cfg.get("require_fresh_obs_seconds", REQUIRE_FRESH_OBS_SECONDS))  # legacy, unused by fire window
    obs_lookback_s = int(cfg.get("max_obs_lookback_seconds", OBS_MAX_LOOKBACK_SECONDS))
    obs_future_s = int(cfg.get("max_obs_future_seconds", OBS_MAX_FUTURE_SECONDS))
    high_start = int(cfg.get("high_fire_local_start", HIGH_FIRE_LOCAL_START))
    # HIGH upper bound: new key high_fire_local_hour_end first, legacy
    # high_fire_local_end for configs not yet migrated, module ceiling last.
    _high_hour_end = cfg.get("high_fire_local_hour_end")
    if _high_hour_end is None:
        _high_hour_end = cfg.get("high_fire_local_end", HIGH_FIRE_LOCAL_HOUR_END)
    high_hour_end = int(_high_hour_end)
    low_start = int(cfg.get("low_fire_local_start", LOW_FIRE_LOCAL_START))
    low_end = int(cfg.get("low_fire_local_end", LOW_FIRE_LOCAL_END))
    cons_win = int(cfg.get("consensus_window_seconds", CONSENSUS_WINDOW_SECONDS))
    cons_min_samples = int(cfg.get("consensus_min_samples", CONSENSUS_MIN_SAMPLES))
    cons_min_lead = Decimal(str(cfg.get("consensus_min_lead", CONSENSUS_MIN_LEAD)))
    require_consensus = bool(cfg.get("require_consensus_filter", True))
    allow_market_ref = bool(cfg.get("allow_market_consensus_reference", True))
    # market-rank-1 fires are allowed (stable-consensus break is the edge);
    # config allow_market_ref_fire=false is the kill-switch if it regresses.
    allow_market_ref_fire = bool(cfg.get("allow_market_ref_fire", True))

    tracker = consensus_tracker or DEFAULT_TRACKER
    tree = ensure_re_state(state)
    key = session_key(city["city_id"], market_local_date, direction)

    # Cross-midnight date guard (2026-09-06 incident): a session whose market
    # local date is no longer the city's local TODAY must never arm/fire. When
    # a market day rolls over (e.g. chicago 09-05 -> 09-06 at 05:00Z), prune
    # deletes yesterday's fired marker every cycle but the rules cache (TTL'd)
    # still lists the old-date rule — so the same breach observation re-fires
    # on every cycle (~$230/min paper burn on both A/B arms). Skipping stale
    # dates here closes the loop: prune keeps cleaning, nothing re-fires.
    try:
        city_tz = city.get("timezone")
        if not city_tz:
            raise ValueError("city missing timezone")
        local_today = now_utc.astimezone(ZoneInfo(city_tz)).date().isoformat()
    except Exception:  # noqa: BLE001 — bad/missing tz: FAIL CLOSED. "Today" in
        # the city's tz cannot be verified, so never arm/fire (the previous
        # fallback set local_today = market_local_date, which self-disabled the
        # guard and re-opened the 2026-09-06 stale-date re-fire path for any
        # city whose tz entry ever broke or went missing).
        return [{"action_type": "re_skip", "reason": "stale_market_date",
                 "key": key, "guard": "tz_unresolvable"}]
    if market_local_date != local_today:
        return [{"action_type": "re_skip", "reason": "stale_market_date", "key": key}]

    # Continuous consensus sampling (even before break)
    tracker.record_books(
        city["city_id"],
        market_local_date,
        direction,
        buckets,
        books_by_token,
        now_utc,
    )

    fired_rec = tree["fired"].get(key)
    fires_before = fires_used(fired_rec)
    # MAX_FIRES_PER_SESSION (2): one fire + one 追火. A session that already
    # used both is done for the day — any further break settles in the held
    # legs, no more action.
    if fires_before >= MAX_FIRES_PER_SESSION:
        return [{"action_type": "re_skip", "reason": "already_fired", "key": key,
                 "fires": fires_before, "max": MAX_FIRES_PER_SESSION}]

    # Bug 1 + Bug 3 — TRIPLE LOCK: in addition to the fired marker, refrain
    # from re-firing when an open (unsettled) paper position already exists
    # for this key. The state["positions"] ledger is the source of truth for
    # "a fire actually produced work" — without this guard a prune-then-fire
    # pattern (or a crash-mid-cycle on the live deploy) re-opens the same
    # 22-fire-on-Seoul gate seen 2026-09-05 even though tree["fired"][key]
    # remained set in memory. 2026-09-09: this lock applies ONLY when the
    # fired marker is absent — with a fires==1 marker an open position is the
    # NORMAL pre-追火 state (fire #1's position stays open until settlement)
    # and the eligibility gate below decides whether a second fire may run.
    _positions = state.get("positions") or {}
    _existing = _positions.get(key)
    if fired_rec is None and _existing is not None and not _existing.get("settled"):
        tree["fired"][key] = {
            "status": "fired_no_fill", "at_utc": iso_utc(now_utc),
            "jump": None, "ref_source": "lock_open_position",
            "reason": "open_position_already_exists",
        }
        return [{"action_type": "re_skip", "reason": "open_position_already_exists",
                 "key": key, "fires_at_utc": _existing.get("fires_at_utc")}]

    # Bug 3 — third lock: the exact same observation timestamp already
    # produced a fire this session. is_new_obs_time below updates
    # last_obs_time on every pass; this section reads last_fire_obs so a
    # repeat push of the same obs (e.g. WS early-pull re-trigger on the
    # same METAR stamp) cannot fire twice. Independent of last_obs_time.
    _obs_iso = iso_utc(obs_time_utc) if obs_time_utc is not None else None
    if _obs_iso is not None and tree.get("last_fire_obs", {}).get(key) == _obs_iso:
        return [{"action_type": "re_skip", "reason": "duplicate_obs_fired",
                 "key": key, "obs_time_utc": _obs_iso}]

    # Open-position cap: never open a new position while the number of
    # unsettled paper positions is at/over max_open_positions. Prevents
    # unbounded concurrent exposure when fires fill but settlements lag
    # (max_open_positions previously existed only as a DEFAULTS entry with no
    # enforcement — positions could stack past the cap).
    max_open = int(cfg.get("max_open_positions") or 0)
    if max_open > 0:
        open_count = sum(
            1 for p in (state.get("positions") or {}).values()
            if not p.get("settled")
        )
        if open_count >= max_open:
            return [{"action_type": "re_skip", "reason": "max_open_positions",
                     "key": key, "open": open_count, "cap": max_open}]

    # Duplicate obs_time guard (must run after fired check so we still record books)
    if not is_new_obs_time(state, key, obs_time_utc):
        return [{"action_type": "re_skip", "reason": "duplicate_obs_time", "key": key}]

    local_hour = now_utc.astimezone(ZoneInfo(city["timezone"])).hour
    rec = update_running_extreme(
        state, city["city_id"], market_local_date, direction, float(observed_temp), now_utc
    )
    running = float(rec["value"])
    ordered = ordered_buckets(buckets)

    # Reference extreme: prefer TAF; fallback to market rank-1 mid
    ref_source = "taf"
    ref_extreme = float(taf_extreme) if taf_extreme is not None else None
    taf_b = find_bucket(ordered, float(taf_extreme)) if taf_extreme is not None else None
    if taf_b is None and allow_market_ref:
        ref_extreme, taf_b, ref_source = reference_extreme_from_consensus(
            tracker,
            city["city_id"],
            market_local_date,
            direction,
            ordered,
            now_utc,
            cons_win,
        )
    if ref_extreme is None or taf_b is None:
        return [{"action_type": "re_skip", "reason": "no_reference_extreme", "key": key, "ref_source": ref_source}]

    run_b = find_bucket(ordered, running)
    taf_i = bucket_index(ordered, taf_b)
    run_i = bucket_index(ordered, run_b)
    if taf_i is None or run_i is None:
        return [{"action_type": "re_skip", "reason": "bucket_unmapped", "key": key}]

    distance_c = abs(running - float(ref_extreme))
    jump = run_i - taf_i if direction == "high" else taf_i - run_i

    # Re-fire lane (one fire already used): the only event that may consume
    # fire #2 is a NEW fresh obs whose running extreme broke ONE bucket past
    # the YES bucket fire #1 actually bought (the reference having ratcheted
    # there — warsaw 16 after the 17->16 break). Same-bucket drift and any
    # non-breaking obs stay already_fired and never re-arm the session.
    refiring = False
    if fires_before == 1:
        if jump <= 0 or refire_target_index(state, key, direction, ordered, run_b) is None:
            return [{"action_type": "re_skip", "reason": "already_fired", "key": key,
                     "fires": 1, "max": MAX_FIRES_PER_SESSION,
                     "detail": "no_further_break"}]
        refiring = True

    armed = tree["armed"].get(key)
    if jump <= 0 and distance_c <= arm_c and hour_ok(direction, local_hour, high_start, high_hour_end, low_start, low_end):
        tree["armed"][key] = {
            "status": "armed",
            "taf_bucket_id": str(taf_b.get("bucket_id") or taf_b.get("id") or ""),
            "ref_extreme": float(ref_extreme),
            "ref_source": ref_source,
            "running": running,
            "armed_at_utc": iso_utc(now_utc),
            "fast_poll": True,
        }
        # Bug 2 — armed-ever: mark this session as having been armed at
        # least once. persisted to disk so the Bug 2 gate below can accept
        # a fire from armed-history even when the in-memory armed marker
        # was popped at the end of a prior arm/fire cycle.
        tree.setdefault("ever_armed", {})[key] = iso_utc(now_utc)
        actions.append({
            "action_type": "re_arm",
            "key": key,
            "distance_c": distance_c,
            "ref_source": ref_source,
            "taf_bucket_id": tree["armed"][key]["taf_bucket_id"],
            "prefetch_tokens": True,
            "fast_poll": True,
            "fast_poll_seconds": int(cfg.get("fast_poll_seconds", 8)),
        })
        return actions

    if jump <= 0:
        if armed and distance_c > arm_c + 0.7:
            tree["armed"].pop(key, None)
            actions.append({"action_type": "re_disarm", "key": key, "reason": "moved_away"})
        return actions

    # F-market boundary confirmation (2026-09-07, SF 9/4 low misfire).
    # METAR temperatures are whole-degree Celsius; after °C→°F the converted
    # extreme can sit anywhere in a ±0.9°F band (14°C = 57.2-58.8°F), while
    # Wunderground finalizes the daily extreme at whole °F post-QC. A break
    # computed on the raw converted float (57.92 < 58) can therefore be FALSE
    # when the finalized extreme stays inside the broken bucket (58.x°F).
    # Require the whole-degree °F extreme to clear the broken-bucket boundary
    # by break_confirm_margin_f (default 1°F): a low break of the 58-59°F
    # bucket needs round(running) <= 57. C markets are exempt: METAR whole °C
    # truncation aligns exactly with Polymarket's whole-degree truncation rule.
    unit = str(city.get("market_unit") or "C").upper()
    if unit == "F":
        margin_f = float(cfg.get("break_confirm_margin_f", BREAK_CONFIRM_MARGIN_F))
        if margin_f > 0:
            run_w = float(math.floor(running + 0.5))  # whole °F (ASOS display convention)
            b_lo = float(taf_b.get("lo") if taf_b.get("lo") is not None else taf_b.get("hi", 0))
            b_hi = float(taf_b.get("hi") if taf_b.get("hi") is not None else b_lo)
            if direction == "high" and run_w < b_hi + margin_f:
                return [{"action_type": "re_skip", "reason": "break_not_confirmed", "key": key,
                         "jump": jump, "run_whole_f": run_w, "bucket_hi": b_hi, "margin_f": margin_f}]
            if direction == "low" and run_w > b_lo - margin_f:
                return [{"action_type": "re_skip", "reason": "break_not_confirmed", "key": key,
                         "jump": jump, "run_whole_f": run_w, "bucket_lo": b_lo, "margin_f": margin_f}]

    # jump > 0 : potential break
    if not hour_ok(direction, local_hour, high_start, high_hour_end, low_start, low_end):
        return [{"action_type": "re_skip", "reason": "hour_not_in_window", "key": key, "jump": jump}]
    # Freshness = "a NEW observation arrived" (deduped by is_new_obs_time
    # above) — NOT "the observation happened within N seconds". METAR/SPECI
    # run on a 20-60 min cadence: obs_time age swings 0-60 min between
    # reports by design (US AWS publish ~7 min EARLY, others 1-8 min late).
    # An absolute age gate (<=180s) structurally killed every fire with
    # stale_obs while obs were perfectly current (0 trades, 2026-09-03).
    # Sanity window only: reject a stalled feed (>90 min behind) and
    # impossible future stamps (>15 min ahead).
    if obs_time_utc is None:
        return [{"action_type": "re_skip", "reason": "stale_obs", "key": key}]
    obs_age = (now_utc.astimezone(timezone.utc) - obs_time_utc.astimezone(timezone.utc)).total_seconds()
    if obs_age > obs_lookback_s or obs_age < -obs_future_s:
        return [{"action_type": "re_skip", "reason": "stale_obs", "key": key, "obs_age_s": round(obs_age, 1)}]

    broken = taf_b
    broken_id = str(broken.get("bucket_id") or broken.get("id") or "")

    # Bug 2 + Bug 4 — single-bucket break, state-machine guard.
    # Bug 4: only jump == 1 fires. The whole strategy rests on
    # "afternoon peak is gradual, the market consensus concentrates on one
    # bucket, the probability of two-bucket crossing in the 13:00-17:00
    # window is essentially zero". Anything else is noise (the historical
    # 2026-09-05 jump=6/2 misfires all came from this path); default to
    # skip the whole basket, not "fire YES only" — the YES-primary
    # oversize-TAF branch has done us no favours in production.
    if jump != 1:
        # A one-shot skip lock (marker written only when none exists — a real
        # fires==1 record from fire #1 must never be clobbered; a 2-bucket
        # crash inside the re-fire lane leaves the 追火 slot available).
        if key not in tree["fired"]:
            tree["fired"][key] = {
                "status": "fired_no_fill", "at_utc": iso_utc(now_utc),
                "jump": jump, "ref_source": ref_source,
                "reason": "jump_must_be_one",
            }
        tree["armed"].pop(key, None)
        return [{"action_type": "re_skip", "reason": "jump_must_be_one",
                 "key": key, "jump": jump, "ref_source": ref_source}]

    # Bug 2 — state-machine gate: IDLE -> ARMED -> FIRED. A jump>0 fire is
    # only valid if this session was armed this cycle (`armed`) or any prior
    # cycle (`ever_armed`). break_without_arm = jump was detected without
    # the prior arming confirmation; that's an "uncommitted observation"
    # path and must not place orders. The 2026-09-05 Seoul 22-fire storm
    # is partly explained by re-arm + fire falling out of the armed state
    # in prune; this gate sits between fresh arm and the fire builder so
    # it never re-fires a session that never confirmed proximity to the
    # consensus.
    ever = tree.get("ever_armed", {}).get(key)
    if armed is None and not ever:
        if key not in tree["fired"]:
            tree["fired"][key] = {
                "status": "fired_no_fill", "at_utc": iso_utc(now_utc),
                "jump": jump, "ref_source": ref_source,
                "reason": "break_without_arm",
            }
        tree["armed"].pop(key, None)
        return [{"action_type": "re_skip", "reason": "break_without_arm",
                 "key": key, "jump": jump,
                 "ref_source": ref_source, "ever_armed": bool(ever)}]

    # market-rank-1 fire path (config-gated). 2026-09-08 London EGLC lossed
    # when a market-rank-1 reference fired on a transient late obs; but
    # blocking market-ref fires outright also kills the stable-consensus
    # break (Paris 27°C / Milan 32°C both won exactly that way). Default
    # open; set allow_market_ref_fire=false to fail closed again.
    if ref_source != "taf" and not allow_market_ref_fire:
        return [{"action_type": "re_skip", "reason": "market_ref_fire_disabled",
                 "key": key, "jump": jump, "ref_source": ref_source}]

    # Long-horizon consensus filter on the *broken* bucket
    consensus_meta: dict[str, Any] = {"ok": True, "reason": "disabled"}
    if require_consensus:
        consensus_meta = tracker.is_long_horizon_consensus(
            city["city_id"],
            market_local_date,
            direction,
            broken_id,
            now_utc=now_utc,
            window_seconds=cons_win,
            min_lead=cons_min_lead,
            require_rank1=True,
            min_samples=cons_min_samples,
        )
        if not consensus_meta.get("ok"):
            return [{
                "action_type": "re_skip",
                "reason": "consensus_filter",
                "key": key,
                "jump": jump,
                "consensus": consensus_meta,
            }]

    # Jump policy: a reversal is a "reference extreme broken by one bucket".
    # The reference's trustworthiness decides how much jump slack we allow:
    #   - TAF TX/TN reference (ref_source="taf"): forecast extreme is
    #     independent of the market, so a 2-bucket breach is a rare genuine
    #     signal worth taking (NO-only).
    #   - Market rank-1 consensus reference (ref_source="market_rank1"):
    #     the reference IS the market's favourite bucket, so a large jump is
    #     usually the favourite being wrong / thin books, not an edge — the
    #     2026-09-05 jump=6 misfires (buenos-aires/qingdao/chicago) all came
    #     from this path. Fire only at exactly max_consensus_jump (1 bucket)
    #     when the reference is market-derived; anything larger is noise and
    #     is skipped outright (no NO-only fire either).
    max_consensus_jump = int(cfg.get("max_consensus_jump", MAX_BUCKET_JUMP))
    # Note: a 追火 (refiring==True) flows through the same branches below —
    # jump is guaranteed 1 by the lane gate, so it lands in the standard
    # ``else`` and builds the SAME leg pair as fire #1 (buy_no_broken on the
    # newly-broken bucket + buy_yes_new), exactly the symmetric structure the
    # operator wants (2026-09-09 reversal of the YES-only refire design).
    if jump > max_jump:
        if ref_source != "taf":
            # market-consensus reference with an oversized jump → not an edge.
            # Mark fired so the session doesn't re-arm and re-alert every tick.
            if key not in tree["fired"]:
                tree["fired"][key] = {
                    "status": "fired_no_fill", "at_utc": iso_utc(now_utc), "jump": jump,
                    "ref_source": ref_source, "reason": "jump_too_large_for_ref",
                }
            tree["armed"].pop(key, None)
            return [{"action_type": "re_skip", "reason": "jump_too_large_for_ref", "key": key,
                     "jump": jump, "ref_source": ref_source}]
        # TAF-sourced multi-bucket jump (2-bucket rare signal). YES-primary
        # strategy: keep the momentum YES leg on the observed bucket (run_b);
        # drop only the broken-bucket NO leg — its book is routinely empty
        # (holders of a practically-won NO don't sell) and the momentum side
        # is what carries the "keeps breaking" thesis.
        fire_yes = True
        new_b = run_b
        _skip_no_leg = True
        # mark the NO leg as skipped for the audit trail (re_skip_yes is now
        # semantically the NO-leg skip under yes-primary sizing)
        actions.append({"action_type": "re_skip_yes", "reason": "jump_gt_one_no_leg_skipped", "key": key, "jump": jump})
    elif jump > max_consensus_jump and ref_source != "taf":
        # same guard for the (jump <= max_jump but still > market-only cap)
        # case — unreachable while max_consensus_jump == max_jump, kept for
        # configurability if the TAF cap is later widened.
        if key not in tree["fired"]:
            tree["fired"][key] = {
                "status": "fired_no_fill", "at_utc": iso_utc(now_utc), "jump": jump,
                "ref_source": ref_source, "reason": "jump_too_large_for_ref",
            }
        tree["armed"].pop(key, None)
        return [{"action_type": "re_skip", "reason": "jump_too_large_for_ref", "key": key,
                 "jump": jump, "ref_source": ref_source}]
    else:
        fire_yes = bool(cfg.get("yes_leg_enabled", True))
        # new_b = the bucket the observed extreme has just entered. For a
        # 1-bucket breach that is run_b (the immediate neighbour of the broken
        # reference bucket). For a 2-bucket TAF breach the observed extreme
        # still sits in a concrete bucket — buy ITS yes token (momentum leg),
        # not nothing: with the yes-primary strategy the momentum leg is the
        # tradeable side (broken-bucket NO books are routinely empty because
        # holders of a practically-won NO never sell).
        new_b = run_b
        _skip_no_leg = False
    # re-skip_yes suppression: with YES-primary we no longer drop the YES leg
    # on a multi-bucket TAF jump — jump > max_jump only suppresses the NO leg.
    # The jump>max_jump / market-ref guard above still returns before here.

    fire = {
        "key": key,
        "city_id": city["city_id"],
        "icao": city.get("icao"),
        "market_local_date": market_local_date,
        "local_fire_time": _local_fire_time(city, now_utc),
        "market_unit": str(city.get("market_unit") or "C").upper(),
        "direction": direction,
        "ref_extreme": float(ref_extreme),
        "ref_source": ref_source,
        "taf_extreme": float(taf_extreme) if taf_extreme is not None else None,
        "running_extreme": running,
        "jump": jump,
        "broken_bucket_id": broken_id,
        "broken_no_token": broken.get("no_token_id") or broken.get("_no_token_id"),
        "new_bucket_id": str(new_b.get("bucket_id") or new_b.get("id") or "") if new_b else None,
        "new_yes_token": (new_b.get("yes_token_id") or new_b.get("_yes_token_id")) if new_b else None,
        "consensus": consensus_meta,
        "legs": [],
        "fire_budget_ms": int(cfg.get("fire_budget_ms", 8000)),
    }
    if not _skip_no_leg:
        fire["legs"].append({
            "leg": "buy_no_broken",
            "token_id": fire["broken_no_token"],
            "side": "BUY",
            "outcome": "NO",
            "cap": str(cfg.get("no_max_ask", NO_MAX_ASK)),
            "notional_pct": str(cfg.get("no_notional_pct", NO_NOTIONAL_PCT)),
            "floor": str(cfg.get("no_min_ask") or ""),
            "bucket_lo": broken.get("lo"),
            "bucket_hi": broken.get("hi"),
            "bucket_label": bucket_label(broken, unit),
        })
    if fire_yes and new_b is not None:
        fire["legs"].append({
            "leg": "buy_yes_new",
            "token_id": fire["new_yes_token"],
            "side": "BUY",
            "outcome": "YES",
            "cap": str(cfg.get("yes_max_ask", YES_MAX_ASK)),
            "notional_pct": str(cfg.get("yes_notional_pct", YES_NOTIONAL_PCT)),
            "floor": str(cfg.get("yes_min_ask") or ""),
            "bucket_lo": new_b.get("lo"),
            "bucket_hi": new_b.get("hi"),
            "bucket_label": bucket_label(new_b, unit),
        })
    # Bug 3 — third lock, write side: stamp the exact obs_time this fire
    # was dispatched on. Future cycles on the same key use this to reject
    # duplicate pushes before the is_new_obs_time check runs.
    new_fires = min(MAX_FIRES_PER_SESSION, fires_before + 1)
    tree["fired"][key] = {
        "status": "fired",
        "at_utc": iso_utc(now_utc),
        "jump": jump,
        "ref_source": ref_source,
        "consensus_rank": consensus_meta.get("rank"),
        "fires": new_fires,
        "new_bucket_id": fire["new_bucket_id"],
    }
    fire["fire_no"] = new_fires
    if refiring:
        fire["refire"] = True
    tree["armed"].pop(key, None)
    if obs_time_utc is not None:
        tree.setdefault("last_fire_obs", {})[key] = iso_utc(obs_time_utc)
    # Bug 2 — keep ever_armed also up to date so a re-arm this session
    # is reflected.
    tree.setdefault("ever_armed", {})[key] = iso_utc(now_utc)
    actions.append({"action_type": "re_fire", **fire})
    return actions
