#!/usr/bin/env bash
set -euo pipefail

# Install the templar-built Template Provider binary and systemd unit on a support node.
# Runtime config remains outside Git at /etc/azcoin-super/templar.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLAR_DIR="${REPO_ROOT}/templar"
RELEASE_DIR="${TEMPLAR_DIR}/target/release"
INSTALL_BIN="/opt/azcoin-super/bin/azcoin-template-provider"
RELEASES_DIR="/opt/azcoin-super/releases/template-provider"
SYSTEMD_SRC="${REPO_ROOT}/deploy/systemd/azcoin-template-provider.service"
SYSTEMD_DST="/etc/systemd/system/azcoin-template-provider.service"
EXPECTED_BIN="azcoin-template-provider"

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

if [[ ! -f "${TEMPLAR_DIR}/Cargo.toml" ]]; then
    echo "ERROR: canonical source missing: ${TEMPLAR_DIR}/Cargo.toml" >&2
    exit 1
fi

mapfile -t candidates < <(find "${RELEASE_DIR}" -maxdepth 1 -type f -executable ! -name '*.d' 2>/dev/null | sort)
if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "ERROR: no release executable found under ${RELEASE_DIR}; run deploy/scripts/build-support-node.sh first" >&2
    exit 1
fi

artifact=""
for candidate in "${candidates[@]}"; do
    base="$(basename "${candidate}")"
    if [[ "${base}" == "${EXPECTED_BIN}" ]]; then
        artifact="${candidate}"
        break
    fi
done

if [[ -z "${artifact}" ]]; then
    echo "ERROR: expected release binary '${EXPECTED_BIN}' not found under ${RELEASE_DIR}" >&2
    echo "Found executables:" >&2
    printf '  %s\n' "${candidates[@]}" >&2
    exit 1
fi

install -d -o root -g root -m 0755 /opt/azcoin-super/bin
install -d -o root -g root -m 0755 "${RELEASES_DIR}"

timestamp="$(date -u +%Y%m%d%H%M%S)"
if [[ -f "${INSTALL_BIN}" ]]; then
    backup="${RELEASES_DIR}/${EXPECTED_BIN}.${timestamp}"
    cp -a "${INSTALL_BIN}" "${backup}"
    echo "BACKUP_OK path=${backup}"
fi

install -m 0755 -o root -g root "${artifact}" "${INSTALL_BIN}"
echo "INSTALL_OK path=${INSTALL_BIN} source=${artifact}"

install -d -o azcoin-templar -g azcoin-templar -m 0750 /var/log/templar
echo "LOGDIR_OK path=/var/log/templar"

if [[ ! -f "${SYSTEMD_SRC}" ]]; then
    echo "ERROR: systemd unit template missing: ${SYSTEMD_SRC}" >&2
    exit 1
fi

install -m 0644 -o root -g root "${SYSTEMD_SRC}" "${SYSTEMD_DST}"
systemctl daemon-reload
echo "SYSTEMD_OK unit=${SYSTEMD_DST}"
