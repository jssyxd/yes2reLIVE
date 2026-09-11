"""Live-cycle orchestration for the weatherbotyes2re paper reversal runner.

Reconstructs the intended ``_r_cycle`` module (the ``run_cycle`` import in
``runner_impl.py``). Pure orchestration over the existing data/strategy/fill
modules; no sigma, no live orders, paper fills only. Every externally useful
event is appended to the JSONL log for the watcher/Hermes layer.

``run_cycle(cfg, state, now_utc) -> bool``  (True while any session stays ARMed)

Cycle responsibilities (cadence-gated per-process in :mod:`_r_globals`):
  1. load the active city universe (contract registry filtered by cfg)
  2. discover today's Gamma rules (high/low bucket markets) per city, ~rules TTL
  3. dual-source METAR at metar cadence; convert to market unit
  4. refresh CLOB book ladders for every in-play token at book cadence
  5. continuously sample books into the consensus tracker (even w/o new METAR)
  6. feed each fresh METAR obs through ``maybe_arm_or_fire``
  7. on ``re_fire`` run the capped-FAK paper fire window + reserve cash + record
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import market_adapter
import re_execution
import sleeve_signal
from _r_exec import settle_markets
from _r_globals import book_cache, bump, clob, set_health_extra, stamp, tracker
from _r_state import DEFAULTS, log_event
from adapters.polymarket.orderbook import from_any
from live.port import PortRefused, get_port
from paper_capital import close_leg_at_best_bid, reserve
from research import common
from reversal_strategy import ensure_re_state, maybe_arm_or_fire, prune_stale_sessions
from ws_bridge import ws_bridge

# B2 pre-breach sleeve: process-lifetime price ring + per-key entered set so a
# session only sleeves once (dedupe against the WS book ticking every cycle).
_SLEEVE_RING = sleeve_signal.PriceRing(window_s=900.0)

LOG_FIELDS = None
ZERO = Decimal("0")
# A cached book older than this is refreshed once before the 追火 old-YES
# liquidation reads best_bid (a stale/missing book must not read as "no bid").
_CLOSE_BOOK_STALE_S = 10.0

# Which side token we sample for consensus.
# Last-good METAR map, carried so telemetry (and the watcher reading health)
# reports real per-city obs ages even between METAR fetches (~45s cadence).
_LAST_GOOD_METAR: dict[str, dict[str, Any]] = {}

# Last-good TAF parse map {ICAO: {tx_c, tn_c, issue_dt, tx_valid_utc, tn_valid_utc, raw}}.
# TAF updates only every 4-6h; on pull failure we keep last-good and retry on
# the next due cycle (fail closed, same contract as _LAST_GOOD_METAR).
_LAST_GOOD_TAF: dict[str, dict[str, Any]] = {}

# duplicate_obs_time skip logs throttled per key (expected every fast cycle at
# 5s cadence — logging all of them would add ~98 lines/cycle to the JSONL).
_DUP_LOG: dict[str, float] = {}
_DUP_LOG_INTERVAL_S = 300.0

# taf_no_extreme events throttled per ICAO (~10min): a TAF that carries no
# TX/TN is the silent reason a city has no TAF reference (and would fall back
# to the market consensus). Ops must see it once per station, not every pull.
_TAF_NO_EXTREME_LOG: dict[str, float] = {}
_TAF_NO_EXTREME_LOG_INTERVAL_S = 600.0


# --------------------------------------------------------------------------- #
# Universe + date window
# --------------------------------------------------------------------------- #
def load_active_cities(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Full registry filtered to the configured active ICAOs (or all of them)."""
    allc = common.load_cities(cfg.get("contract_cities_path"))
    active = cfg.get("active_icaos")
    if not active:
        return list(allc)
    want = {str(x).strip().upper() for x in active if str(x).strip()}
    return [c for c in allc if str(c.get("icao", "")).upper() in want]


def target_dates_by_icao(
    cities: list[dict[str, Any]],
    cfg: dict[str, Any],
    now_utc: datetime,
) -> dict[str, list[str]]:
    """Map icao -> the local calendar date being traded (today, in city tz).

    A live reversal needs real METAR now vs a *current* consensus-ranked bucket,
    so we only open today's (local) daily market. Adjacent dates are surfaced
    for information but not driven by live METAR (their books belong to forecast
    markets whose prices are set ahead of time, not by intraday observation)."""
    out: dict[str, list[str]] = {}
    for city in cities:
        icao = str(city.get("icao", "")).upper()
        z = city.get("timezone")
        if not z:
            continue
        from zoneinfo import ZoneInfo
        local_date = now_utc.astimezone(ZoneInfo(z)).date().isoformat()
        out.setdefault(icao, []).append(local_date)
    return out


def _rule_is_local_today(rule: dict[str, Any], city_by_id: dict[str, Any], now_utc: datetime) -> bool:
    """True only when ``rule``'s market_local_date is still the city's local today.

    Single source of truth for the cross-midnight date guard (2026-09-06
    incident: chicago re-fired every cycle after local midnight because prune
    dropped the fired marker while the TTL'd rules cache still fed the
    old-date rule). Fails closed: an unknown city, a missing/bad timezone, or
    a missing rule date yields False — a session whose "today" cannot be
    verified must never arm/fire/sleeve. NOTE: this guard deliberately
    imports ZoneInfo locally — the hotfix guard at the sleeve call site
    referenced an unimported ``ZoneInfo`` name, so every evaluation raised
    NameError and fell through to the fail-open branch (guard never fired).
    """
    city = city_by_id.get(rule.get("city_id"))
    if city is None:
        return False
    tz_name = city.get("timezone")
    rl_date = rule.get("market_local_date")
    if not rl_date or not tz_name:
        return False
    try:
        from zoneinfo import ZoneInfo
        return rl_date == now_utc.astimezone(ZoneInfo(tz_name)).date().isoformat()
    except Exception:  # noqa: BLE001 — bad tz entry: fail closed
        return False


# --------------------------------------------------------------------------- #
# Rule discovery
# --------------------------------------------------------------------------- #
def _rules_key(city_id: str, local_date: str, direction: str) -> str:
    return f"{city_id}|{local_date}|{direction}"


def refresh_rules(
    cfg: dict[str, Any],
    cities: list[dict[str, Any]],
    dates: dict[str, list[str]],
    now_utc: datetime,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Refresh Gamma rule discovery on TTL; returns (index, failures).

    Index keyed ``city_id|date|direction`` -> rule dict (see
    ``market_adapter.parse_event_rules``). Caches failures by key so a down
    Gamma doesn't spam the log every tick (retried only at rules TTL).

    Failure hardening (2026-09-08 KR-egress 451 incident): a full-failure
    round NEVER wipes a previously-good index — old rules stay usable (the
    caller still filters non-today dates) so one bad Gamma window cannot blind
    the whole runner; retries back off instead of storming every cycle.
    """
    ttl = float(cfg.get("rules_refresh_interval_seconds", DEFAULTS["rules_refresh_interval_seconds"]))
    if time.time() - stamp("rules") < ttl:
        idx, _ = _load_rule_cache()
        if idx:
            return idx, _RULE_MEMO["failures"]
        # Cold cache (nothing usable yet): honour the failure backoff instead
        # of re-hammering Gamma every cycle.
        if time.time() < _RULE_MEMO.get("retry_at", 0.0):
            return {}, _RULE_MEMO["failures"]
    cities_by_icao = {str(c["icao"]).upper(): c for c in cities}
    limited = {icao: cities_by_icao[icao] for icao in dates if icao in cities_by_icao}
    # Generous per-request timeout + deadline: 10 cities × 2 directions × 1 date
    # = 20 Gamma lookups. 5s/req with a 30s wall deadline lost ~30% on the first
    # soak (cold cache). Bump so one slow endpoint doesn't starve the others.
    rules, failures = market_adapter.refresh_market_rules(
        limited,
        dates,
        timeout_seconds=12.0,
        total_deadline_seconds=150.0,
    )
    idx: dict[str, Any] = {}
    for rule in rules:
        k = _rules_key(rule["city_id"], rule["market_local_date"], rule["direction"])
        # Inject the session key into the rule dict. Consumers (_sleeve_tick /
        # _enter_sleeve / action_key_in_tree / _sleeve_enter_ok) read
        # rule["key"] / rule.get("key") and would KeyError on the sleeve signal
        # path without it (sleeve_tick_error: KeyError 'key' ×53 on B2, the
        # sleeve arm never actually entered — 2026-09-06 finding).
        rule["key"] = k
        idx[k] = rule
    if rules:
        _store_rule_cache(idx, failures)
        _RULE_MEMO["retry_at"] = 0.0
    else:
        prev_idx, _ = _load_rule_cache()
        if prev_idx:
            # Full-failure round with a previously-good index: keep the old
            # rules usable (caller filters stale dates), surface the current
            # failures for health/watcher. Next attempt at TTL cadence.
            _store_rule_cache(prev_idx, failures)
        else:
            # Cold start, everything failing: keep empty but back off so the
            # runner does not storm Gamma every cycle while it is down.
            _store_rule_cache({}, failures)
            _RULE_MEMO["retry_at"] = time.time() + 120.0
    bump("rules", time.time())
    return _RULE_MEMO["idx"], failures


_RULE_MEMO: dict[str, Any] = {"idx": {}, "failures": {}, "retry_at": 0.0}


def _store_rule_cache(idx: dict[str, Any], failures: dict[str, str]) -> None:
    _RULE_MEMO["idx"] = idx
    _RULE_MEMO["failures"] = failures


def _load_rule_cache() -> tuple[dict[str, Any], dict[str, str]]:
    return _RULE_MEMO["idx"], _RULE_MEMO["failures"]


def _all_tokens_for_rules(rules: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for rule in rules.values():
        for b in rule.get("buckets", []):
            for side in ("yes_token_id", "no_token_id"):
                tok = str(b.get(side) or "")
                if tok and tok not in tokens:
                    tokens.append(tok)
    return tokens


def _rule_ref_extreme(rule: dict[str, Any], state: dict[str, Any]) -> float | None:
    """Reference extreme (market units) for a rule, from armed-session state if
    present, else None. The armed record stores the reference the strategy is
    currently watching (ref_extreme), which is exactly the bucket whose NO we
    would buy on a breach — so its ±1 neighbours are the fire-critical tokens."""
    tree = state.get("weatherbotyes2re", {})
    key = _rules_key(rule.get("city_id"), rule.get("market_local_date"), rule.get("direction"))
    armed = (tree.get("armed") or {}).get(key)
    if armed and armed.get("ref_extreme") is not None:
        try:
            return float(armed["ref_extreme"])
        except (TypeError, ValueError):
            return None
    return None


def _warm_tokens_for_rules(rules: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Fire-critical token subset to keep hot while sessions are armed.

    For every rule with an armed session we derive the reference bucket from
    the armed ref_extreme, then collect:
      - the reference bucket's NO token   (fire leg: BUY NO broken bucket)
      - the neighbour bucket on the likely breach side — high direction: the
        next-higher bucket (a higher observed extreme breaks into it, so its
        YES is the second fire leg); low direction: the next-lower bucket
      - that neighbour's NO token too (so a 2-bucket TAF breach can still fill)
    Buckets are located in temperature order around the reference. Boundary
    buckets (lo/hi None — "≤x" / "≥x") are handled the same way the strategy
    does: a None bound never excludes a value on that side.

    Returns a small deduped token list (tens, not the ~1000-token full set) so
    the armed fast-poll can refresh just these every few seconds and a fire
    finds a warm book instead of no_book.
    """
    warm: list[str] = []
    for rule in rules.values():
        ref = _rule_ref_extreme(rule, state)
        if ref is None:
            continue
        buckets = rule.get("buckets", [])
        if not buckets:
            continue
        # order by lo where present; open lower bound sorts first
        def _lo_key(b: dict[str, Any]) -> float:
            lo = b.get("lo")
            return float(lo) if lo is not None else float("-inf")
        ordered = sorted(buckets, key=_lo_key)
        # find the bucket containing the reference extreme (None bound = open)
        idx = None
        for i, b in enumerate(ordered):
            lo = b.get("lo")
            hi = b.get("hi")
            if (lo is None or ref >= float(lo)) and (hi is None or ref < float(hi)):
                idx = i
                break
        if idx is None:
            continue
        ref_bucket = ordered[idx]
        # likely breach neighbour
        if rule.get("direction") == "high":
            nxt = ordered[idx + 1] if idx + 1 < len(ordered) else None
        else:
            nxt = ordered[idx - 1] if idx - 1 >= 0 else None
        cands = [ref_bucket]
        if nxt is not None:
            cands.append(nxt)
        for b in cands:
            for side in ("no_token_id", "yes_token_id"):
                tok = str(b.get(side) or "")
                if tok and tok not in warm:
                    warm.append(tok)
    return warm


def _warm_token_icao_map(
    rules: dict[str, Any],
    cities: list[dict[str, Any]],
    armed_keys: set[str] | None = None,
) -> dict[str, str]:
    """Reverse map token id -> station ICAO for the armed rules' buckets.

    Iterates every rule bucket (YES/NO token ids) and maps the owning rule's
    ``city_id`` (first segment of the rule key) to its ICAO via ``cities``.
    Used by the R1 early-pull to know which METAR station to fetch when a
    fire-critical token's WS book reprices."""
    city_by_id = {str(c.get("city_id")): c for c in cities if c.get("city_id") is not None}
    out: dict[str, str] = {}
    for key, rule in (rules or {}).items():
        if armed_keys is not None and key not in armed_keys:
            continue
        city = city_by_id.get(str(key).split("|", 1)[0])
        if city is None:
            continue
        icao = str(city.get("icao")).upper()
        for b in rule.get("buckets", []) or []:
            for side in ("yes_token_id", "no_token_id"):
                tok = str(b.get(side) or "")
                if tok:
                    out[tok] = icao
    return out


def _normalize_snapshot(token_id: str, snapshot: Any) -> dict[str, Any] | None:
    """Turn any CLOB/WS book snapshot into the pure ladder-dict the strategy
    and ``paper_match_fak`` were written against:
      {best_ask, best_bid, tick_size, asks:[{price,size}], bids:[...]}"""
    view = from_any(snapshot, token_id=token_id)
    if view is None:
        return None
    if not view.asks and not view.bids:
        return None
    asks = [{"price": str(p), "size": str(s)} for p, s in view.asks]
    bids = [{"price": str(p), "size": str(s)} for p, s in view.bids]
    return {
        "best_ask": str(view.best_ask) if view.best_ask is not None else (asks[0]["price"] if asks else None),
        "best_bid": str(view.best_bid) if view.best_bid is not None else (bids[0]["price"] if bids else None),
        "tick_size": str(view.tick_size) if view.tick_size is not None else "0.01",
        "asks": asks,
        "bids": bids,
        "fetched_at_epoch": snapshot.fetched_at_epoch if hasattr(snapshot, "fetched_at_epoch") else time.time(),
    }


def refresh_books(cfg: dict[str, Any], token_ids: list[str], now_utc: datetime) -> dict[str, Any]:
    """Fetch CLOB books for ``token_ids`` and warm the process-ladder cache.

    Returns {token_id: book_dict} for tokens that returned a live ladder; tokens
    with no executable side are omitted so downstream reads fail closed."""
    cache = book_cache()
    fresh: dict[str, Any] = {}
    if not token_ids:
        return fresh
    try:
        fetched = clob(cfg.get("clob_timeout_seconds", 8.0)).fetch_books(token_ids)
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "book_fetch_failed", "error": f"{type(exc).__name__}: {exc}"})
        return fresh
    for tid, snap in fetched.items():
        ladder = _normalize_snapshot(tid, snap)
        if ladder is None:
            continue
        cache[tid] = ladder
        fresh[tid] = ladder
    return fresh


def _ws_pump(wsb: Any, cache: dict[str, Any], max_age_s: float = 5.0) -> int:
    """Overlay fresh (<max_age_s) WS-fed LocalOrderBook snapshots onto the
    ladder cache. A snapshot only overwrites the cached ladder when it is
    strictly newer (fetched_at_epoch comparison) — a quiet/stale WS book can
    never clobber a fresher REST ladder."""
    stream = getattr(wsb, "stream", None)
    if stream is None:
        return 0
    n = 0
    now_e = time.time()
    for tid, lb in list(stream.books.items()):
        try:
            if lb.is_fresh(max_age_s, now=now_e):
                snap = lb.snapshot()
                ladder = _normalize_snapshot(tid, snap)
                if ladder is None:
                    continue
                cur = cache.get(tid)
                new_epoch = float(ladder.get("fetched_at_epoch") or 0)
                old_epoch = float((cur or {}).get("fetched_at_epoch") or 0)
                if new_epoch >= old_epoch:
                    cache[tid] = ladder
                    n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


# WS-triggered METAR pull (R1): when an armed session's fire-critical token
# sees a live WS book update (the market repriced = someone likely read a new
# obs we have not pulled yet), pull that ICAO's METAR immediately instead of
# waiting for the next armed 5s cadence. Cooldown per ICAO prevents request
# storms from a churning book.
_WS_METAR_COOLDOWN: dict[str, float] = {}
_WS_METAR_COOLDOWN_S = 3.0


def _recent_ws_tokens(wsb: Any, max_age_s: float = 4.0) -> set[str]:
    """Token ids whose WS LocalOrderBook received an update within max_age_s.

    Used by the R1 early-pull: a fresh WS tick on a fire-critical token means
    the market repriced — pull the underlying obs now, don't wait for the
    5s armed cadence (mirror-lag reduction: we are not the last reader of a
    published METAR, but we can stop being the *second*-last)."""
    stream = getattr(wsb, "stream", None)
    if stream is None:
        return set()
    now_e = time.time()
    out: set[str] = set()
    for tid, lb in list(stream.books.items()):
        try:
            if getattr(lb, "received_at_epoch", 0.0) > 0 and (now_e - lb.received_at_epoch) <= max_age_s:
                out.add(str(tid))
        except Exception:  # noqa: BLE001
            continue
    return out


# --------------------------------------------------------------------------- #
# METAR
# --------------------------------------------------------------------------- #
def _metar_age_s(obs: dict[str, Any], now_utc: datetime | None = None) -> float:
    """Age of a METAR obs dict (its ``obs_time`` ISO field) in seconds from
    now. Returns -1 when the obs has no parseable timestamp (caller treats as
    unknown age rather than a misleading 0)."""
    raw = obs.get("obs_time") or obs.get("obs_dt")
    if not raw:
        return -1.0
    try:
        if isinstance(raw, datetime):
            dt = raw
        else:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        base = now_utc if now_utc is not None else datetime.now(timezone.utc)
        return max(0.0, (base - dt).total_seconds())
    except Exception:  # noqa: BLE001
        return -1.0


def _fetch_metar(
    cfg: dict[str, Any],
    icaos: list[str],
    now_utc: datetime,
) -> dict[str, dict[str, Any]]:
    """Dual-source METAR (CheckWX primary, AWC backup) for the given ICAO set.

    The caller picks the subset: whole universe on the idle cadence, armed
    ICAOs only on the fast cadence. Returns {icao: {raw, temp_c, obs_time,
    source}}; on failure logs ``metar_fetch_failed`` and returns {} so the
    caller keeps last-good obs and retries next cycle (fail closed)."""
    icaos = sorted({str(i).upper() for i in icaos})
    if not icaos:
        return {}
    api_key = cfg.get("_checkwx_key")
    try:
        return common.dual_source_metar(icaos, api_key, now=now_utc)
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "metar_fetch_failed", "error": f"{type(exc).__name__}: {exc}"})
        return {}


def _fetch_taf(
    cfg: dict[str, Any],
    icaos: list[str],
    now_utc: datetime,
) -> dict[str, dict[str, Any]]:
    """Fetch + parse TAF TX/TN for the given ICAO set via CheckWX.

    Returns {ICAO: {tx_c, tn_c, issue_dt, tx_valid_utc, tn_valid_utc, raw}}.
    Fail closed: on any error logs ``taf_fetch_failed`` and returns {} so the
    caller keeps last-good TAFs and retries next due cycle. Without a CheckWX
    key TAF is disabled (returns {}) and the strategy falls back to the
    market-rank-1 consensus reference. A TAF that parses to no TX/TN logs a
    throttled ``taf_no_extreme`` event (icao + truncated raw) and is omitted,
    so ops can see why a city has no TAF reference.
    """
    icaos = sorted({str(i).upper() for i in icaos})
    if not icaos:
        return {}
    api_key = cfg.get("_checkwx_key")
    if not api_key:
        return {}
    try:
        raw_by_icao = common.checkwx_taf(icaos, api_key)
        out: dict[str, dict[str, Any]] = {}
        for icao, raw in raw_by_icao.items():
            parsed = common.parse_tx_tn(raw)
            if not parsed:
                # Short/odd TAF with no TX/TN: this city then has NO TAF
                # reference and the strategy arm/watch falls back to market
                # consensus — surface why (rate-limited ~10min per ICAO).
                last = _TAF_NO_EXTREME_LOG.get(icao)
                if last is None or time.time() - last >= _TAF_NO_EXTREME_LOG_INTERVAL_S:
                    _TAF_NO_EXTREME_LOG[icao] = time.time()
                    log_event(cfg.get("log_path"), {
                        "type": "taf_no_extreme",
                        "icao": icao,
                        "raw": raw[:200],
                    })
                continue
            issue_dt = common.parse_taf_issue_time(raw, ref_utc=now_utc)
            entry: dict[str, Any] = {
                "raw": raw,
                "tx_c": parsed.get("tx_c"),
                "tn_c": parsed.get("tn_c"),
                "issue_dt": issue_dt.isoformat() if issue_dt else None,
            }
            if issue_dt is not None:
                tx_day = parsed.get("tx_day")
                tx_hour = parsed.get("tx_hour")
                if tx_day and tx_hour is not None and parsed.get("tx_c") is not None:
                    v = common.resolve_tx_valid_utc(issue_dt, tx_day, int(tx_hour))
                    if v is not None:
                        entry["tx_valid_utc"] = v.isoformat()
                # TN uses the same day/hour fields on the TN side; resolve only
                # when parse_tx_tn returned a TN day/hour (it stores them under
                # tn_day/tn_hour). resolve_tx_valid_utc's day parsing expects
                # the TX-style day token; TN tokens share the same format.
                tn_day = parsed.get("tn_day")
                tn_hour = parsed.get("tn_hour")
                if tn_day and tn_hour is not None and parsed.get("tn_c") is not None:
                    v = common.resolve_tx_valid_utc(issue_dt, tn_day, int(tn_hour))
                    if v is not None:
                        entry["tn_valid_utc"] = v.isoformat()
            out[icao] = entry
        return out
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "taf_fetch_failed", "error": f"{type(exc).__name__}: {exc}"})
        return {}


def _armed_icaos(armed_keys: set[str], cities: list[dict[str, Any]]) -> set[str]:
    """Map armed session keys (``city_id|local_date|direction``) to station ICAOs.

    The fast METAR pull only re-polls the armed cities' stations, so a fresh
    obs reaches ``maybe_arm_or_fire`` on the ~10s armed cadence instead of
    waiting for the next ~60s full-universe pull."""
    city_by_id = {str(c.get("city_id")): c for c in cities if c.get("city_id") is not None}
    icaos: set[str] = set()
    for key in armed_keys or ():
        city = city_by_id.get(str(key).split("|", 1)[0])
        if city is not None:
            icaos.add(str(city.get("icao")).upper())
    return icaos


# --------------------------------------------------------------------------- #
# B2 sleeve helpers (paper entry path, shares the capped-FAK fire window)
# --------------------------------------------------------------------------- #
def action_key_in_tree(tree: dict[str, Any], rule: dict[str, Any]) -> bool:
    """True when the session already fired (breach happened) — no sleeve on a
    done deal."""
    return rule.get("key") in tree.get("fired", {})


def _sleeve_enter_ok(state: dict[str, Any], rule: dict[str, Any]) -> bool:
    """Dedupe: one sleeve entry per session, tracked on state (survives
    restart, unlike an in-memory set)."""
    sleeves = state.setdefault("weatherbotyes2re", {}).setdefault("sleeves", {})
    return rule.get("key") not in sleeves


def _sleeve_tick(
    cfg: dict[str, Any],
    state: dict[str, Any],
    rule: dict[str, Any],
    rule_books: dict[str, dict[str, Any]],
    tree: dict[str, Any],
    now_utc: datetime,
) -> None:
    """One B2 detection tick for a single (today-dated) rule: feed the price
    ring from current books, locate the rank-1 bucket, and on a structure
    signal enter a small sleeve via the shared paper window."""
    srule = rule.get("buckets") or []
    if not srule:
        return
    try:
        now_e = time.time()
        sleeve_signal.update_rings_from_books(_SLEEVE_RING, srule, rule_books, now_e)
        session_fired = action_key_in_tree(tree, rule)
        r1_idx = sleeve_signal.locate_rank1_bucket(srule, rule_books, _SLEEVE_RING, now_e)
        if session_fired or r1_idx is None or not _sleeve_enter_ok(state, rule):
            return
        sleeve_cfg = cfg.get("strategy") or {}
        det = sleeve_signal.detect_sleeve_signal(
            buckets=srule,
            rank1_bucket_idx=r1_idx,
            direction=rule.get("direction", "high"),
            books_by_token=rule_books,
            ring=_SLEEVE_RING,
            now=now_e,
            short_window_s=float(sleeve_cfg.get("sleeve_short_window_s", 150)),
            long_window_s=float(sleeve_cfg.get("sleeve_long_window_s", 600)),
            rank1_weaken_drop=Decimal(str(sleeve_cfg.get("sleeve_rank1_drop", 0.05))),
            neighbour_strengthen_rise=Decimal(str(sleeve_cfg.get("sleeve_neighbour_rise", 0.04))),
            neighbour_max_ask=Decimal(str(sleeve_cfg.get("sleeve_max_ask", 0.35))),
        )
        if det is not None:
            n_idx, sig = det
            sig.key = rule.get("key", "")
            sig.city_id = rule.get("city_id", "")
            sig.market_local_date = rule.get("market_local_date", "")
            log_event(cfg.get("log_path"), sig.as_event(now_utc.isoformat()))
            _enter_sleeve(cfg, state, rule, srule, n_idx, sig, now_utc)
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "sleeve_tick_error", "error": f"{type(exc).__name__}: {exc}"})


def _enter_sleeve(
    cfg: dict[str, Any],
    state: dict[str, Any],
    rule: dict[str, Any],
    buckets: list[dict[str, Any]],
    n_idx: int,
    sig: sleeve_signal.SleeveSignal,
    now_utc: datetime,
) -> None:
    """Build a sleeve fire (single neighbour-YES leg, small budget) and run it
    through the shared paper fire window. Key is \"<session>#sleeve\" so the
    position ledger never collides with the main reversal position."""
    try:
        ordered = sorted(buckets, key=lambda b: float(b.get("lo") if b.get("lo") is not None else float("-inf")))
        if not (0 <= n_idx < len(ordered)):
            return
        nbr = ordered[n_idx]
        yes_tok = str(nbr.get("yes_token_id") or "")
        bucket_id = str(nbr.get("bucket_id") or nbr.get("id") or "")
        if not yes_tok:
            return
        sleeve_cfg = cfg.get("strategy") or {}
        max_ask = str(sleeve_cfg.get("sleeve_max_ask", 0.35))
        sess_key = str(rule.get("key") or "")
        if not sess_key:
            # Defensive: a rule without an injected session key cannot be
            # recorded as a sleeve — abort rather than fabricate a bad key.
            log_event(cfg.get("log_path"), {"type": "sleeve_error", "error": "sleeve rule missing key"})
            return
        sleeve_key = f"{sess_key}#sleeve"
        fire: dict[str, Any] = {
            "action_type": "re_sleeve",
            "key": sleeve_key,
            "city_id": rule.get("city_id"),
            "icao": rule.get("icao"),
            "market_local_date": rule.get("market_local_date"),
            "direction": rule.get("direction"),
            "sleeve": True,
            "sleeve_signal_reason": sig.reason,
            "ref_source": "book_structure",
            "jump": 0,
            "legs": [
                {
                    "leg": "buy_yes_sleeve",
                    "token_id": yes_tok,
                    "side": "BUY",
                    "outcome": "YES",
                    "cap": max_ask,
                    "notional_pct": "1.0",
                }
            ],
            "new_bucket_id": bucket_id,
        }
        position, ladlog = _paper_fire(cfg, state, fire, now_utc)
        if position is not None:
            pos = state.setdefault("positions", {})
            pos[sleeve_key] = position
            tree2 = state.setdefault("weatherbotyes2re", {})
            tree2.setdefault("sleeves", {})[sess_key] = {
                "entered_at_utc": re_execution.iso_utc(now_utc),
                "bucket_id": bucket_id,
                "token_id": yes_tok,
                "status": "open",
            }
            log_event(
                cfg.get("log_path"),
                {
                    "type": "sleeve_entered",
                    "key": sleeve_key,
                    "session_key": sess_key,
                    "bucket_id": bucket_id,
                    "reason": sig.reason,
                    "fills": {
                        str(lg.get("leg")): {"shares": lg.get("shares"), "cost": lg.get("cost_usdc")}
                        for lg in (position.get("legs") or [])
                    },
                    "ts_utc": now_utc.isoformat(),
                },
            )
        else:
            # nothing fillable — record intent so we don't retry every cycle
            tree2 = state.setdefault("weatherbotyes2re", {})
            tree2.setdefault("sleeves", {})[sess_key] = {
                "entered_at_utc": re_execution.iso_utc(now_utc),
                "bucket_id": bucket_id,
                "token_id": yes_tok,
                "status": "no_fill",
            }
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "sleeve_error", "error": f"{type(exc).__name__}: {exc}"})


def _expire_stale_sleeves(cfg: dict[str, Any], state: dict[str, Any], now_utc: datetime) -> None:
    """Time out pre-breach sleeves whose session never fired.

    A sleeve is a bet that the market's book rotation is *information* (a new
    obs is about to confirm a breach). If the session has not fired within
    ``sleeve_timeout_s`` the move was sentiment churn — close the sleeve at the
    current book to stop the bleed. Paper close: sell shares at best_bid
    (release proceeds back to the pool); if no bid exists the leg is written
    off at full cost (like a losing settlement)."""
    tree = state.setdefault("weatherbotyes2re", {})
    sleeves = tree.get("sleeves", {})
    if not sleeves:
        return
    sleeve_cfg = cfg.get("strategy") or {}
    timeout_s = float(sleeve_cfg.get("sleeve_timeout_s", 1800))
    cache = book_cache()
    positions = state.setdefault("positions", {})
    now_epoch = time.time()
    for sess_key, rec in list(sleeves.items()):
        if rec.get("status") != "open":
            continue
        # Session fired -> the sleeve was validated (its neighbour YES either
        # won or will resolve normally). Leave it to passive settlement.
        if sess_key in tree.get("fired", {}):
            rec["status"] = "validated_by_breach"
            rec["validated_at_utc"] = re_execution.iso_utc(now_utc)
            continue
        try:
            entered = datetime.fromisoformat(str(rec.get("entered_at_utc", "")).replace("Z", "+00:00"))
            age_s = (now_utc - entered).total_seconds()
        except Exception:  # noqa: BLE001
            age_s = 0.0
        if age_s < timeout_s:
            continue
        # Timeout: close the sleeve position leg at current best bid.
        pos_key = f"{sess_key}#sleeve"
        pos = positions.get(pos_key)
        if pos is None or pos.get("settled"):
            rec["status"] = "expired_no_position"
            continue
        leg = None
        for lg in pos.get("legs", []):
            if not lg.get("settled"):
                leg = lg
                break
        if leg is None:
            rec["status"] = "expired_no_leg"
            continue
        # Shared paper close (best_bid sell / write-off at 0 when no bid) —
        # the same helper the 追火 old-leg liquidation uses (2026-09-09).
        close = close_leg_at_best_bid(
            state, leg, books=cache, closed_by="sleeve_timeout",
            settled_at_utc=re_execution.iso_utc(now_utc),
        )
        if close is None:
            rec["status"] = "expired_no_leg"
            continue
        proceeds = Decimal(str(close["proceeds_usdc"]))
        bid = close.get("bid")
        if all(lg.get("settled") for lg in pos.get("legs", [])):
            pos["settled"] = True
            pos["settled_at_utc"] = re_execution.iso_utc(now_utc)
        rec["status"] = "expired"
        rec["expired_at_utc"] = re_execution.iso_utc(now_utc)
        rec["close_proceeds_usdc"] = str(proceeds)
        rec["bid_at_close"] = str(bid) if bid is not None else None
        log_event(
            cfg.get("log_path"),
            {
                "type": "sleeve_timeout",
                "session_key": sess_key,
                "position_key": pos_key,
                "age_s": round(age_s, 1),
                "shares": close.get("shares"),
                "proceeds_usdc": str(proceeds),
                "bid_at_close": str(bid) if bid is not None else None,
                "ts_utc": now_utc.isoformat(),
            },
        )
    # --- bookkeeping sweep (2026-09-06 review, residual-state cleanup) ---- #
    # Drop sleeve *records* that are no longer protecting anything: status is
    # closed (validated/expired) AND the sleeve's paper position is settled or
    # gone. "open" records are left to the timeout logic above; "no_fill" /
    # "expired_no_position" records stay so an unfillable session is not
    # re-attempted every cycle (dedupe intent). Positions settle passively via
    # settle_markets, at which point the validated record becomes sweepable.
    for sess_key, rec in list(sleeves.items()):
        if rec.get("status") in ("open", "no_fill", "expired_no_position"):
            continue
        pos = positions.get(f"{sess_key}#sleeve")
        if pos is not None and not pos.get("settled"):
            continue
        del sleeves[sess_key]


# --------------------------------------------------------------------------- #
# Fire (paper) window
# --------------------------------------------------------------------------- #
def _paper_fire(
    cfg: dict[str, Any],
    state: dict[str, Any],
    fire: dict[str, Any],
    now_utc: datetime,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Run the capped-FAK paper fill for a ``re_fire`` event over warmed books.

    Mirrors the sim's ``run_fire_window`` but against the live normalized
    ladder cache, reserving real paper cash and recording the position on the
    state blob. Returns (position_legs_or_None, ladder_log).

    The fill itself goes through the execution port (``live/port.py``): paper keeps the
    in-memory capped FAK matcher, live sends these very same intents to the real CLOB
    ("how a fill happens" is the only difference). Strategy, sizing, windows, state
    schema and bookkeeping stay identical in both modes.
    """
    try:
        port = get_port(cfg)
    except PortRefused as exc:
        # live requested without its gates: stand down loudly, never downgrade to paper
        log_event(cfg.get("log_path"), {"type": "fire_port_refused", "key": fire.get("key"),
                                        "reason": exc.reason, "detail": exc.detail})
        return None, [{"status": "port_refused", "reason": exc.reason, "detail": exc.detail}]
    pre = port.preflight(fire=fire, cfg=cfg)
    if not pre["ok"]:
        log_event(cfg.get("log_path"), {"type": "fire_port_refused", "key": fire.get("key"),
                                        "reason": pre["reason"], "detail": pre["detail"]})
        return None, [{"status": "port_refused", "reason": pre["reason"], "detail": pre["detail"]}]
    cache = book_cache()
    is_sleeve = bool(fire.get("sleeve"))
    if is_sleeve:
        # B2 pre-breach sleeve: a small fraction of the fire budget on a single
        # neighbour-YES leg. budget is fire_budget × sleeve_notional_pct.
        pct = Decimal(str(fire.get("sleeve_notional_pct") or cfg.get("sleeve_notional_pct", 0.08)))
        budget = (Decimal(str(cfg.get("fire_budget_usdc", DEFAULTS["fire_budget_usdc"]))) * pct).quantize(Decimal("0.01"))
    else:
        budget = Decimal(str(cfg.get("fire_budget_usdc", DEFAULTS["fire_budget_usdc"])))
    remaining = re_execution.size_legs(fire, budget)
    fills: dict[str, dict[str, Any]] = {}
    ladlog: list[dict[str, Any]] = []
    for leg in fire.get("legs", []):
        name = str(leg["leg"])
        fills[name] = {"shares": ZERO, "cost": ZERO, "fill_price": None}
    budget_ms = int(fire.get("fire_budget_ms") or cfg.get("fire_budget_ms", 8000))
    now_ms = int(now_utc.timestamp() * 1000)

    # If every leg's token is missing from the cache on the first attempt, do
    # one targeted refresh of exactly the fire's tokens before the ladder
    # starts — a fire that trips while the armed fast-poll hasn't refreshed
    # yet (e.g. fresh arm on this very cycle) would otherwise spin all three
    # ladder rungs as no_book and fill nothing.
    fire_tokens = [str(l.get("token_id") or "") for l in fire.get("legs", []) if l.get("token_id")]
    missing = [t for t in fire_tokens if t not in cache]
    if missing:
        refresh_books(cfg, missing, now_utc)
        cache = book_cache()

    for elapsed in (0, 1500, 4000):
        if elapsed > budget_ms:
            break
        intents = re_execution.plan_fire_cycle(
            fire,
            cache,  # live ladder dicts keyed by token
            remaining,
            now_utc + timedelta(milliseconds=elapsed),
            elapsed,
            budget_ms=budget_ms,
        )
        for intent in intents:
            # always log FAK ladder intent
            ladlog.append(intent)
            if intent.get("status") != "send_fak":
                continue
            match = port.match(
                leg=intent,
                book=cache.get(intent.get("token_id")) if intent.get("token_id") in cache else {},
                limit=Decimal(intent["limit_price"]),
                shares=Decimal(intent["shares"]),
                fire=fire,
                cfg=cfg,
            )
            fills[intent["leg"]]["shares"] += match["filled_shares"]
            fills[intent["leg"]]["cost"] += match["cost"]
            remaining[intent["leg"]] = match["unfilled"]
            intent["fill"] = {
                "filled": str(match["filled_shares"]),
                "avg": str(match["avg_price"]) if match["avg_price"] is not None else None,
                "unfilled": str(match["unfilled"]),
            }
            if match["avg_price"] is not None:
                fills[intent["leg"]]["fill_price"] = str(match["avg_price"])

    total_cost = sum((fills[k]["cost"] for k in fills), ZERO)
    # Fail closed: if the ledger cannot book the filled cost, stand the whole fire down
    # (do not record a position we cannot fund). The ledger is shared by every mode.
    funding = port.fund(state=state, cfg=cfg, fire=fire, total_cost=total_cost)
    if total_cost > ZERO and not funding["ok"]:
        log_event(cfg.get("log_path"), {"type": "fire_insufficient_capital", "key": fire["key"], "need": str(total_cost)})
        return None, ladlog
    # Start ledger baseline: paper_account debit already incremented by reserve.

    position = {
        "key": fire["key"],
        "kind": "sleeve" if is_sleeve else "reversal",
        "city_id": fire.get("city_id"),
        "icao": fire.get("icao"),
        "market_local_date": fire.get("market_local_date"),
        "local_fire_time": fire.get("local_fire_time"),
        "market_unit": fire.get("market_unit"),
        "direction": fire.get("direction"),
        "fires_at_utc": re_execution.iso_utc(now_utc),
        "ref_extreme": fire.get("ref_extreme"),
        "ref_source": fire.get("ref_source"),
        "running_extreme": fire.get("running_extreme"),
        "jump": fire.get("jump"),
        "budget_usdc": str(budget),
        "settled": False,
        "legs": [],
    }
    # Map legs back to position-level fields for settlement bookkeeping.
    pos_bucket_by_leg = {str(l["leg"]): l for l in fire.get("legs", [])}
    pos_legs_by_name: dict[str, dict[str, Any]] = {}
    for leg in fire.get("legs", []):
        name = str(leg["leg"])
        fl = fills.get(name, {})
        pos_legs_by_name[name] = {
            "leg": name,
            "token_id": leg.get("token_id"),
            "side": leg.get("side"),
            "outcome": leg.get("outcome"),
            "cap": leg.get("cap"),
            "notional_pct": leg.get("notional_pct"),
            "cost_usdc": str(fl["cost"]),
            "shares": str(fl["shares"]),
            "avg_price": fl["fill_price"],
            "bucket_id": None,
            "bucket_lo": leg.get("bucket_lo"),
            "bucket_hi": leg.get("bucket_hi"),
            "bucket_label": leg.get("bucket_label"),
            "settled": False,
            "leg_won": None,
        }
    # attach bucket_id by matching back through the fire leg spec (broken_no/new_yes)
    for leg in fire.get("legs", []):
        name = str(leg["leg"])
        if name == "buy_no_broken":
            bucket_id = fire.get("broken_bucket_id")
        elif name in ("buy_yes_new", "buy_yes_sleeve"):
            bucket_id = fire.get("new_bucket_id")
        else:
            continue
        pos_legs_by_name[name]["bucket_id"] = bucket_id
    position["legs"] = list(pos_legs_by_name.values())

    return position, ladlog


def _record_fire_event(cfg, state, fire, position, ladlog, now_utc) -> None:
    ensure_re_state(state)
    legs = position.get("legs") or []
    fill_summary = {
        str(lg.get("leg")): {"shares": lg.get("shares"), "cost": lg.get("cost_usdc"), "avg": lg.get("avg_price")}
        for lg in legs
    }
    state["entry_count"] = int(state.get("entry_count") or 0) + 1
    pos = state.setdefault("positions", {})
    # Merge repeated fires for the same key are impossible (one fire per session),
    # but guard against clobbering anyway.
    prev = pos.get(fire["key"])
    if prev is not None and prev.get("settled") is not True:
        pass  # key already recorded; keep first, log anomaly
    else:
        pos[fire["key"]] = position
    log_event(
        cfg.get("log_path"),
        {
            "type": "fire",
            "key": fire.get("key"),
            "fire_no": int(fire.get("fire_no") or 1),
            "city_id": fire.get("city_id"),
            "icao": fire.get("icao"),
            "direction": fire.get("direction"),
            "jump": fire.get("jump"),
            "ref_source": fire.get("ref_source"),
            "fills": fill_summary,
            # First-hand evidence for why a fire did not fill: every FAK
            # ladder intent (send_fak / abort_above_cap / no_book /
            # abort_timeout / missing_token) with best_ask/cap/limit so the
            # 15-min watcher can reverse-trace no-fill causes without guessing.
            "ladder": [
                {k: v for k, v in intent.items() if k != "fill"} | ({"fill": intent.get("fill")} if intent.get("fill") else {})
                for intent in ladlog
            ],
        },
    )


def record_refire(
    cfg: dict[str, Any],
    state: dict[str, Any],
    fire: dict[str, Any],
    position: dict[str, Any],
    ladlog: list[dict[str, Any]],
    now_utc: datetime,
) -> None:
    """Second-fire (追火) ledger write for a session that already fired once.

    The 追火腿 is structurally symmetric with fire #1 (buy_no_broken on the
    newly-broken bucket + buy_yes_new), so ``position`` may carry both legs
    under the same names as fire #1's row; both are merged as filled legs.
    Fire #1's position is still open under the session key. Once the refire's
    new-bucket YES leg actually filled:
      1. sell fire #1's OLD-bucket YES leg at best_bid — proceeds released
         back to the pool; a missing/no-bid book writes the leg off at 0
         (shared ``close_leg_at_best_bid``; the refire's NO leg on that very
         bucket is its book-side offset, a no-fill there is acceptable).
      2. append the refire's filled legs to the SAME position record so a
         session keeps one ledger row (prune / settlement / reports
         invariants — one position per session key) with an auditable
         ``refire_*`` stamp on it.
    A refire that fills nothing consumes its fire slot (fired.fires == 2) but
    never liquidates the old leg — no new YES secured, the old leg rides to
    settlement.
    """
    ensure_re_state(state)
    sess = fire.get("key")
    positions = state.setdefault("positions", {})
    old = positions.get(sess)
    new_legs = [
        lg for lg in (position.get("legs") or [])
        if Decimal(str(lg.get("shares") or 0)) > ZERO
    ]
    if not new_legs:
        # Refire filled nothing (below floor / above cap / empty book): the
        # fire slot is consumed (fired.fires == 2, no retry) but the ledger is
        # untouched — fire #1's old YES leg rides to settlement.
        state["entry_count"] = int(state.get("entry_count") or 0) + 1
        log_event(
            cfg.get("log_path"),
            {
                "type": "fire",
                "key": sess,
                "fire_no": int(fire.get("fire_no") or 2),
                "city_id": fire.get("city_id"),
                "icao": fire.get("icao"),
                "direction": fire.get("direction"),
                "jump": fire.get("jump"),
                "ref_source": fire.get("ref_source"),
                "refire": True,
                "refire_unfilled": True,
                "fills": {},
                "ladder": [
                    {k: v for k, v in intent.items() if k != "fill"} | ({"fill": intent.get("fill")} if intent.get("fill") else {})
                    for intent in ladlog
                ],
                "ts_utc": now_utc.isoformat(),
            },
        )
        return
    # The 追火腿 carries the SAME leg names as fire #1 (buy_no_broken +
    # buy_yes_new, symmetric by operator decision 2026-09-09) — detect the
    # filled refire YES leg by outcome, not by name.
    filled_refire_yes = any(
        str(lg.get("outcome") or "").upper() == "YES" for lg in new_legs
    )
    close: dict[str, Any] | None = None
    if old is not None and not old.get("settled") and filled_refire_yes:
        old_yes = next(
            (
                lg for lg in (old.get("legs") or [])
                if not lg.get("settled")
                and str(lg.get("outcome") or "").upper() == "YES"
                and Decimal(str(lg.get("shares") or 0)) > ZERO
            ),
            None,
        )
        if old_yes is not None:
            # Freshness guard on the old YES leg's book (audit follow-up): a
            # transient fetch gap must not be read as "no bid" and silently
            # write the leg off at 0. Mirror _paper_fire's missing-token
            # refresh — when the token is not cached or its cached ladder is
            # stale (> _CLOSE_BOOK_STALE_S), pull it once, then close off the
            # refreshed book. Still no book after the attempt -> write off at
            # 0 with book_unavailable=True in the close_old_yes event.
            old_tok = str(old_yes.get("token_id") or "")
            close_books = book_cache()
            book = close_books.get(old_tok) if old_tok else None
            book_unavailable = False
            fetched = (book or {}).get("fetched_at_epoch")
            stale = not old_tok or book is None or (fetched is not None
                                                    and time.time() - float(fetched) > _CLOSE_BOOK_STALE_S)
            if stale and old_tok:
                try:
                    refresh_books(cfg, [old_tok], now_utc)
                except Exception:  # noqa: BLE001 — refresh best-effort; fall through to 0
                    pass
                close_books = book_cache()
                book = close_books.get(old_tok) if old_tok else None
            book_unavailable = not old_tok or book is None
            close = close_leg_at_best_bid(
                state, old_yes, books=close_books, closed_by="refire_liquidation",
                settled_at_utc=re_execution.iso_utc(now_utc),
            )
            if close is not None:
                try:
                    cost = Decimal(str(old_yes.get("cost_usdc") or 0))
                except Exception:  # noqa: BLE001
                    cost = ZERO
                loss = max(ZERO, cost - Decimal(str(close["proceeds_usdc"])))
                log_event(cfg.get("log_path"), {
                    "type": "close_old_yes",
                    "key": sess,
                    "fire_no": int(fire.get("fire_no") or 2),
                    "leg": old_yes.get("leg"),
                    "token_id": old_yes.get("token_id"),
                    "bucket_id": old_yes.get("bucket_id"),
                    "shares": close.get("shares"),
                    "bid_at_close": close.get("bid"),
                    "proceeds_usdc": close.get("proceeds_usdc"),
                    "loss_usdc": str(loss),
                    "closed_by": "refire_liquidation",
                    "book_unavailable": book_unavailable,
                    "ts_utc": now_utc.isoformat(),
                })
    if old is not None:
        # Merge the refire's filled legs into the session's single position.
        for lg in new_legs:
            old.setdefault("legs", []).append(lg)
        old["refire_at_utc"] = re_execution.iso_utc(now_utc)
        old["refire_jump"] = fire.get("jump")
        old["refire_ref_source"] = fire.get("ref_source")
        old["refire_running_extreme"] = fire.get("running_extreme")
        position = old
    else:
        # No first position (anomaly): fall back to a standalone record.
        positions[sess] = position
    state["entry_count"] = int(state.get("entry_count") or 0) + 1
    log_event(
        cfg.get("log_path"),
        {
            "type": "fire",
            "key": sess,
            "fire_no": int(fire.get("fire_no") or 2),
            "city_id": fire.get("city_id"),
            "icao": fire.get("icao"),
            "direction": fire.get("direction"),
            "jump": fire.get("jump"),
            "ref_source": fire.get("ref_source"),
            "refire": True,
            "close_old_yes": close,
            "fills": {
                str(lg.get("leg")): {"shares": lg.get("shares"), "cost": lg.get("cost_usdc"), "avg": lg.get("avg_price")}
                for lg in new_legs
            },
            "ladder": [
                {k: v for k, v in intent.items() if k != "fill"} | ({"fill": intent.get("fill")} if intent.get("fill") else {})
                for intent in ladlog
            ],
            "ts_utc": now_utc.isoformat(),
        },
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_cycle(
    cfg: dict[str, Any],
    state: dict[str, Any],
    now_utc: datetime | None = None,
    *,
    force_metar: bool = False,
    force_books: bool = False,
    force_rules: bool = False,
) -> bool:
    """Run one full poll cycle. Returns True while any session remains armed
    (caller keeps fast cadence; prune drops stale sessions).

    Caller (``runner_impl``) chooses sleep cadence from the return value:
    fast-poll (~10s) when armed, else scan interval (~20s)."""
    now = now_utc or datetime.now(timezone.utc)
    log_path = cfg.get("log_path", "data/yes2re_events.jsonl")
    cities = load_active_cities(cfg)
    if not cities:
        log_event(log_path, {"type": "empty_universe"})
        return False

    # Ensure strategy state sections present.
    ensure_re_state(state)

    # 1) Rule discovery (TTL-gated, caches failures)
    ttl_rules = float(cfg.get("rules_refresh_interval_seconds", DEFAULTS["rules_refresh_interval_seconds"]))
    rules_needed = force_rules or (time.time() - stamp("rules") >= ttl_rules)
    dates = target_dates_by_icao(cities, cfg, now)
    if rules_needed:
        refresh_rules(cfg, cities, dates, now)

    # Rebuild index from cache this cycle (even off-TTL we still know rules)
    rules_idx, rule_failures = _load_rule_cache()
    healthy_rules = {k: v for k, v in rule_failures.items() if k}
    log_event(log_path, {
        "type": "rules_refresh",
        "rules": len(rules_idx),
        "failures": rule_failures,
    })

    # Prune stale sessions before deriving armed_keys: the fast-poll / fast
    # METAR subset must only ever see the cleaned set — yesterday's keys or
    # low-direction zombies must not keep an ICAO hot-polled. prune never
    # raises (contract), so no guard is needed here.
    removed = prune_stale_sessions(state, cities, now)
    if removed:
        log_event(log_path, {"type": "prune_stale_sessions", "removed": removed})

    # Determine armed keys
    tree = ensure_re_state(state)
    armed_keys = set(tree.get("armed", {}).keys())
    # 2) Books for consensus/arm at rule scope. Feed ONLY today-dated rules
    # (2026-09-06 incident, second layer): between a city's local-midnight
    # rollover and the next rules-TTL refresh the cache can still list the old
    # date's rules, and prune has already cleared their sessions this cycle —
    # feeding them would re-open the stale-date arm/fire/sleeve path. Stale
    # rules are counted and logged once per cycle (a watcher-visible rollover
    # window), never fed to consensus, sleeve, or strategy.
    city_by_id = {c["city_id"]: c for c in cities}
    rules_now: list[dict[str, Any]] = []
    stale_rule_keys: list[str] = []
    for key, rule in rules_idx.items():
        if _rule_is_local_today(rule, city_by_id, now):
            rules_now.append(rule)
        else:
            stale_rule_keys.append(key)
    if stale_rule_keys:
        log_event(log_path, {"type": "stale_rules_ignored", "count": len(stale_rule_keys),
                             "keys": stale_rule_keys, "ts_utc": now.isoformat()})
    # token set from every *enabled active* rule bucket
    tokens = _all_tokens_for_rules(rules_idx)
    book_ttl = float(cfg.get("idle_book_interval_seconds", DEFAULTS["idle_book_interval_seconds"]))
    arm_book_ttl = float(cfg.get("arm_book_interval_seconds", DEFAULTS["arm_book_interval_seconds"]))
    do_book = force_books or (time.time() - stamp("book") >= book_ttl)
    if do_book and tokens:
        refresh_books(cfg, tokens, now)
        bump("book", time.time())
    # Armed fast-path: while any session is armed, keep the fire-critical
    # token subset (reference-bucket NO + breach-neighbour buckets from each
    # armed rule) hot on the armed cadence. The full-universe pull above is
    # ~1000 tokens and only runs on the idle TTL; without this fast path a
    # fire's legs frequently find no_book because their tokens were never
    # refreshed since the previous idle pull (or fell out of a chunk that
    # timed out). Warm subset is tens of tokens — cheap to refresh every few
    # seconds and exactly what plan_fire_cycle needs at fire time.
    warm_tokens = _warm_tokens_for_rules(rules_idx, state)
    if armed_keys and not do_book and warm_tokens and (time.time() - stamp("book_fast")) >= arm_book_ttl:
        refresh_books(cfg, warm_tokens, now)
        bump("book_fast", time.time())
    cache = book_cache()

    # ---- WebSocket bridge: live L2 overlay on the ladder cache ------------ #
    # WS feeds LocalOrderBook per token on a daemon thread (self-reconnecting);
    # every cycle we overlay any fresh (<5 s) local snapshot onto the ladder
    # cache, so paper FAK sees sub-second book updates when the feed is live.
    # REST /books above remains the correctness backbone and seeds on startup.
    wsb = ws_bridge()
    if cfg.get("market_ws_enabled", True) and tokens:
        if not wsb.running:
            try:
                wsb.start(tokens)
            except Exception as exc:  # noqa: BLE001
                log_event(log_path, {"type": "ws_start_failed", "error": f"{type(exc).__name__}: {exc}"})
        wsb.ensure_tokens(tokens)
        _ws_pump(wsb, cache)
    ws_tel = wsb.telemetry()

    # 2b) R1 — WS-triggered METAR early pull. If a fire-critical token's WS
    # book just repriced (fresh tick within ~4s) while its session is armed,
    # the market likely already read a new obs we have not pulled yet. Pull
    # that ICAO's METAR immediately (dual-source) instead of waiting for the
    # armed cadence, with a per-ICAO cooldown so a churning book cannot storm
    # the feed. The pulled obs is merged into metar_by_icao below so the
    # strategy sees it this very cycle.
    _ws_touched_icaos: set[str] = set()
    r1_enabled = bool(cfg.get("ws_triggered_metar_enabled", True))
    if r1_enabled and armed_keys and wsb.running:
        try:
            recent = _recent_ws_tokens(wsb, max_age_s=4.0)
            if recent:
                # Reverse-map token -> ICAO via the rules' bucket token ids.
                icao_by_tok = _warm_token_icao_map(rules_idx, cities, armed_keys)
                now_e2 = time.time()
                for tok in recent:
                    icao = icao_by_tok.get(str(tok))
                    if not icao:
                        continue
                    last = _WS_METAR_COOLDOWN.get(icao, 0.0)
                    if (now_e2 - last) < _WS_METAR_COOLDOWN_S:
                        continue
                    _WS_METAR_COOLDOWN[icao] = now_e2
                    _ws_touched_icaos.add(icao)
        except Exception as exc:  # noqa: BLE001
            log_event(log_path, {"type": "ws_trigger_failed", "error": f"{type(exc).__name__}: {exc}"})

    # 3) METAR — dual-rate pulls.
    # Full universe still runs on the idle cadence (stamp "metar", ~60s) and
    # feeds consensus + every rule. While any session is armed, a fast subset
    # pull of only the armed ICAOs additionally runs on the armed cadence
    # (stamp "metar_fast", ~10s) so a fresh obs reaches the strategy ~10s after
    # publication instead of ~60s. Both paths share common.dual_source_metar
    # (CheckWX primary, AWC backup) and both bump their stamp only on a
    # non-empty result: a failed pull leaves last-good in place and retries on
    # the next due cycle instead of hammering every tick.
    idle_metar = float(cfg.get("idle_metar_interval_seconds", DEFAULTS["idle_metar_interval_seconds"]))
    arm_metar = float(cfg.get("arm_metar_interval_seconds", DEFAULTS["arm_metar_interval_seconds"]))
    do_metar = force_metar or (time.time() - stamp("metar") >= idle_metar)
    metar_by_icao: dict[str, dict[str, Any]] = {}
    if do_metar:
        # Full-universe pull (every configured city ICAO).
        fresh = _fetch_metar(cfg, sorted({str(c.get("icao")).upper() for c in cities}), now)
        if fresh:
            _LAST_GOOD_METAR.clear()
            _LAST_GOOD_METAR.update(fresh)
            metar_by_icao = fresh
            bump("metar", time.time())
        else:
            # Pull failed: keep surfacing last-good obs (never wipe them).
            metar_by_icao = dict(_LAST_GOOD_METAR)
    else:
        # Between METAR fetches, still surface the last good obs so the
        # watcher can see obs-age growth and the consensus loop has data.
        metar_by_icao = dict(_LAST_GOOD_METAR)
    # Fast pull: armed-only ICAO subset, on the armed cadence, only when this
    # cycle did not already run the full pull (a successful full pull already
    # covers the armed subset). Empty fast result preserves last-good for those
    # ICAOs — feed jitter must not erase an observation.
    if armed_keys and not do_metar and (time.time() - stamp("metar_fast")) >= arm_metar:
        fast_icaos = sorted(_armed_icaos(armed_keys, cities))
        if fast_icaos:
            fast = _fetch_metar(cfg, fast_icaos, now)
            if fast:
                metar_by_icao.update(fast)
                _LAST_GOOD_METAR.update(fast)
                bump("metar_fast", time.time())
    # R1 early pull: WS-triggered ICAOs (market repriced on a fire-critical
    # token while armed). Pull them immediately regardless of cadence stamps —
    # this is the mirror-lag reduction: the book moved because someone read a
    # fresh obs; we want it in THIS cycle, not at the next 5s beat. A failed
    # pull keeps last-good and the cooldown handles retry pacing.
    if _ws_touched_icaos and not do_metar:
        _ws_touched_icaos = {i.upper() for i in _ws_touched_icaos}
        _ws_touched_icaos -= set(metar_by_icao)  # full pull already covered it
        if _ws_touched_icaos:
            trig = _fetch_metar(cfg, sorted(_ws_touched_icaos), now)
            if trig:
                metar_by_icao.update(trig)
                _LAST_GOOD_METAR.update(trig)
                log_event(
                    log_path,
                    {
                        "type": "ws_triggered_metar",
                        "icaos": sorted(_ws_touched_icaos),
                        "obs_ages": {i: _metar_age_s(trig[i]) for i in sorted(_ws_touched_icaos) if i in trig},
                        "ts_utc": now.isoformat(),
                    },
                )

    # 3b) TAF TX/TN — full-universe pull on a slow cadence (TAF updates every
    # 4-6h; no fast/armed variant needed). Parsed TX/TN per ICAO feed the
    # strategy's reference-extreme (ref_source="taf") so the reversal fires
    # relative to the forecast extreme, not the market rank-1 bucket — which
    # was the cause of the jump=6 misfires (running extreme vs favourite
    # bucket can be many buckets apart in extreme weather).
    taf_ttl = float(cfg.get("taf_refresh_interval_seconds", DEFAULTS.get("taf_refresh_interval_seconds", 1800)))
    do_taf = force_metar or (time.time() - stamp("taf")) >= taf_ttl
    taf_by_icao: dict[str, dict[str, Any]] = {}
    if do_taf:
        fresh_taf = _fetch_taf(cfg, sorted({str(c.get("icao")).upper() for c in cities}), now)
        if fresh_taf:
            _LAST_GOOD_TAF.clear()
            _LAST_GOOD_TAF.update(fresh_taf)
            bump("taf", time.time())
        taf_by_icao = dict(_LAST_GOOD_TAF)
    else:
        taf_by_icao = dict(_LAST_GOOD_TAF)

    # 4+5) Feed strategy per (city,direction) live contract for today
    armed_any = False
    for rule in rules_now:
        # Build per-rule book map for the YES tokens that have addresses
        rule_books: dict[str, dict[str, Any]] = {}
        for b in rule.get("buckets", []):
            for side in ("yes_token_id", "no_token_id"):
                tok = str(b.get(side) or "")
                if tok and tok in cache:
                    rule_books[tok] = cache[tok]
        # sample consensus even without new METAR (book cadence ~30s)
        t = tracker()
        t.record_books(
            rule.get("city_id"),
            rule.get("market_local_date"),
            rule.get("direction"),
            rule.get("buckets", []),
            rule_books,
            now,
        )

        # ---- B2 pre-breach sleeve (pure book structure, no METAR). ----
        # While enabled, keep the price ring fed and look for the rank-1 YES
        # weakening + neighbour YES strengthening that says the market is
        # pricing a breach before the obs reaches us. On signal we enter a
        # SMALL neighbour-YES sleeve (budget = fire_budget × sleeve_notional_pct,
        # ask cap = sleeve_max_ask). Sleeve positions carry key "<session>#sleeve"
        # so they never collide with the main reversal position of the same
        # session, and kind="sleeve" so the reports can separate the A/B arm.
        sleeve_cfg = cfg.get("strategy") or {}
        if sleeve_cfg.get("sleeve_enabled") and rule_books:
            srule = rule.get("buckets") or []
            # Cross-midnight guard (2026-09-06 incident, same as
            # reversal_strategy.maybe_arm_or_fire): never sleeve a rule whose
            # market_local_date is not the city's local today. _rule_is_local_today
            # fails closed (unknown city / bad or missing tz / missing date ->
            # False), so a dead-date session can never reserve sleeve cash.
            if srule and _rule_is_local_today(rule, city_by_id, now):
                _sleeve_tick(cfg, state, rule, rule_books, tree, now)
        icao = city_by_id.get(rule.get("city_id"), {}).get("icao", "").upper()
        obs = metar_by_icao.get(icao)
        if obs is None or obs.get("temp_c") is None:
            continue
        city = city_by_id.get(rule.get("city_id"))
        if city is None:
            continue
        market_unit = city.get("market_unit", "C")
        temp = common.c_to_market_unit(float(obs["temp_c"]), market_unit)
        rule_buckets = rule.get("buckets", [])
        # TAF reference extreme for this rule: TX for high-direction markets,
        # TN for low-direction markets. The extreme must fall inside the
        # market's local calendar date — a TAF spans ~30h and its TX/TN can
        # belong to the neighbouring local day; using a same-day mismatch
        # would recreate the jump-misfire bug against a stale reference. When
        # TAF is missing or off-date we pass None and the strategy falls back
        # to the market rank-1 consensus reference (allow_market_consensus_reference).
        taf_extreme_market: float | None = None
        taf_rec = taf_by_icao.get(icao)
        if taf_rec is not None:
            valid_iso = taf_rec.get("tx_valid_utc" if rule.get("direction") == "high" else "tn_valid_utc")
            taf_c = taf_rec.get("tx_c" if rule.get("direction") == "high" else "tn_c")
            if valid_iso and taf_c is not None:
                try:
                    from zoneinfo import ZoneInfo
                    tz_name = city.get("timezone") or "UTC"
                    valid_dt = datetime.fromisoformat(valid_iso.replace("Z", "+00:00"))
                    local_date = valid_dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
                    if local_date == rule.get("market_local_date"):
                        taf_extreme_market = common.c_to_market_unit(float(taf_c), market_unit)
                except Exception:  # noqa: BLE001 — bad TAF metadata → None → consensus fallback
                    taf_extreme_market = None
        # pass all known books for the whole rule (only YES needed for consensus)
        actions = maybe_arm_or_fire(
            state,
            city,
            rule.get("market_local_date"),
            rule.get("direction"),
            rule_buckets,
            taf_extreme_market,  # TAF TX/TN (market units) or None → consensus fallback
            temp,
            obs.get("obs_time"),
            now,
            rule_books,
            cfg.get("strategy") or {},
            consensus_tracker=t,
        )
        for action in actions:
            atype = action.get("action_type")
            if atype == "re_arm":
                armed_any = True
                log_event(log_path, {"type": "arm", "key": action.get("key"), **{k: action[k] for k in ("ref_source", "distance_c") if k in action}})
            elif atype in ("re_fire",):
                fire_no = int(action.get("fire_no") or 1)
                log_event(log_path, {"type": "fire_attempt", "key": action.get("key"),
                                     "fire_no": fire_no, "jump": action.get("jump"),
                                     "ref_source": action.get("ref_source")})
                position, ladlog = _paper_fire(cfg, state, action, now)
                if position is not None:
                    if fire_no >= 2:
                        # 追火: liquidate fire #1's old YES leg (on fill) and
                        # merge the new legs into the session position.
                        record_refire(cfg, state, action, position, ladlog, now)
                    else:
                        _record_fire_event(cfg, state, action, position, ladlog, now)
                else:
                    # insufficient capital / nothing fillable — mark fired anyway
                    # so we don't retry-fire the same session each tick. Update
                    # the strategy-written record IN PLACE so its fires counter
                    # (the fire-slot credential) survives the no-fill status.
                    rec = tree.setdefault("fired", {}).setdefault(action["key"], {})
                    rec["status"] = "fired_no_fill"
                    rec["at_utc"] = re_execution.iso_utc(now)
                    rec["jump"] = action.get("jump")
                # fire branch complete — never fall through to skip logging
                continue
            elif atype in ("re_skip", "re_skip_yes"):
                # Every skip is recorded (2026-09-03: silent skips hid the
                # stale_obs deadlock — 0 fires with zero audit trail).
                reason = action.get("reason")
                if reason == "duplicate_obs_time":
                    # Expected on every fast cycle (no new METAR for that session):
                    # keep the audit but bound JSONL growth to ~1/key/5 min.
                    key = action.get("key")
                    now_f = time.time()
                    if now_f - _DUP_LOG.get(key, 0.0) < _DUP_LOG_INTERVAL_S:
                        continue
                    _DUP_LOG[key] = now_f
                log_event(log_path, {"type": "skip", "key": action.get("key"),
                                     "reason": reason,
                                     "jump": action.get("jump"),
                                     "consensus": action.get("consensus")})
                continue
            elif atype in ("re_disarm",):
                # disarm is audit-only; log and move on
                log_event(log_path, {"type": "disarm", "key": action.get("key"),
                                     "reason": action.get("reason")})
                continue

    # 5b) B2 sleeve timeout: a pre-breach sleeve that has not been validated by
    # a real breach (i.e. its session has not fired) within sleeve_timeout_s is
    # sentiment churn, not information. Sell it at the current book (paper:
    # mark the leg cost as a sleeve_timeout loss) so a rotating book cannot
    # bleed the small sleeve allocation indefinitely. Real breaches settle via
    # the normal passive settlement below (win/lose at resolution).
    _expire_stale_sleeves(cfg, state, now)

    # 6) Passive settlement of resolved positions (best-effort, TTL-gated).
    # Gamma pulls only when open positions exist and settle cadence elapsed.
    open_pos = [p for p in state.get("positions", {}).values() if not p.get("settled")]
    settle_ttl = float(cfg.get("settle_poll_seconds", 3600))
    if open_pos and (force_books or time.time() - stamp("settle") >= settle_ttl):
        pos_meta = {}
        for p in open_pos:
            c = city_by_id.get(p.get("city_id"))
            if c:
                pos_meta[p.get("key")] = {"city": c}
        try:
            settled = settle_markets(cfg, state, position_meta=pos_meta)
            if settled:
                log_event(log_path, {"type": "settled", "count": len(settled)})
        except Exception as exc:  # noqa: BLE001
            log_event(log_path, {"type": "settle_failed", "error": f"{type(exc).__name__}: {exc}"})
        bump("settle", time.time())

    # ---- Publish feed telemetry for the watcher/hermes summary ---------- #
    now_epoch = time.time()
    metar_tel: dict[str, Any] = {}
    for icao, rec in metar_by_icao.items():
        age_s = None
        if rec.get("obs_time") is not None:
            age_s = max(0.0, (now - rec["obs_time"]).total_seconds())
        metar_tel[icao] = {
            "source": rec.get("source"),
            "obs_age_s": round(age_s, 1) if age_s is not None else None,
            "temp_c": rec.get("temp_c"),
            "fetched_this_cycle": True,
            "fields_ok": rec.get("temp_c") is not None and rec.get("obs_time") is not None,
        }
    book_age_s = {}
    _now_epoch = now_epoch
    for tid, bk in cache.items():
        f = bk.get("fetched_at_epoch")
        book_age_s[tid] = round(max(0.0, _now_epoch - float(f)), 1) if f else None
    tel: dict[str, Any] = {
        "ts_utc": re_execution.iso_utc(now),
        "cycle_armed": bool(armed_any),
        "armed_keys": list(tree.get("armed", {}).keys()),
        "fired_keys": list(tree.get("fired", {}).keys()),
        "open_positions": sum(1 for p in state.get("positions", {}).values() if not p.get("settled")),
        "rules": {
            "count": len(rules_idx),
            "failures": rule_failures,
            "age_s": round(max(0.0, now_epoch - stamp("rules")), 1) if stamp("rules") else None,
        },
        "metar": {
            "fetched": len(metar_by_icao),
            "cities_ok": sum(1 for r in metar_tel.values() if r["fields_ok"]),
            "max_obs_age_s": max((r["obs_age_s"] for r in metar_tel.values() if r["obs_age_s"] is not None), default=None),
            "per_icao": metar_tel,
        },
        "books": {
            "cached_tokens": len(cache),
            "oldest_age_s": max((a for a in book_age_s.values() if a is not None), default=None),
            "per_token_sample": {k: book_age_s[k] for k in list(book_age_s)[:6]},
        },
        # Polymarket market websocket: live when the bridge thread is up.
        "websocket_market": ws_tel,
        "clob": {"mode": "read_only", "submits_orders": False},
        "gamma": {"mode": "public_read_only", "events_discovered": len(rules_idx) // 2 if rules_idx else 0},
        "signal_latency_s": {"not_yet_fired": True},
    }
    set_health_extra(tel)
    # Post-processing truth: fires/disarms/prune this cycle already popped
    # their sessions, so only sessions still armed keep the caller fast-polling.
    return bool(tree.get("armed"))
