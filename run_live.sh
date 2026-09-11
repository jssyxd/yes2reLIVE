#!/bin/bash
set -e
cd /root/weatherbotyes2re
set -a
. ./.env
export YES2RE_LIVE_ENABLE_SUBMIT=1
export LIVE_SUBMIT_ENABLED=1
export YES2RE_LIVE_CONFIRM="SMOKE-$(date -u +%Y-%m-%d)"
set +a
exec /root/yes2re-live/.venv/bin/python reversal_runner.py run --config config/yes2re_reversal.json
