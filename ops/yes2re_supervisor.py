#!/usr/bin/env python3
"""Supervisor: keeps the weatherbotyes2re paper runner alive.

Restarts `reversal_runner.py run` on unexpected exit and logs lifecycle to
data/supervisor.log. The 15-minute CSV/log recorder runs via cron
(ops/yes2re_recorder.py) so it stays independent of this process.

Stdlib only. Detached usage:
    setsid nohup python3 ops/yes2re_supervisor.py >> data/supervisor_nohup.log 2>&1 < /dev/null &
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
CONFIG = "config/yes2re_reversal.json"
CHECK_INTERVAL_S = 15.0
# Project requires Python 3.13 (stdlib-only, cpython-313 bytecode). Pin the
# interpreter explicitly — an ambient PATH (e.g. a Hermes venv on 3.11) would
# otherwise break the runner silently.
PYTHON_BIN = os.environ.get("YES2RE_PYTHON", "/usr/bin/python3")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(msg: str) -> None:
    line = f"{now_utc()}  {msg}"
    try:
        with (ROOT / "data" / "supervisor.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def start_runner() -> subprocess.Popen:
    env = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    out = (ROOT / "data" / "runner_stdout.log").open("ab")
    proc = subprocess.Popen(
        [PYTHON_BIN, "reversal_runner.py", "run", "--config", CONFIG],
        cwd=str(ROOT),
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"runner started pid={proc.pid}")
    return proc


def main() -> int:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    log("supervisor starting")
    proc = start_runner()
    while True:
        time.sleep(CHECK_INTERVAL_S)
        if proc.poll() is not None:
            log(f"runner exited rc={proc.returncode}; restarting")
            proc = start_runner()
    return 0


if __name__ == "__main__":
    sys.exit(main())
