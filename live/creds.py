#!/usr/bin/env python3
"""Live credentials: read ``.env``, validate *format only*, never print values.

stdlib only. Secret values are never logged, echoed, serialized or embedded in
error messages — errors name the KEY, never the value. The only way a value ever
leaves this module is :func:`mask` (``first8…last4``) for human debug output.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

HEX_PRIVATE_KEY = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
SIGNATURE_TYPES = ("0", "1", "2")

REQUIRED_KEYS = ("POLY_PRIVATE_KEY", "POLY_FUNDER_ADDRESS", "POLY_SIGNATURE_TYPE")
API_TRIO = ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")

#: never allowed to appear in output/logs (values are stripped from messages)
SECRET_KEYS = (
    "POLY_PRIVATE_KEY",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "RELAYER_API_KEY",
    "RELAYER_API_KEY_ADDRESS",
    "CHECKWX_API_KEY",
)


class CredError(ValueError):
    """Missing/malformed credential. Message carries the key name only."""


def load_env_file(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` / ``export KEY=VALUE`` lines; ``os.environ`` wins."""
    env: dict[str, str] = dict(os.environ)
    p = Path(path) if path else ENV_PATH
    if not p.exists():
        return env
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export").strip()
        value = value.strip().strip('"').strip("'")
        if not env.get(key):
            env[key] = value
    return env


def mask(value: str | None) -> str:
    """``0xdeadbeef…c0ffee`` — safe for humans, useless for attackers."""
    if not value or len(value) < 12:
        return "***"
    return f"{value[:8]}…{value[-4:]}"


def sanitize(text: Any, env: dict[str, str] | None = None) -> str:
    """Strip any known secret value out of ``text`` (defence in depth)."""
    out = str(text)
    src = env if env is not None else load_env_file()
    for key in SECRET_KEYS:
        val = src.get(key)
        if val and len(val) >= 8:
            out = out.replace(val, f"<{key}:masked>")
    return out


def validate_creds(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate the live credential set. Raises :class:`CredError` on any problem.

    Formats: private key ``0x``+64 hex, funder ``0x``+40 hex,
    signature type one of ``0/1/2``. The L2 API trio is optional but all-or-nothing
    (when absent the client derives it from the private key).
    """
    env = env if env is not None else load_env_file()

    missing = [k for k in REQUIRED_KEYS if not (env.get(k) or "").strip()]
    if missing:
        raise CredError(f"missing required key(s): {', '.join(missing)}")

    if not HEX_PRIVATE_KEY.match(env["POLY_PRIVATE_KEY"].strip()):
        raise CredError("POLY_PRIVATE_KEY malformed (want 0x + 64 hex)")

    if not HEX_ADDRESS.match(env["POLY_FUNDER_ADDRESS"].strip()):
        raise CredError("POLY_FUNDER_ADDRESS malformed (want 0x + 40 hex)")

    sig = env["POLY_SIGNATURE_TYPE"].strip()
    if sig not in SIGNATURE_TYPES:
        raise CredError("POLY_SIGNATURE_TYPE malformed (want one of 0, 1, 2)")

    api_present = [k for k in API_TRIO if (env.get(k) or "").strip()]
    if api_present and len(api_present) != len(API_TRIO):
        absent = [k for k in API_TRIO if k not in api_present]
        raise CredError(f"incomplete L2 API creds, missing: {', '.join(absent)}")

    return {
        "private_key": env["POLY_PRIVATE_KEY"].strip(),
        "funder_address": env["POLY_FUNDER_ADDRESS"].strip(),
        "signature_type": int(sig),
        "api_key": (env.get("POLY_API_KEY") or "").strip() or None,
        "api_secret": (env.get("POLY_API_SECRET") or "").strip() or None,
        "api_passphrase": (env.get("POLY_API_PASSPHRASE") or "").strip() or None,
    }


def load_creds(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """``load_env_file`` + ``validate_creds`` in one call."""
    return validate_creds(load_env_file(path))
