#!/usr/bin/env bash
# Post-start bootstrap for the Grok2API Home Assistant add-on.
#
# Runs as the unprivileged `grok2api` user once the HTTP server answers on
# 127.0.0.1:8000. It uses only the official admin API:
#
#   1. Admin password is authoritative in the add-on options. If the running
#      database still has an older password, sign in with it and rotate it to
#      the option value through `PUT /api/admin/v1/me/password`.
#   2. Make sure one client API key with the configured name exists so that
#      Home Assistant / OpenAI-compatible clients have something to use.
#
# Nothing secret is ever written to the log: no password, no API key, no
# token, no cookie. The API key secret is revealed by the user in the Web UI
# (API keys -> reveal) and is never printed here.
set -Eeuo pipefail
umask 077

# Paths are fixed inside the add-on container. The HAOS_* overrides exist only
# so the same script can be exercised by the integration harness on a
# developer machine; the add-on never sets them.
readonly BASE_URL="${HAOS_BASE_URL:-http://127.0.0.1:8000}"
readonly OPTIONS_FILE="${HAOS_OPTIONS_FILE:-/data/options.json}"
readonly APPLIED_FILE="${HAOS_APPLIED_FILE:-/data/config/admin.applied}"

ADMIN_USER="${HAOS_ADMIN_USER:-admin}"
WANT_BOOTSTRAP_KEY="${HAOS_BOOTSTRAP_CLIENT_KEY:-true}"
CLIENT_KEY_NAME="${HAOS_CLIENT_KEY_NAME:-Home Assistant}"
USE_OPTIONS_PASSWORD="${HAOS_ADMIN_PASSWORD_FROM_OPTIONS_FILE:-1}"

WORK_DIR=""

log() {
    local level="$1"; shift
    printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${level}" "$*"
}
log_info() { log INFO "$*"; }
log_warn() { log WARNING "$*"; }

cleanup() {
    if [ -n "${WORK_DIR}" ] && [ -d "${WORK_DIR}" ]; then
        rm -rf "${WORK_DIR}"
    fi
}
# Clean up on normal exit AND on SIGINT/SIGTERM: if the container is stopped
# while wait_for_health is sleeping, some shells leave an EXIT-only trap
# unexecuted. cleanup is idempotent, so running twice is harmless.
trap cleanup EXIT INT TERM

WORK_DIR="$(mktemp -d)"
mkdir -p "$(dirname "${APPLIED_FILE}")" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Wait for the HTTP server (liveness endpoint only; no dependency on accounts)
# ---------------------------------------------------------------------------
wait_for_health() {
    local i code
    for i in $(seq 1 120); do
        code="$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "${BASE_URL}/healthz" 2>/dev/null || true)"
        if [ "${code}" = "200" ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

if ! wait_for_health; then
    log_warn "HTTP server did not answer on ${BASE_URL}/healthz within 240s; skipping bootstrap."
    exit 0
fi
log_info "HTTP server is reachable; running post-start bootstrap."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
read_option_password() {
    if [ "${USE_OPTIONS_PASSWORD}" != "1" ] || [ ! -f "${OPTIONS_FILE}" ]; then
        return 1
    fi
    local value
    value="$(jq -r 'if has("admin_password") then (.admin_password // "") else "" end' "${OPTIONS_FILE}" 2>/dev/null || true)"
    [ -n "${value}" ] || return 1
    printf '%s' "${value}"
}

read_applied_password() {
    if [ ! -f "${APPLIED_FILE}" ]; then
        return 1
    fi
    local value
    value="$(jq -r '.password // ""' "${APPLIED_FILE}" 2>/dev/null || true)"
    [ -n "${value}" ] || return 1
    printf '%s' "${value}"
}

# login <password> -> prints the access token, returns non-zero on failure
login() {
    local password="$1"
    local body_file="${WORK_DIR}/login.json"
    local payload code

    payload="$(jq -nc --arg u "${ADMIN_USER}" --arg p "${password}" '{username:$u,password:$p}')"
    code="$(printf '%s' "${payload}" | curl -sS -m 15 \
        -o "${body_file}" -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        --data-binary @- \
        "${BASE_URL}/api/admin/v1/auth/login" 2>/dev/null || true)"

    if [ "${code}" != "200" ]; then
        rm -f "${body_file}"
        return 1
    fi
    local token
    token="$(jq -r '.data.tokens.accessToken // ""' "${body_file}" 2>/dev/null || true)"
    rm -f "${body_file}"
    [ -n "${token}" ] || return 1
    printf '%s' "${token}"
}

save_applied_password() {
    local password="$1"
    jq -nc --arg u "${ADMIN_USER}" --arg p "${password}" \
        '{username:$u,password:$p}' > "${APPLIED_FILE}"
    chmod 0600 "${APPLIED_FILE}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 1. Admin password synchronisation (add-on options win)
# ---------------------------------------------------------------------------
TOKEN=""
DESIRED_PASSWORD="$(read_option_password || true)"

if [ -z "${DESIRED_PASSWORD}" ]; then
    log_warn "No admin_password in ${OPTIONS_FILE}; leaving the stored admin password untouched."
    log_warn "Set admin_password in the add-on Configuration tab to let the add-on manage it."
else
    if TOKEN="$(login "${DESIRED_PASSWORD}")"; then
        log_info "Admin login OK with the password from the add-on options."
        save_applied_password "${DESIRED_PASSWORD}"
    else
        log_info "Admin login with the option password failed; checking the previously applied password."
        PREVIOUS_PASSWORD="$(read_applied_password || true)"
        PREVIOUS_TOKEN=""
        if [ -n "${PREVIOUS_PASSWORD}" ] && PREVIOUS_TOKEN="$(login "${PREVIOUS_PASSWORD}")"; then
            log_info "Signing in with the previously applied password succeeded; rotating to the option password."
            code="$(jq -nc --arg cur "${PREVIOUS_PASSWORD}" --arg new "${DESIRED_PASSWORD}" \
                '{currentPassword:$cur,newPassword:$new}' \
                | curl -sS -m 20 -X PUT -o /dev/null -w '%{http_code}' \
                    -H 'Content-Type: application/json' \
                    -H "Authorization: Bearer ${PREVIOUS_TOKEN}" \
                    --data-binary @- \
                    "${BASE_URL}/api/admin/v1/me/password" 2>/dev/null || true)"
            if [ "${code}" = "200" ]; then
                log_info "Admin password rotated to the add-on option value (previous sessions revoked)."
                save_applied_password "${DESIRED_PASSWORD}"
                if ! TOKEN="$(login "${DESIRED_PASSWORD}")"; then
                    log_warn "Password rotation succeeded but the follow-up login failed; skipping API key bootstrap."
                    TOKEN=""
                fi
            else
                log_warn "Admin password rotation failed (HTTP ${code}); the stored password was left unchanged."
            fi
        else
            log_warn "Could not sign in with either the option password or the previously applied password."
            log_warn "The admin password was NOT changed. Set admin_password to the current password, or set a new"
            log_warn "password and restart; the add-on will not overwrite an unknown password automatically."
            TOKEN=""
        fi
    fi
fi

if [ -z "${TOKEN}" ]; then
    log_warn "Skipping client API key bootstrap because there is no authenticated admin session."
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. Client API key bootstrap (idempotent, by name)
# ---------------------------------------------------------------------------
if [ "${WANT_BOOTSTRAP_KEY}" != "true" ]; then
    log_info "bootstrap_client_key is disabled; not creating a client API key."
    exit 0
fi

LIST_FILE="${WORK_DIR}/keys.json"
code="$(curl -sS -m 20 -o "${LIST_FILE}" -w '%{http_code}' \
    -H "Authorization: Bearer ${TOKEN}" \
    --get --data-urlencode "search=${CLIENT_KEY_NAME}" --data-urlencode "pageSize=100" \
    "${BASE_URL}/api/admin/v1/client-keys" 2>/dev/null || true)"

if [ "${code}" != "200" ]; then
    log_warn "Could not list client API keys (HTTP ${code}); skipping key bootstrap."
    exit 0
fi

EXISTING_ID="$(jq -r --arg name "${CLIENT_KEY_NAME}" \
    '[.data.items[]? | select(.name == $name)][0].id // ""' "${LIST_FILE}" 2>/dev/null || true)"
EXISTING_ENABLED="$(jq -r --arg name "${CLIENT_KEY_NAME}" \
    '[.data.items[]? | select(.name == $name)][0].enabled // false' "${LIST_FILE}" 2>/dev/null || true)"

if [ -n "${EXISTING_ID}" ]; then
    if [ "${EXISTING_ENABLED}" = "true" ]; then
        log_info "Client API key \"${CLIENT_KEY_NAME}\" already exists (id ${EXISTING_ID}, enabled)."
    else
        log_warn "Client API key \"${CLIENT_KEY_NAME}\" exists (id ${EXISTING_ID}) but is DISABLED; leaving it disabled."
    fi
    log_info "Reveal the key secret in the Web UI: API keys -> ${CLIENT_KEY_NAME} -> reveal."
    exit 0
fi

CREATE_FILE="${WORK_DIR}/create.json"
payload="$(jq -nc --arg name "${CLIENT_KEY_NAME}" '{name:$name}')"
code="$(printf '%s' "${payload}" | curl -sS -m 20 -o "${CREATE_FILE}" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${TOKEN}" \
    --data-binary @- \
    "${BASE_URL}/api/admin/v1/client-keys" 2>/dev/null || true)"

case "${code}" in
    201|200)
        NEW_ID="$(jq -r '.data.key.id // ""' "${CREATE_FILE}" 2>/dev/null || true)"
        # Remove the response immediately: it contains the plaintext secret.
        rm -f "${CREATE_FILE}"
        log_info "Created client API key \"${CLIENT_KEY_NAME}\" (id ${NEW_ID})."
        log_info "The secret is not printed. Open the Web UI: API keys -> ${CLIENT_KEY_NAME} -> reveal."
        ;;
    409)
        rm -f "${CREATE_FILE}"
        log_info "Client API key \"${CLIENT_KEY_NAME}\" already exists (conflict); nothing to do."
        ;;
    *)
        rm -f "${CREATE_FILE}"
        log_warn "Could not create client API key \"${CLIENT_KEY_NAME}\" (HTTP ${code}). Create it in the Web UI."
        ;;
esac

exit 0
