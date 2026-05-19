#!/usr/bin/env bash
set -euo pipefail

# Smoke checks for support-node Template Provider build/install prerequisites.
# Does not read or print /etc config contents.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLAR_CARGO="${REPO_ROOT}/templar/Cargo.toml"
RELEASE_DIR="${REPO_ROOT}/templar/target/release"
INSTALL_BIN="/opt/azcoin-super/bin/azcoin-template-provider"
SYSTEMD_TEMPLATE="${REPO_ROOT}/deploy/systemd/azcoin-template-provider.service"
CONFIG_PATH="/etc/azcoin-super/templar/azcoin-template-provider.toml"
LOG_DIR="/var/log/templar"
SERVICE_USER="azcoin-templar"
EXPECTED_BIN="azcoin-template-provider"
OLD_REPO_PATTERNS=(
    'github.com/satoshiware/azcoin-template-provider'
    'git clone.*azcoin-template-provider'
    'templar-source'
    'repos/azcoin-template-provider'
)

failures=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

if [[ -f "${TEMPLAR_CARGO}" ]]; then
    pass "templar/Cargo.toml exists (${TEMPLAR_CARGO})"
else
    fail "templar/Cargo.toml missing (${TEMPLAR_CARGO})"
fi

old_repo_hits=()
check_paths=(
    "${REPO_ROOT}/README.md"
    "${REPO_ROOT}/deploy/scripts/build-support-node.sh"
    "${REPO_ROOT}/deploy/scripts/install-support-node.sh"
    "${REPO_ROOT}/docs/adr"
    "${REPO_ROOT}/docs/runbooks/template-provider-build-install.md"
)
for pattern in "${OLD_REPO_PATTERNS[@]}"; do
    while IFS= read -r hit; do
        [[ -n "${hit}" ]] && old_repo_hits+=("${hit}")
    done < <(grep -nE "${pattern}" "${check_paths[@]}" 2>/dev/null || true)
done

if [[ ${#old_repo_hits[@]} -eq 0 ]]; then
    pass "allowed build/install docs do not require the old standalone repository"
else
    fail "old standalone repository still referenced in allowed paths"
    printf '  %s\n' "${old_repo_hits[@]}" >&2
fi

mapfile -t candidates < <(find "${RELEASE_DIR}" -maxdepth 1 -type f -executable ! -name '*.d' 2>/dev/null | sort)
artifact=""
for candidate in "${candidates[@]}"; do
    if [[ "$(basename "${candidate}")" == "${EXPECTED_BIN}" ]]; then
        artifact="${candidate}"
        break
    fi
done

if [[ -n "${artifact}" && -x "${artifact}" ]]; then
    pass "build artifact exists (${artifact})"
elif [[ ${#candidates[@]} -gt 0 ]]; then
    fail "expected build artifact '${EXPECTED_BIN}' not found; found: ${candidates[*]}"
else
    fail "build artifact missing under ${RELEASE_DIR}; run deploy/scripts/build-support-node.sh"
fi

if [[ -x "${INSTALL_BIN}" ]]; then
    pass "installed binary exists (${INSTALL_BIN})"
else
    fail "installed binary missing or not executable (${INSTALL_BIN})"
fi

if [[ -f "${SYSTEMD_TEMPLATE}" ]]; then
    pass "systemd template exists (${SYSTEMD_TEMPLATE})"
else
    fail "systemd template missing (${SYSTEMD_TEMPLATE})"
fi

if [[ -f "${CONFIG_PATH}" ]]; then
    pass "runtime config path present (${CONFIG_PATH})"
else
    echo "WARN: runtime config path missing (${CONFIG_PATH}) — create from templar/config/azcoin-template-provider.toml.example before starting the service"
fi

if [[ -d "${LOG_DIR}" ]]; then
    log_owner="$(stat -c '%U:%G' "${LOG_DIR}" 2>/dev/null || true)"
    log_mode="$(stat -c '%a' "${LOG_DIR}" 2>/dev/null || true)"
    if [[ "${log_owner}" == "${SERVICE_USER}:${SERVICE_USER}" && "${log_mode}" == "750" ]]; then
        writable=false
        if id "${SERVICE_USER}" &>/dev/null; then
            if [[ "$(id -u)" -eq 0 ]] && runuser -u "${SERVICE_USER}" -- test -w "${LOG_DIR}" 2>/dev/null; then
                writable=true
            elif sudo -n -u "${SERVICE_USER}" test -w "${LOG_DIR}" 2>/dev/null; then
                writable=true
            elif [[ "$(id -u)" -ne 0 ]]; then
                writable=true
            fi
        fi
        if [[ "${writable}" == true ]]; then
            pass "log directory exists and is writable by ${SERVICE_USER} (${LOG_DIR})"
        else
            fail "log directory exists but is not writable by ${SERVICE_USER} (${LOG_DIR})"
        fi
    else
        fail "log directory ownership/mode incorrect (${LOG_DIR}; want ${SERVICE_USER}:${SERVICE_USER} 0750, got ${log_owner:-unknown} ${log_mode:-unknown})"
    fi
else
    fail "log directory missing (${LOG_DIR})"
fi

if [[ "${failures}" -gt 0 ]]; then
    echo "SMOKE_FAIL failures=${failures}" >&2
    exit 1
fi

echo "SMOKE_OK"
