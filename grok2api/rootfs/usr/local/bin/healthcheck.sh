#!/usr/bin/env sh
# Container health probe for the Grok2API Home Assistant add-on.
#
# Uses /healthz (liveness only). /readyz is deliberately NOT used here:
# it returns 503 until at least one usable upstream account exists, and a
# fresh install has zero accounts. Using /readyz would put the Supervisor
# watchdog / HEALTHCHECK into a permanent restart loop.
set -eu

PORT="${GROK2API_HEALTHCHECK_PORT:-8000}"

body="$(curl -fsS -m 4 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || true)"
case "${body}" in
    *'"ok":true'*) exit 0 ;;
    *) exit 1 ;;
esac
