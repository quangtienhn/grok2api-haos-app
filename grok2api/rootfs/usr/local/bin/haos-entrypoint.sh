#!/usr/bin/env bash
# Home Assistant OS bootstrap for the Grok2API add-on.
#
# Responsibilities (in order):
#   1. Read /data/options.json written by the Supervisor.
#   2. Validate the mandatory admin password (no insecure generated default).
#   3. Create/load the persistent JWT secret and credential encryption key.
#   4. Render /data/config/config.yaml (0600) for the upstream Go binary.
#   5. Start the upstream binary as the unprivileged grok2api user and forward
#      termination signals to it.
#   6. Run the post-start bootstrap (admin password sync + client API key)
#      once the HTTP server answers on localhost.
#
# This script never prints options, passwords, cookies, keys or tokens.
set -Eeuo pipefail
umask 077

readonly OPTIONS_FILE="/data/options.json"
readonly DATA_DIR="/data"
readonly CONFIG_DIR="/data/config"
readonly APP_DATA_DIR="/data/app"
readonly BACKUP_DIR="/data/backup"
readonly CONFIG_FILE="${CONFIG_DIR}/config.yaml"
readonly SECRETS_FILE="${CONFIG_DIR}/secrets.env"
readonly RUN_DIR="/run/grok2api"

log() {
    local level="$1"; shift
    printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${level}" "$*"
}
log_info() { log INFO "$*"; }
log_warn() { log WARNING "$*"; }
log_error() { log ERROR "$*"; }
die() { log_error "$*"; exit 1; }

mkdir -p "${CONFIG_DIR}" "${APP_DATA_DIR}" "${APP_DATA_DIR}/media" "${BACKUP_DIR}" "${RUN_DIR}" /var/lib/grok2api-quality-guard
chmod 0700 "${CONFIG_DIR}" "${APP_DATA_DIR}" "${APP_DATA_DIR}/media" "${BACKUP_DIR}" /var/lib/grok2api-quality-guard

# ---------------------------------------------------------------------------
# 1. Options
# ---------------------------------------------------------------------------
ADMIN_USER="admin"
ADMIN_PASS=""
BOOTSTRAP_CLIENT_KEY="true"
CLIENT_KEY_NAME="Home Assistant"
REQUEST_TIMEOUT="600"
PUBLIC_BASE_URL=""

if [ -f "${OPTIONS_FILE}" ]; then
    opt() {
        jq -r --arg key "$1" '
            if has($key) and (.[$key] != null) then (.[$key] | tostring) else "" end
        ' "${OPTIONS_FILE}" 2>/dev/null || true
    }
    value="$(opt admin_username)";            [ -n "${value}" ] && ADMIN_USER="${value}"
    value="$(opt admin_password)";            ADMIN_PASS="${value}"
    value="$(opt bootstrap_client_key)";      [ -n "${value}" ] && BOOTSTRAP_CLIENT_KEY="${value}"
    value="$(opt client_key_name)";           [ -n "${value}" ] && CLIENT_KEY_NAME="${value}"
    value="$(opt request_timeout)";           [[ "${value}" =~ ^[0-9]+$ ]] && REQUEST_TIMEOUT="${value}"
    value="$(opt public_base_url)";           PUBLIC_BASE_URL="${value}"
else
    log_warn "No ${OPTIONS_FILE} found (not running under the Supervisor?). Using defaults."
fi

if [ "${#ADMIN_PASS}" -lt 8 ]; then
    log_error "admin_password is missing or shorter than 8 characters."
    log_error "Open the add-on 'Configuration' tab, set a strong admin_password (8+ characters) and start the add-on again."
    log_error "No password was generated on purpose: generated passwords would have to be written to the log."
    exit 1
fi

log_info "Starting Grok2API Home Assistant add-on (upstream 3.1.8)."
log_info "Admin username: ${ADMIN_USER}"
log_info "Persistent data directory: ${DATA_DIR}"

# ---------------------------------------------------------------------------
# 2. Persistent secrets
# ---------------------------------------------------------------------------
if [ ! -f "${SECRETS_FILE}" ]; then
    log_info "First start: generating persistent JWT secret and credential encryption key."
    JWT_SECRET="$(openssl rand -hex 32)"
    CREDENTIAL_ENCRYPTION_KEY="$(openssl rand -base64 32)"
    ( umask 077; printf 'JWT_SECRET=%s\nCREDENTIAL_ENCRYPTION_KEY=%s\n' \
        "${JWT_SECRET}" "${CREDENTIAL_ENCRYPTION_KEY}" > "${SECRETS_FILE}" )
    log_info "Secrets stored in ${SECRETS_FILE} (mode 0600). Back this file up together with the database."
else
    log_info "Loading existing secrets from ${SECRETS_FILE}."
fi

set +u
# shellcheck disable=SC1090
. "${SECRETS_FILE}"
set -u

[ -n "${JWT_SECRET:-}" ] || die "JWT_SECRET missing from ${SECRETS_FILE}"
[ -n "${CREDENTIAL_ENCRYPTION_KEY:-}" ] || die "CREDENTIAL_ENCRYPTION_KEY missing from ${SECRETS_FILE}"
chmod 0600 "${SECRETS_FILE}" || true

# ---------------------------------------------------------------------------
# 3. Runtime configuration
# ---------------------------------------------------------------------------
log_info "Rendering ${CONFIG_FILE}."
cat > "${CONFIG_FILE}" <<EOF
server:
  listen: "0.0.0.0:8000"
  maxBodyBytes: 33554432
  trustedProxies: []
  readTimeout: 15m
  requestTimeout: ${REQUEST_TIMEOUT}s
  swaggerEnabled: false

auth:
  accessTokenTTL: 60m
  refreshTokenTTL: 720h
  secureCookies: false

secrets:
  jwtSecret: "${JWT_SECRET}"
  credentialEncryptionKey: "${CREDENTIAL_ENCRYPTION_KEY}"

bootstrapAdmin:
  username: "${ADMIN_USER}"
  password: "${ADMIN_PASS}"

frontend:
  publicApiBaseURL: "${PUBLIC_BASE_URL}"
  staticPath: "/app/frontend/dist"

database:
  driver: sqlite
  sqlite:
    path: "${APP_DATA_DIR}/backend.db"

runtimeStore:
  driver: memory

deployment:
  replicas: 1
  clusterID: "grok2api-haos"
  sharedMedia: false

media:
  driver: local
  local:
    path: "${APP_DATA_DIR}/media"

routing:
  reasoningReplayEnabled: true
  reasoningReplayTTL: 1h
  reasoningReplayMaxEntries: 10240
  accountIsolatedConnections: false
  segmentedSelectorEnabled: true

audit:
  bufferSize: 16384
  batchSize: 256
  flushInterval: 250ms
  commitDelay: 5ms
  retentionDays: 7
  ledgerMode: enforce

qualityGuard:
  enabled: false
EOF
chmod 0600 "${CONFIG_FILE}"
unset ADMIN_PASS JWT_SECRET CREDENTIAL_ENCRYPTION_KEY

chown -R grok2api:grok2api "${DATA_DIR}" "${RUN_DIR}" /var/lib/grok2api-quality-guard

log_info "Database: ${APP_DATA_DIR}/backend.db (SQLite, credentials AES-256-GCM encrypted at field level)."
log_info "OpenAI-compatible API: http://<home-assistant-ip>:8000/v1"
log_info "Web UI / admin API:    http://<home-assistant-ip>:8000/"
log_info "Home Assistant Ingress is intentionally disabled (see DOCS.md)."

# ---------------------------------------------------------------------------
# 4. Start the application
# ---------------------------------------------------------------------------
if [ "$#" -eq 0 ]; then
    # shellcheck disable=SC2206
    set -- /app/grok2api --config "${CONFIG_FILE}" --listen 0.0.0.0:8000
fi

su-exec grok2api:grok2api "$@" &
APP_PID=$!
log_info "Grok2API started with pid ${APP_PID}."

forward_signal() {
    log_info "Termination signal received, forwarding to pid ${APP_PID}."
    kill -TERM "${APP_PID}" 2>/dev/null || true
}
trap forward_signal TERM INT QUIT

# Post-start bootstrap runs as the unprivileged user and only talks to
# 127.0.0.1:8000 with the credentials supplied by the Supervisor options.
su-exec grok2api:grok2api env \
    HAOS_ADMIN_USER="${ADMIN_USER}" \
    HAOS_ADMIN_PASSWORD_FROM_OPTIONS_FILE=1 \
    HAOS_BOOTSTRAP_CLIENT_KEY="${BOOTSTRAP_CLIENT_KEY}" \
    HAOS_CLIENT_KEY_NAME="${CLIENT_KEY_NAME}" \
    /usr/local/bin/haos-bootstrap.sh &

rc=0
while :; do
    if wait "${APP_PID}"; then rc=0; else rc=$?; fi
    if kill -0 "${APP_PID}" 2>/dev/null; then
        # `wait` was interrupted by a trapped signal; keep waiting.
        continue
    fi
    break
done

log_info "Grok2API exited with code ${rc}."
exit "${rc}"
