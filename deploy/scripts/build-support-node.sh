#!/usr/bin/env bash
set -euo pipefail

# Build the AZCOIN Template Provider from the canonical azpool/templar source tree.
# Does not clone or fetch the archived standalone azcoin-template-provider repository.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLAR_DIR="${REPO_ROOT}/templar"
RELEASE_DIR="${TEMPLAR_DIR}/target/release"

if [[ ! -f "${TEMPLAR_DIR}/Cargo.toml" ]]; then
    echo "ERROR: canonical source missing: ${TEMPLAR_DIR}/Cargo.toml" >&2
    exit 1
fi

cd "${TEMPLAR_DIR}"
cargo build --release

mapfile -t candidates < <(find "${RELEASE_DIR}" -maxdepth 1 -type f -executable ! -name '*.d' 2>/dev/null | sort)
if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "ERROR: no release executable found under ${RELEASE_DIR}" >&2
    exit 1
fi

expected="azcoin-template-provider"
artifact=""
for candidate in "${candidates[@]}"; do
    base="$(basename "${candidate}")"
    if [[ "${base}" == "${expected}" ]]; then
        artifact="${candidate}"
        break
    fi
done

if [[ -z "${artifact}" ]]; then
    echo "ERROR: expected release binary '${expected}' not found under ${RELEASE_DIR}" >&2
    echo "Found executables:" >&2
    printf '  %s\n' "${candidates[@]}" >&2
    exit 1
fi

echo "BUILD_OK artifact=${artifact}"
