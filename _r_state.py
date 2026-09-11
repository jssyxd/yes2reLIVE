"""State, config, log, and health I/O for the weatherbotyes2re paper runner.

Faithful reconstruction of the intended ``_r_state`` module (see CHANGELOG +
``runner_impl.py`` import contract). Stdlib only. All money values travel as
``Decimal`` across the live session and are only widened to float/JSON strings
at the persistence boundary (``paper_capital``/``save_state``).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Runtime schema version. Bump and add a migration in ``load_state`` when the
# on-disk shape of ``data/yes2re_state.json`` changes incompatibly.
STATE_VERSION = 2

# State tree sections we create on first load.
_SECTIONS = ("armed", "fired", "running_extremes", "taf_forecasts", "last_obs", "last_obs_time", "ever_armed", "last_fire_obs")

# Sensible defaults when a key is absent from a (hand-written) config JSON.
DEFAULTS: dict[str, Any] = {
    "mode": "paper",
    "scan_interval_seconds": 20,
    "fast_poll_interval_seconds": 8,
    "idle_metar_interval_seconds": 45,
    "idle_book_interval_seconds": 30,
    "arm_metar_interval_seconds": 10,
    "arm_book_interval_seconds": 8,
    "rules_refresh_interval_seconds": 1200,
    "taf_refresh_interval_seconds": 1800,
    "tail_hours": 144,
    "checkwx_api_key_env": "CHECKWX_API_KEY",
    "base_fee_rate": "0.02",
    "paper_initial_capital_usdc": 1000.0,
    "fire_budget_usdc": 20.0,
    "max_open_positions": 12,
    "settle_grace_hours": 6,
    "settle_max_hours": 72,
    "contract_cities_path": "config/contract_cities.json",
    # Optional: restrict the discovery/trading universe. Absent -> whole registry.
    "active_icaos": None,
    "state_path": "data/yes2re_state.json",
    "log_path": "data/yes2re_events.jsonl",
    "health_path": "data/yes2re_health.json",
    "strategy": {},
}

_LOG_FIELDS_STR = None

# JSONL rotation (2026-09-06 review): the events log grows without bound
# (~0.6 MB/h at arm cadence; ~15 MB/day idle — instance A had already
# reached 13.5 MB / 90k lines in 22 h). Rotate size-based, keep
# LOG_KEEP rotated generations plus the live file. log_event has no cfg
# handle, so these are module constants (tune via environment if ever
# needed). Rotation is best-effort and never raises.
LOG_MAX_BYTES = 64 * 1024 * 1024
LOG_KEEP = 3


def _rotate_events_log(path: Path) -> None:
    """Shift ``path`` -> ``path.1`` -> ``path.2`` ... when the live log exceeds
    :data:`LOG_MAX_BYTES`. Best-effort: any OSError silently leaves the log
    unrotated (the observer path must stay up)."""
    try:
        if not path.exists():
            return
        if path.stat().st_size < LOG_MAX_BYTES:
            return
        # Drop the oldest generation, then shift .1 -> .2 ... (K-1) -> K, and
        # finally rotate the live file -> .1. At most LOG_KEEP backups exist.
        oldest = path.with_name(f"{path.name}.{LOG_KEEP}")
        if oldest.exists():
            oldest.unlink()
        for i in range(LOG_KEEP - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def _decode(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


#: deployment overrides (only these three; see ``_env_overrides``)
ENV_MODE = "YES2RE_MODE"
ENV_FIRE_BUDGET = "YES2RE_FIRE_BUDGET_USDC"
ENV_MAX_OPEN = "YES2RE_MAX_OPEN_POSITIONS"
#: live instances seed the ledger with the *real* account balance so "current equity" starts
#: from real money (the paper instance keeps the config value)
ENV_INITIAL_CAPITAL = "YES2RE_INITIAL_CAPITAL_USDC"
ENV_OVERRIDE_KEYS = (ENV_MODE, ENV_FIRE_BUDGET, ENV_MAX_OPEN, ENV_INITIAL_CAPITAL)
_VALID_MODES = ("paper", "live")


def _env_overrides(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Read the three deployment env overrides.

    Rationale: the live and paper instances share **one** config file (so strategy
    parameters can never drift apart); only the mode and the two hard caps may be
    selected per instance, and only through the environment.

    Absent/empty ⇒ not applied (behaviour identical to no override at all).
    Illegal ⇒ ``SystemExit`` naming the variable (fail closed, never silently ignored).
    """
    src = os.environ if env is None else env
    applied: dict[str, Any] = {}
    raw_mode = src.get(ENV_MODE)
    if raw_mode is not None and str(raw_mode).strip() != "":
        mode = str(raw_mode).strip().lower()
        if mode not in _VALID_MODES:
            raise SystemExit(f"env {ENV_MODE}={raw_mode!r}: bad mode (want {'|'.join(_VALID_MODES)})")
        applied["mode"] = mode
    raw_budget = src.get(ENV_FIRE_BUDGET)
    if raw_budget is not None and str(raw_budget).strip() != "":
        try:
            budget = float(str(raw_budget).strip())
        except (TypeError, ValueError):
            raise SystemExit(f"env {ENV_FIRE_BUDGET}={raw_budget!r}: not a number") from None
        if not math.isfinite(budget) or budget <= 0:
            raise SystemExit(f"env {ENV_FIRE_BUDGET}={raw_budget!r}: must be a finite positive number")
        applied["fire_budget_usdc"] = budget
    raw_max = src.get(ENV_MAX_OPEN)
    if raw_max is not None and str(raw_max).strip() != "":
        text = str(raw_max).strip()
        if not text.lstrip("+").isdigit() or int(text) <= 0:
            raise SystemExit(f"env {ENV_MAX_OPEN}={raw_max!r}: must be a positive integer")
        applied["max_open_positions"] = int(text)
    raw_capital = src.get(ENV_INITIAL_CAPITAL)
    if raw_capital is not None and str(raw_capital).strip() != "":
        try:
            capital = float(str(raw_capital).strip())
        except (TypeError, ValueError):
            raise SystemExit(f"env {ENV_INITIAL_CAPITAL}={raw_capital!r}: not a number") from None
        if not math.isfinite(capital) or capital <= 0:
            raise SystemExit(f"env {ENV_INITIAL_CAPITAL}={raw_capital!r}: must be a finite positive number")
        applied["paper_initial_capital_usdc"] = capital
    return {"applied": applied, "mode_explicit": "mode" in applied,
            "keys": [k for k in ENV_OVERRIDE_KEYS if k in src and str(src.get(k) or "").strip() != ""]}


def load_config(path: str | os.PathLike, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Read + validate a single run-config JSON, merging missing keys with
    :data:`DEFAULTS`. The ``strategy`` sub-dict is merged shallowly with the
    strategy module defaults at call time (see ``reversal_strategy``).

    Deployment overrides come from the environment (``env=None`` ⇒ ``os.environ``): only
    ``mode`` / ``fire_budget_usdc`` / ``max_open_positions`` / ``paper_initial_capital_usdc``
    can differ between the paper and live instances — every strategy parameter stays exactly as
    the shared config file says. The live instance sets ``YES2RE_INITIAL_CAPITAL_USDC`` to the
    real account balance so the ledger (and therefore the reported current equity) starts from
    real money. Without those variables the returned dict is bit-for-bit what it always was.
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            user = json.load(fh)
        cfg.update({k: v for k, v in user.items() if v is not None})
        strat = dict(cfg.get("strategy") or {})
        if isinstance(user.get("strategy"), dict):
            strat.update(_decode(user["strategy"]))
        cfg["strategy"] = strat
    overrides = _env_overrides(env)
    cfg.update(overrides["applied"])
    _validate_config(cfg, p, mode_opt_in=overrides["mode_explicit"])
    if overrides["applied"].get("mode") not in (None, "paper"):
        print(f"WARNING: {ENV_MODE}={overrides['applied']['mode']} selected by the environment "
              f"(overrides the shared config); live writes still require the execution-port gates",
              file=sys.stderr)
    # Default active universe: whole registry unless restricted.
    cfg.setdefault("active_icaos", None)
    return cfg


def _validate_config(cfg: dict[str, Any], source: Path, *, mode_opt_in: bool = False) -> None:
    """Validate the *effective* config (after env overrides).

    ``mode_opt_in`` is True only when the mode came from ``YES2RE_MODE``: a config **file**
    can still never select a non-paper mode (that was and remains the safety lock), while
    the operator may opt in explicitly per process.
    """
    mode = str(cfg.get("mode", "paper")).lower()
    if mode not in _VALID_MODES:
        raise SystemExit(f"config {source}: bad mode {mode!r} (want {'|'.join(_VALID_MODES)})")
    if mode != "paper" and not mode_opt_in:
        raise SystemExit(f"refusing non-paper mode {mode!r}: safety lock (paper only); "
                         f"set {ENV_MODE}={mode} to opt in explicitly")
    intervs = [
        "scan_interval_seconds",
        "fast_poll_interval_seconds",
        "idle_metar_interval_seconds",
        "arm_metar_interval_seconds",
        "idle_book_interval_seconds",
    ]
    for k in intervs:
        try:
            v = float(cfg.get(k, DEFAULTS.get(k, 0)))
        except (TypeError, ValueError):
            v = -1
        if cfg.get(k) is None or v < 0:
            raise SystemExit(f"config {source}: bad required interval {k}={cfg.get(k)!r}")
    for money in ("paper_initial_capital_usdc", "fire_budget_usdc"):
        if cfg.get(money) is None or float(cfg[money]) < 0:
            raise SystemExit(f"config {source}: bad money field {money}={cfg.get(money)!r}")


def _blank_state(cfg: dict[str, Any]) -> dict[str, Any]:
    capital = float(cfg.get("paper_initial_capital_usdc", DEFAULTS["paper_initial_capital_usdc"]))
    return {
        "positions": {},
        "entry_count": 0,
        "weatherbotyes2re": {s: {} for s in _SECTIONS},
        "version": STATE_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_saved_at_utc": None,
        "paper_initial_capital_usdc": capital,
        "paper_total_debit_usdc": 0.0,
    }


def load_state(path: str | os.PathLike) -> dict[str, Any]:
    """Load the state blob from ``path``, migrating/blanking if stale/missing.
    Returned dict is fully mutable and safe for the strategy to read/write."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return _blank_state({})  # capital patched by caller via setdefault
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _blank_state({})
    if not isinstance(raw, dict):
        return _blank_state({})
    if raw.get("version") != STATE_VERSION:
        # Preserve capital across schema bumps, then re-blank.
        capital = raw.get("paper_initial_capital_usdc", _blank_state({})["paper_initial_capital_usdc"])
        st = _blank_state({})
        st["paper_initial_capital_usdc"] = float(capital)
        st.pop("removed_capital_note", None)
        return st
    tree = raw.setdefault("weatherbotyes2re", {})
    for s in _SECTIONS:
        tree.setdefault(s, {})
    raw.setdefault("positions", {})
    raw.setdefault("entry_count", 0)
    raw.setdefault("paper_total_debit_usdc", 0.0)
    raw.setdefault("paper_initial_capital_usdc", DEFAULTS["paper_initial_capital_usdc"])
    return raw


def save_state(path: str | os.PathLike, state: dict[str, Any]) -> None:
    """Serialize the state blob to disk (float-widened for JSON)."""
    d = Path(path)
    if d.parent and not d.parent.exists():
        d.parent.mkdir(parents=True, exist_ok=True)
    state["last_saved_at_utc"] = datetime.now(timezone.utc).isoformat()
    tmp = d.with_suffix(d.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(d)


def log_event(path: str | os.PathLike, event: dict[str, Any]) -> None:
    """Append one JSON line to the JSONL events log. ``event`` gains an
    ISO-``ts_utc`` if absent. Never raises on write failure: the observer path
    must stay up even if disk hiccups."""
    p = Path(path)
    try:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        if "ts_utc" not in event:
            event["ts_utc"] = datetime.now(timezone.utc).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        _rotate_events_log(p)  # size-check BEFORE append: live file always exists after a write
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
    except OSError:
        pass


def monitor_epoch() -> float:
    return time.time()


def _iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
