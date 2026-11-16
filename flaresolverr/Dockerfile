ARG BUILD_FROM=ghcr.io/flaresolverr/flaresolverr:v3.3.25
FROM ${BUILD_FROM}

# FlareSolverr is the Cloudflare challenge proxy used by Grok2API to obtain
# browser clearance (cf_clearance) for Grok Web sessions.
#
#   - LOG_LEVEL=info / CAPTCHA_SOLVER=none keep the proxy in default mode.
#   - HEADLESS=true runs the bundled Chromium without a display.
#
# The add-on only exposes the internal HTTP API on container port 8191, which
# the Home Assistant Supervisor publishes as host port 8191.

ENV LOG_LEVEL="info" \
    CAPTCHA_SOLVER="none" \
    HEADLESS="true"

EXPOSE 8191

# Liveness probe: TCP connect to the local proxy port (FlareSolverr does not
# expose a dedicated /health endpoint; the proxy itself is the service).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8191"]

# The base image already ends with `USER flaresolverr`.
