ARG BUILD_FROM=ghcr.io/chenyme/grok2api:v3.1.8-amd64
FROM ${BUILD_FROM}

# The upstream image is Alpine based and already provides:
#   /app/grok2api                 static Go binary (no CGO)
#   /app/frontend/dist            built React SPA
#   user grok2api (uid/gid 10001)
#   /sbin/su-exec
# The Home Assistant Supervisor mounts /data as persistent storage and passes
# the add-on options as /data/options.json -- no `map` entry is required.

USER root

RUN apk add --no-cache bash curl jq openssl \
 && mkdir -p /data/app /data/config /data/backup /run/grok2api \
 && chown -R grok2api:grok2api /data /run/grok2api

COPY rootfs /

RUN chmod 0755 /usr/local/bin/haos-entrypoint.sh \
               /usr/local/bin/haos-bootstrap.sh \
               /usr/local/bin/healthcheck.sh \
 && chown root:root /usr/local/bin/haos-entrypoint.sh \
                    /usr/local/bin/haos-bootstrap.sh \
                    /usr/local/bin/healthcheck.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/local/bin/haos-entrypoint.sh"]
CMD ["/app/grok2api", "--config", "/data/config/config.yaml", "--listen", "0.0.0.0:8000"]
