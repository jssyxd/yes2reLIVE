"""CheckWX / AviationWeather helpers (stdlib only). No σ / fair-value math."""
from __future__ import annotations

import csv
import json
import os
import re
import urllib.error
import urllib.request
import calendar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import math

ROOT = Path(__file__).resolve().parent.parent

TX_RE = re.compile(r"TX(?P<sign>M)?(?P<temp>\d{2})/(?:(?P<day>\d{2}))?(?P<hour>\d{2})Z", re.I)
TN_RE = re.compile(r"TN(?P<sign>M)?(?P<temp>\d{2})/(?:(?P<day>\d{2}))?(?P<hour>\d{2})Z", re.I)

CHECKWX_BASE = "https://api.checkwx.com"
AWC_METAR = "https://aviationweather.gov/api/data/metar"


def load_env(path: str | os.PathLike | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    env.update(os.environ)
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() not in env or not env[key.strip()]:
            env[key.strip()] = value.strip()
    return env


def load_cities(path: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else ROOT / "config" / "contract_cities.json"
    if not p.is_absolute():
        p = ROOT / p
    return json.loads(p.read_text(encoding="utf-8"))


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "weatherbotyes2re/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error for {url}: {exc.reason}") from exc


def http_text(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "weatherbotyes2re/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


_TAF_PREFIX_MARKERS = frozenset({"AMD", "COR", "RTD", "CNL"})
_TAF_ICAO_TOKEN_RE = re.compile(r"^[A-Z]{3,4}$")


def _key_taf_entry(entry: str, wanted: set[str]) -> tuple[str, str] | None:
    """Map one raw CheckWX TAF string to ``(real ICAO, body-after-ICAO)``.

    Corrected/amended reports arrive as ``"TAF AMD EGLC 180600Z ..."`` (also
    COR/RTD): token[1] is the amendment marker, NOT the station id. Naively
    keying on ``parts[1]`` stored the EGLC TAF under "AMD", so EGLC was never
    found and its reference silently fell back to market consensus (the
    2026-09-08 London loss). The real ICAO is the first station-looking token
    after any prefix markers that is a requested station; the body is every
    token after it — a multi-line raw (embedded ``\\n``) is preserved because
    the remainder is sliced, never re-joined. Returns None when no requested
    station token precedes the issue time (entry is dropped, never mis-keyed).
    """
    tokens = entry.split()
    if not tokens:
        return None
    start = 1 if tokens[0].upper() == "TAF" else 0
    for i in range(start, len(tokens)):
        tok = tokens[i].upper()
        if tok[:1].isdigit():
            break  # issue time reached — the station token must precede it
        if tok in _TAF_PREFIX_MARKERS:
            continue
        if _TAF_ICAO_TOKEN_RE.match(tok) and tok in wanted:
            rest = entry.split(None, i + 1)
            return tok, rest[i + 1] if len(rest) > i + 1 else ""
    return None


def checkwx_taf(icaos: list[str], api_key: str, chunk: int = 15) -> dict[str, str]:
    """Fetch CheckWX TAFs keyed by the REAL station ICAO — never by an
    AMD/COR/RTD amendment marker (an amended report is ``"TAF AMD EGLC …"``
    whose token[1] is "AMD"; keying on token[1] made EGLC unfindable)."""
    out: dict[str, str] = {}
    wanted = {str(i).strip().upper() for i in icaos if str(i).strip()}
    for i in range(0, len(icaos), chunk):
        batch = icaos[i : i + chunk]
        url = f"{CHECKWX_BASE}/taf/{','.join(batch)}"
        data = http_json(url, headers={"X-API-Key": api_key})
        for entry in data.get("data", []):
            if not isinstance(entry, str) or not entry.strip():
                continue
            keyed = _key_taf_entry(entry, wanted)
            if keyed is not None:
                out[keyed[0]] = keyed[1]
    return out


def parse_tx_tn(raw_taf: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    m = TX_RE.search(raw_taf)
    if m:
        temp = int(m.group("temp"))
        if m.group("sign"):
            temp = -temp
        result.update(tx_c=temp, tx_hour=int(m.group("hour")), tx_day=m.group("day"))
    m = TN_RE.search(raw_taf)
    if m:
        temp = int(m.group("temp"))
        if m.group("sign"):
            temp = -temp
        result.update(tn_c=temp, tn_hour=int(m.group("hour")), tn_day=m.group("day"))
    return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_csv(path: str | os.PathLike, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


ISSUE_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})Z")


def parse_taf_issue_time(raw_taf: str, ref_utc: datetime | None = None) -> datetime | None:
    ref = ref_utc or utc_now()
    m = ISSUE_RE.match(raw_taf.strip())
    if not m:
        return None
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = datetime(ref.year, ref.month, day, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None
    if (dt - ref) > timedelta(days=15):
        prev = ref.month - 1 or 12
        year = ref.year - 1 if prev == 12 else ref.year
        try:
            dt = datetime(year, prev, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            return None
    return dt


def resolve_tx_valid_utc(issue_dt: datetime, tx_day: str | None, tx_hour: int) -> datetime | None:
    year, month = issue_dt.year, issue_dt.month
    if tx_day is not None:
        day = int(tx_day)
        if day < issue_dt.day:
            month += 1
            if month > 12:
                month, year = 1, year + 1
    else:
        day = issue_dt.day
    _, last = calendar.monthrange(year, month)
    day = min(day, last)
    try:
        return datetime(year, month, day, tx_hour, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


def local_date_for(city: dict[str, Any], dt_utc: datetime) -> str:
    from zoneinfo import ZoneInfo
    return dt_utc.astimezone(ZoneInfo(city["timezone"])).date().isoformat()


METAR_T_RE = re.compile(r"T([01])(\d{3})([01])(\d{3})")
METAR_TT_RE = re.compile(r"(?<![A-Za-z])(M?\d{2})/(M?\d{2})(?![A-Za-z])")
OBS_Z_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")


def parse_metar_temp_c(raw_metar: str) -> float | None:
    if not raw_metar:
        return None
    m = METAR_T_RE.search(raw_metar)
    if m:
        sign = -1.0 if m.group(1) == "1" else 1.0
        return sign * int(m.group(2)) / 10.0
    m = METAR_TT_RE.search(raw_metar)
    if m:
        temp = m.group(1)
        sign = -1.0 if temp.startswith("M") else 1.0
        return sign * float(temp.lstrip("M"))
    return None


def parse_obs_time_utc(raw_metar: str, now: datetime | None = None) -> datetime | None:
    if not raw_metar:
        return None
    now = now or utc_now()
    m = OBS_Z_RE.search(raw_metar)
    if not m:
        return None
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        if day > now.day:
            # METAR dd day rolled past the end of the current month: the obs is
            # from the previous month (last METAR before month boundary).
            prev_end = now.replace(day=1) - timedelta(days=1)
            dt = prev_end.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
        else:
            dt = now.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
    if (now - dt).total_seconds() < -6 * 3600:
        return None
    if (now - dt).total_seconds() > 18 * 3600:
        return None
    return dt


def c_to_market_unit(temp_c: float, market_unit: str) -> float:
    """ICAO METAR/TAF temperatures are Celsius; US Polymarket buckets are often °F.

    For °F markets we replicate the official/NWS convention: METAR °C is
    converted and rounded to the nearest whole degree (22 °C -> 71.6 -> 72).
    Raw float 71.6 falls below a 72-73°F bucket boundary and misclassifies a
    boundary reading as a broken bucket (Chicago NO misfire, 2026-09-05).

    Polymarket unit rules (audited 2026-09-07):
    - Buckets: US cities 1-2°F integer buckets; EU/Asia cities 1°C buckets.
    - Resolution: Wunderground station "Daily Observations" finalized daily
      extreme, whole degrees, post-QC (not intraday METAR, not NWS CLI
      summary). Truncation (23.9°C -> 23) is the stated °C rule.
    - METAR has NO native °F anywhere (global °C). US ASOS displays whole °F
      via rounding, which is why we round here rather than truncate — the
      truncation rule applies to the °C-bucket side, where whole-degree METAR
      (a truncated reading) aligns naturally.
    - Residual risk: METAR whole-°C granularity spans ±0.9°F after conversion,
      and the finalized Wunderground extreme can differ ~1°F from the METAR
      extreme (SF 9/4 low misfire). The F-market break-confirmation margin
      (break_confirm_margin_f) in reversal_strategy.py absorbs this.
    """
    unit = (market_unit or "C").upper()
    if unit == "F":
        return float(math.floor(temp_c * 9.0 / 5.0 + 32.0 + 0.5))
    return float(temp_c)


def checkwx_metar(icaos: list[str], api_key: str, chunk: int = 20) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(icaos), chunk):
        batch = icaos[i : i + chunk]
        url = f"{CHECKWX_BASE}/metar/{','.join(batch)}"
        data = http_json(url, headers={"X-API-Key": api_key})
        for entry in data.get("data", []):
            parts = entry.split(None, 2)
            if len(parts) >= 3 and parts[0].upper() == "METAR":
                out[parts[1]] = parts[2]
            elif len(parts) >= 2:
                # sometimes raw without leading METAR keyword
                out[parts[0]] = entry
    return out


def aviationweather_metar(icaos: list[str], chunk: int = 20) -> dict[str, str]:
    """Public AWC Data API — no API key. Returns {icao: raw metar text}."""
    out: dict[str, str] = {}
    for i in range(0, len(icaos), chunk):
        batch = icaos[i : i + chunk]
        ids = ",".join(batch)
        url = f"{AWC_METAR}?ids={ids}&format=json"
        try:
            data = http_json(url, timeout=20)
        except RuntimeError:
            continue
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            icao = str(row.get("icaoId") or row.get("stationId") or "").upper()
            raw = str(row.get("rawOb") or row.get("raw") or "")
            if icao and raw:
                out[icao] = raw
    return out


def dual_source_metar(
    icaos: list[str],
    api_key: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Dual-source METAR: CheckWX primary, AviationWeather (AWC) only backup.

    When ``api_key`` is set, CheckWX is authoritative: every ICAO CheckWX
    returns is adopted as-is with source ``"checkwx"`` — no freshness
    comparison against AWC (CheckWX wins even when AWC would be newer).
    AWC is queried only for the ICAOs CheckWX did not cover (source
    ``"awc"``), so a CheckWX gap still yields an observation. When
    ``api_key`` is empty/None the keyless mode serves the whole universe
    from AWC (source ``"awc"``). A CheckWX outage degrades to pure AWC;
    an AWC error is swallowed (per-batch, see ``aviationweather_metar``).

    Returns {icao: {"raw": str, "temp_c": float|None, "obs_time": datetime|None, "source": str}}
    """
    now = now or utc_now()
    check: dict[str, str] = {}
    if api_key:
        try:
            check = checkwx_metar(icaos, api_key)
        except RuntimeError:
            check = {}
    # AWC only fills CheckWX gaps; with no key it serves everything itself.
    awc_icaos = [i for i in icaos if i not in check] if api_key else list(icaos)
    awc: dict[str, str] = {}
    if awc_icaos:
        try:
            awc = aviationweather_metar(awc_icaos)
        except Exception:
            awc = {}

    out: dict[str, dict[str, Any]] = {}
    for icao in icaos:
        if api_key and icao in check:
            raw, src = check[icao], "checkwx"
        elif icao in awc:
            raw, src = awc[icao], "awc"
        else:
            continue
        out[icao] = {
            "raw": raw,
            "temp_c": parse_metar_temp_c(raw),
            "obs_time": parse_obs_time_utc(raw, now),
            "source": src,
        }
    return out


if __name__ == "__main__":  # offline self-check of the TAF keying logic
    def _k(e: str, want: list[str]) -> tuple[str, str] | None:
        return _key_taf_entry(e, {w.upper() for w in want})

    # FIX-1 regression: "TAF AMD <ICAO> ..." must key under the real ICAO.
    got = _k("TAF AMD EGLC 171800Z 1718/1818 23010KT TX24/1815Z", ["EGLC"])
    assert got == ("EGLC", "171800Z 1718/1818 23010KT TX24/1815Z"), got
    assert _k("TAF EGLC 180600Z 1806/1812 22010KT", ["EGLC"])[0] == "EGLC"
    assert _k("TAF COR KORD 181200Z 1812/1824 24012KT", ["KORD"])[0] == "KORD"
    assert _k("TAF RTD EDDM 180500Z 1806/1812 12008KT", ["EDDM"])[0] == "EDDM"
    # multi-line raw survives verbatim (newlines intact, TX/TN still parseable)
    ml = "TAF AMD EGLC 180600Z 1806/1812\n    1806/1812 22010KT 9999 SCT025\n    TX24/1812Z TN13/1806Z"
    icao, body = _k(ml, ["EGLC"])
    assert icao == "EGLC" and "\n" in body and parse_tx_tn(body)["tx_c"] == 24
    # never mis-key an unknown/extra station; raw without the TAF prefix ok
    assert _k("TAF AMD XXYY 180600Z 1806/1812 TX24/1812Z", ["EGLC"]) is None
    assert _k("EGLC 180600Z 1806/1812 22010KT TX24/1812Z", ["EGLC"])[0] == "EGLC"
    print("common.checkwx_taf keying self-check: OK")
