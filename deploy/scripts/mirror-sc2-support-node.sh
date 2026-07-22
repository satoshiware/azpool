#!/usr/bin/env bash
# =============================================================================
# mirror-sc2-support-node.sh
# -----------------------------------------------------------------------------
# Reproduce an AZPool *support-node* software layout (sc-2 style):
#   azcoind + Template Provider + pool-ledger timers.
#
# Explicit non-goals for this playbook (LOCKED):
#   - NO Bitcoin Core / bitcoind (omit entirely; remote/upstream handles BTC if needed)
#   - NO local pool-sv2 (remote pool instances run SV2)
#
# Canonical production checkout (this host / runbooks / systemd):
#   /opt/azcoin-super/src/azpool   @ origin/main
#   Recommended pin: branch main (validated on sc-2 at 344d060)
#
# Git PR / development clone (separate from runtime):
#   ~/repos/azpool  — sync to origin/main, then deploy/checkout into /opt
#
# Source installers (do not reinvent):
#   sc-node  → azcoin-install.sh
#   azpool   → deploy/scripts/build|install-support-node.sh
#            → deploy/scripts/install-sc-node-pool-ledger.sh
#            → deploy/scripts/install-azcoin-sc-node-fresh-cycle-automation.sh
#            → deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh
#            → deploy/scripts/install-azc-payout-readonly-wrapper.sh
#
# Timers matching support-node intent on sc-2:
#   enable:  pool-collector, fresh-cycle-automation, support-wallet-reward-scan
#   disable: payout-scheduler (service may be installed; timer stays off)
#
# Usage:
#   DRY_RUN=1 ./deploy/scripts/mirror-sc2-support-node.sh --phase all
#   sudo ./deploy/scripts/mirror-sc2-support-node.sh --phase all
#   ./deploy/scripts/mirror-sc2-support-node.sh --phase status
#
# Safe defaults:
#   - Does NOT start services unless START_SERVICES=1
#   - Does NOT enable payout-scheduler timer
#   - Does NOT copy secrets (see --phase secrets-hint)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PHASE="help"
DRY_RUN="${DRY_RUN:-0}"
START_SERVICES="${START_SERVICES:-0}"
ENABLE_PAYOUT_SCHEDULER_TIMER="${ENABLE_PAYOUT_SCHEDULER_TIMER:-0}"

OPERATOR_USER="${OPERATOR_USER:-${SUDO_USER:-${USER:-benc}}}"
REPOS_ROOT="${REPOS_ROOT:-/home/${OPERATOR_USER}/repos}"
SC_NODE_REPO="${SC_NODE_REPO:-${REPOS_ROOT}/sc-node}"
AZPOOL_DEV_REPO="${AZPOOL_DEV_REPO:-${REPOS_ROOT}/azpool}"

# Production runtime tree — systemd WorkingDirectory / PYTHONPATH on support nodes
PROD_AZPOOL="${PROD_AZPOOL:-/opt/azcoin-super/src/azpool}"
AZPOOL_PIN_REF="${AZPOOL_PIN_REF:-origin/main}"
AZPOOL_PIN_SHA_HINT="${AZPOOL_PIN_SHA_HINT:-344d06091ea0fcb2e65904d60b79f261dd8549af}"

AZPOOL_GIT_URL="${AZPOOL_GIT_URL:-https://github.com/satoshiware/azpool.git}"
SC_NODE_GIT_URL="${SC_NODE_GIT_URL:-https://github.com/satoshiware/sc-node.git}"

# Prefer production tree; fall back to this checkout when seeding a fresh host
AZPOOL_LIVE="${AZPOOL_LIVE:-}"
if [[ -z "${AZPOOL_LIVE}" ]]; then
  if [[ -d "${PROD_AZPOOL}/.git" || -x "${PROD_AZPOOL}/deploy/scripts/install-support-node.sh" ]]; then
    AZPOOL_LIVE="${PROD_AZPOOL}"
  else
    AZPOOL_LIVE="${REPO_ROOT}"
  fi
fi

# sc-node/azcoin-install.sh hardcodes AZCOIN_BIN_PARENT=/home/benc/repos/sc-node
SC_NODE_HARDCODED_PARENT="${SC_NODE_HARDCODED_PARENT:-/home/benc/repos/sc-node}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
need_root() { [[ "${EUID}" -eq 0 ]] || die "phase requires root (sudo)"; }
run() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "DRY_RUN: $*"
  else
    log "+ $*"
    "$@"
  fi
}

usage() {
  cat <<EOF
mirror-sc2-support-node.sh — AZPool support-node mirror (no bitcoin, no pool-sv2)

Canonical runtime: ${PROD_AZPOOL} (pin ${AZPOOL_PIN_REF}; hint SHA ${AZPOOL_PIN_SHA_HINT})
Active AZPOOL_LIVE: ${AZPOOL_LIVE}

Phases:
  prereqs       apt packages; note docker/rust
  clone         Clone/update sc-node + azpool under REPOS_ROOT; seed ${PROD_AZPOOL}
  users         System users azcoin, azcoin-templar, azledger
  azcoin        Wrap sc-node/azcoin-install.sh
  templar       build-support-node.sh + install-support-node.sh
  wrappers      install-azc-payout-readonly-wrapper.sh
  ledger        pool-ledger layout + fresh-cycle + scheduler service + collector + reward-scan
  timers        Enable collector / fresh-cycle / reward-scan; scheduler timer OFF by default
  status        Read-only unit summary
  secrets-hint  Manual config copy checklist (no secret values)
  all           prereqs → clone → users → azcoin → templar → wrappers → ledger → timers → hints → status

Flags / env:
  --phase NAME
  --dry-run | DRY_RUN=1
  START_SERVICES=0|1                    default 0
  ENABLE_PAYOUT_SCHEDULER_TIMER=0|1     default 0
  OPERATOR_USER=...  REPOS_ROOT=...  SC_NODE_REPO=...  PROD_AZPOOL=...  AZPOOL_LIVE=...

Examples:
  DRY_RUN=1 ./deploy/scripts/mirror-sc2-support-node.sh --phase all
  sudo START_SERVICES=0 ./deploy/scripts/mirror-sc2-support-node.sh --phase all
  ./deploy/scripts/mirror-sc2-support-node.sh --phase status
EOF
}

ensure_user() {
  local user="$1" home="$2" comment="$3"
  if id "${user}" &>/dev/null; then
    log "user exists: ${user}"
    return 0
  fi
  need_root
  groupadd --system "${user}" 2>/dev/null || true
  useradd --system --gid "${user}" --create-home --home-dir "${home}" \
    --shell /usr/sbin/nologin --comment "${comment}" "${user}"
  log "created user: ${user}"
}

clone_or_update() {
  local url="$1" dest="$2"
  mkdir -p "$(dirname "${dest}")"
  if [[ -d "${dest}/.git" ]]; then
    log "updating ${dest}"
    git -C "${dest}" fetch --all --prune || true
    git -C "${dest}" status -sb || true
  else
    run git clone "${url}" "${dest}"
  fi
}

maybe_start() {
  local unit="$1"
  if [[ "${START_SERVICES}" == "1" ]]; then
    run systemctl start "${unit}"
  else
    log "START_SERVICES=0 — not starting ${unit}"
  fi
}

as_operator() {
  if [[ "${EUID}" -eq 0 ]]; then
    run sudo -u "${OPERATOR_USER}" -H bash -lc "$*"
  else
    bash -lc "$*"
  fi
}

ensure_sc_node_tarball_parent() {
  # Documented hardcode in sc-node/azcoin-install.sh — do not rewrite sc-node here.
  if [[ "${SC_NODE_REPO}" == "${SC_NODE_HARDCODED_PARENT}" ]]; then
    return 0
  fi
  log "NOTE: sc-node/azcoin-install.sh hardcodes AZCOIN_BIN_PARENT=${SC_NODE_HARDCODED_PARENT}"
  if [[ -d "${SC_NODE_HARDCODED_PARENT}" ]]; then
    log "hardcoded parent exists — azcoin-install will look for tarball there"
    return 0
  fi
  if [[ -d "${SC_NODE_REPO}" ]]; then
    need_root
    run mkdir -p "$(dirname "${SC_NODE_HARDCODED_PARENT}")"
    if [[ ! -e "${SC_NODE_HARDCODED_PARENT}" ]]; then
      run ln -s "${SC_NODE_REPO}" "${SC_NODE_HARDCODED_PARENT}"
      log "symlinked ${SC_NODE_HARDCODED_PARENT} -> ${SC_NODE_REPO} for azcoin-install portability"
    fi
  else
    log "WARNING: neither ${SC_NODE_REPO} nor ${SC_NODE_HARDCODED_PARENT} present — azcoin phase may fail"
  fi
}

phase_prereqs() {
  need_root
  log "=== PHASE prereqs ==="
  export DEBIAN_FRONTEND=noninteractive
  run apt-get update -y
  run apt-get install -y \
    ca-certificates curl git jq python3 python-is-python3 \
    build-essential pkg-config libssl-dev libzmq3-dev \
    postgresql postgresql-contrib \
    ufw
  if ! command -v docker >/dev/null 2>&1; then
    log "Docker not found — install docker.io or Docker CE separately, then: systemctl enable --now docker"
  else
    run systemctl enable docker.service || true
  fi
  run systemctl enable postgresql.service || true
  if ! command -v rustc >/dev/null 2>&1 && ! command -v cargo >/dev/null 2>&1; then
    log "Rust/cargo not found — install rustup as ${OPERATOR_USER} before templar build:"
    log "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  fi
  if [[ "${START_SERVICES}" == "1" ]]; then
    run systemctl start postgresql.service || true
    run systemctl start docker.service || true
  fi
}

phase_clone() {
  log "=== PHASE clone ==="
  if [[ "${EUID}" -eq 0 ]]; then
    run sudo -u "${OPERATOR_USER}" mkdir -p "${REPOS_ROOT}"
    run sudo -u "${OPERATOR_USER}" bash -c "git clone '${SC_NODE_GIT_URL}' '${SC_NODE_REPO}' 2>/dev/null || git -C '${SC_NODE_REPO}' fetch --all --prune"
    run sudo -u "${OPERATOR_USER}" bash -c "git clone '${AZPOOL_GIT_URL}' '${AZPOOL_DEV_REPO}' 2>/dev/null || git -C '${AZPOOL_DEV_REPO}' fetch --all --prune"
  else
    mkdir -p "${REPOS_ROOT}"
    clone_or_update "${SC_NODE_GIT_URL}" "${SC_NODE_REPO}"
    clone_or_update "${AZPOOL_GIT_URL}" "${AZPOOL_DEV_REPO}"
  fi

  if [[ ! -e "${PROD_AZPOOL}" ]]; then
    need_root
    run mkdir -p /opt/azcoin-super/src /opt/azcoin-super/bin /opt/azcoin-super/releases
    local seed="${AZPOOL_DEV_REPO}"
    [[ -d "${seed}/.git" ]] || seed="${REPO_ROOT}"
    [[ -d "${seed}/.git" ]] || die "no azpool seed tree; run clone or set AZPOOL_DEV_REPO"
    run git clone -- "${seed}" "${PROD_AZPOOL}"
    run chown -R "${OPERATOR_USER}:${OPERATOR_USER}" "${PROD_AZPOOL}"
    log "seeded ${PROD_AZPOOL} from ${seed}"
  fi

  if [[ -d "${PROD_AZPOOL}/.git" ]]; then
    log "production tree: $(git -C "${PROD_AZPOOL}" rev-parse --short HEAD 2>/dev/null || echo unknown) ($(git -C "${PROD_AZPOOL}" status -sb 2>/dev/null | head -1))"
    log "recommended pin: ${AZPOOL_PIN_REF} (hint ${AZPOOL_PIN_SHA_HINT})"
    log "to align: git -C ${PROD_AZPOOL} fetch origin && git -C ${PROD_AZPOOL} checkout ${AZPOOL_PIN_REF}"
  fi
  AZPOOL_LIVE="${PROD_AZPOOL}"
}

phase_users() {
  need_root
  log "=== PHASE users (no bitcoin user) ==="
  ensure_user azcoin /home/azcoin "AZCoin Core daemon"
  ensure_user azcoin-templar /var/lib/azcoin-super/templar "AZCoin Template Provider service user"
  ensure_user azledger /var/lib/azcoin-super/pool-ledger "AZCoin pool ledger"
  run install -d -o azcoin-templar -g azcoin-templar -m 0750 /var/lib/azcoin-super/templar
  run install -d -o azcoin-templar -g azcoin-templar -m 0750 /var/log/templar
  run install -d -o azcoin-templar -g azcoin-templar -m 0750 /var/log/azcoin-super/templar
  run install -d -o azledger -g azledger -m 0750 /var/lib/azcoin-super/pool-ledger
  run install -d -o azledger -g azledger -m 0750 /var/log/azcoin-super/pool-ledger
}

phase_azcoin() {
  need_root
  log "=== PHASE azcoin (wrap sc-node installer; bitcoin omitted) ==="
  [[ -x "${SC_NODE_REPO}/azcoin-install.sh" ]] || die "missing ${SC_NODE_REPO}/azcoin-install.sh"
  ensure_sc_node_tarball_parent
  if [[ -f /usr/local/bin/azcoind ]]; then
    log "azcoind already present at /usr/local/bin/azcoind — skipping reinstall"
    /usr/local/bin/azcoind --version 2>&1 | head -3 || true
  else
    run bash "${SC_NODE_REPO}/azcoin-install.sh"
  fi
  run systemctl enable azcoind.service
  maybe_start azcoind.service
  log "azcoin paths: conf=/etc/azcoin/azcoin.conf datadir=/var/lib/azcoin unit=azcoind.service"
}

phase_templar() {
  need_root
  log "=== PHASE templar ==="
  local root="${AZPOOL_LIVE}"
  [[ -x "${root}/deploy/scripts/build-support-node.sh" ]] || die "missing build-support-node.sh under ${root}"
  as_operator "cd '${root}' && ./deploy/scripts/build-support-node.sh"
  run bash "${root}/deploy/scripts/install-support-node.sh"
  run install -d -o root -g azcoin-templar -m 0750 /etc/azcoin-super/templar
  if [[ ! -f /etc/azcoin-super/templar/azcoin-template-provider.toml ]]; then
    log "MISSING runtime config: /etc/azcoin-super/templar/azcoin-template-provider.toml — copy from source host"
  fi
  run systemctl enable azcoin-template-provider.service
  maybe_start azcoin-template-provider.service
}

phase_wrappers() {
  need_root
  log "=== PHASE wrappers ==="
  local root="${AZPOOL_LIVE}"
  if [[ -x "${root}/deploy/scripts/install-azc-payout-readonly-wrapper.sh" ]]; then
    run bash "${root}/deploy/scripts/install-azc-payout-readonly-wrapper.sh"
  else
    log "install-azc-payout-readonly-wrapper.sh not found under ${root} — skip"
  fi
}

install_reward_scan_units() {
  local root="$1"
  local svc_src="${root}/deploy/systemd/azcoin-support-wallet-reward-scan.service"
  local tmr_src="${root}/deploy/systemd/azcoin-support-wallet-reward-scan.timer"
  if [[ -f "${svc_src}" && -f "${tmr_src}" ]]; then
    run install -m 0644 "${svc_src}" /etc/systemd/system/azcoin-support-wallet-reward-scan.service
    run install -m 0644 "${tmr_src}" /etc/systemd/system/azcoin-support-wallet-reward-scan.timer
    return 0
  fi
  log "reward-scan unit templates missing under ${root}/deploy/systemd — writing known-good units"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "DRY_RUN: would write azcoin-support-wallet-reward-scan.{service,timer}"
    return 0
  fi
  cat >/etc/systemd/system/azcoin-support-wallet-reward-scan.service <<'UNIT'
[Unit]
Description=AZCoin support wallet reward event scanner
After=network-online.target postgresql.service azcoind.service
Wants=network-online.target

[Service]
Type=oneshot
User=azledger
Group=azledger
WorkingDirectory=/opt/azcoin-super/src/azpool
Environment=PYTHONPATH=/opt/azcoin-super/src/azpool
EnvironmentFile=/etc/azcoin-super/pool-ledger/collector.env
ExecStart=/opt/azcoin-super/src/azpool/.venv/bin/python payouts/scripts/support_wallet_reward_events.py scan --wallet wallet --azc-bin /usr/local/bin/azc-payout-readonly --count 5000 --write
NoNewPrivileges=false
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/azcoin-super/pool-ledger /var/log/azcoin-super/pool-ledger
UNIT
  cat >/etc/systemd/system/azcoin-support-wallet-reward-scan.timer <<'UNIT'
[Unit]
Description=Run AZCoin support wallet reward event scanner every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
AccuracySec=30s
Persistent=true
Unit=azcoin-support-wallet-reward-scan.service

[Install]
WantedBy=timers.target
UNIT
}

phase_ledger() {
  need_root
  log "=== PHASE ledger ==="
  local root="${AZPOOL_LIVE}"
  [[ -x "${root}/deploy/scripts/install-sc-node-pool-ledger.sh" ]] || die "missing install-sc-node-pool-ledger.sh under ${root}"
  run bash "${root}/deploy/scripts/install-sc-node-pool-ledger.sh"
  run bash "${root}/deploy/scripts/install-azcoin-sc-node-fresh-cycle-automation.sh" --timer '*:0/30'
  # Install scheduler *service* (safe report-only defaults); timer stays disabled in phase_timers
  run bash "${root}/deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh"

  if [[ -f "${root}/deploy/systemd/azcoin-pool-collector.service" ]]; then
    run install -m 0644 "${root}/deploy/systemd/azcoin-pool-collector.service" /etc/systemd/system/azcoin-pool-collector.service
    run install -m 0644 "${root}/deploy/systemd/azcoin-pool-collector.timer" /etc/systemd/system/azcoin-pool-collector.timer
  else
    die "missing ${root}/deploy/systemd/azcoin-pool-collector.service"
  fi
  install_reward_scan_units "${root}"

  if [[ ! -x "${root}/.venv/bin/python" ]]; then
    log "creating ${root}/.venv (install payouts package / requirements)"
    as_operator "cd '${root}' && python3 -m venv .venv && .venv/bin/pip install -U pip && (.venv/bin/pip install -e './payouts' || .venv/bin/pip install -r payouts/requirements.txt || true)"
  fi
  run systemctl daemon-reload
  log "Fill /etc/azcoin-super/pool-ledger/*.env (DATABASE_URL etc.) before enabling timers."
}

phase_timers() {
  need_root
  log "=== PHASE timers (support-node intent) ==="
  run systemctl daemon-reload
  run systemctl enable azcoin-pool-collector.timer
  run systemctl enable azcoin-sc-node-fresh-cycle-automation.timer
  run systemctl enable azcoin-support-wallet-reward-scan.timer
  if [[ "${ENABLE_PAYOUT_SCHEDULER_TIMER}" == "1" ]]; then
    run systemctl enable azcoin-sc-node-payout-scheduler.timer
  else
    run systemctl disable azcoin-sc-node-payout-scheduler.timer 2>/dev/null || true
    log "payout-scheduler.timer left DISABLED (support-node default)"
  fi
  if [[ "${START_SERVICES}" == "1" ]]; then
    run systemctl start azcoin-pool-collector.timer
    run systemctl start azcoin-sc-node-fresh-cycle-automation.timer
    run systemctl start azcoin-support-wallet-reward-scan.timer
  fi
}

phase_status() {
  log "=== PHASE status (read-only) ==="
  log "AZPOOL_LIVE=${AZPOOL_LIVE}"
  if [[ -d "${AZPOOL_LIVE}/.git" ]]; then
    log "AZPOOL_LIVE HEAD=$(git -C "${AZPOOL_LIVE}" rev-parse HEAD 2>/dev/null || echo n/a)"
  fi
  local units=(
    azcoind.service
    azcoin-template-provider.service
    docker.service
    postgresql.service
    azcoin-pool-collector.timer
    azcoin-sc-node-fresh-cycle-automation.timer
    azcoin-support-wallet-reward-scan.timer
    azcoin-sc-node-payout-scheduler.timer
  )
  printf '%-48s %-12s %-12s\n' UNIT ENABLED ACTIVE
  for u in "${units[@]}"; do
    en="$(systemctl is-enabled "${u}" 2>/dev/null || echo n/a)"
    ac="$(systemctl is-active "${u}" 2>/dev/null || echo n/a)"
    printf '%-48s %-12s %-12s\n' "${u}" "${en}" "${ac}"
  done
  echo
  echo "Expected on mirrored support node:"
  echo "  azcoind, templar, docker, postgresql, collector/fresh-cycle/reward-scan timers = enabled"
  echo "  payout-scheduler.timer = disabled"
  echo "  bitcoind / pool-sv2 = NOT installed by this playbook"
}

phase_secrets_hint() {
  cat <<'EOF'
=== Manual secret / config copy checklist (do NOT commit these) ===

From source host, copy with appropriate ownership:

  /etc/azcoin/azcoin.conf                                     azcoin:azcoin
  /etc/azcoin-super/templar/azcoin-template-provider.toml     root:azcoin-templar 0640
  /etc/azcoin-super/pool-ledger/collector.env                 root:azledger 0640
  /etc/azcoin-super/pool-ledger/fresh-cycle-automation.env
  /etc/azcoin-super/pool-ledger/payout-scheduler.env
  /home/azcoin/rpcpassword                                    (if present)
  WireGuard: /etc/wireguard/                                  (if used for pool monitoring)

Also:
  - Create Postgres DB/role matching DATABASE_URL in collector.env
  - Apply payouts migrations as needed (see docs/runbooks/)
  - Do NOT shell-source pool-ledger env as a normal user (dir is root:azledger 0750)
  - Prefer: sudo ./deploy/scripts/discover-sc-node-current-state.sh on source

NOT required by this playbook:
  - /etc/bitcoin/bitcoin.conf
  - /etc/azcoin-super/pool/pool-config.toml (local pool-sv2)

EOF
}

phase_all() {
  phase_prereqs
  phase_clone
  phase_users
  phase_azcoin
  phase_templar
  phase_wrappers
  phase_ledger
  phase_timers
  phase_secrets_hint
  phase_status
  log "=== DONE — review secrets-hint, fill configs, then START_SERVICES=1 selectively ==="
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) PHASE=help; shift ;;
    *) die "unknown arg: $1 (try --help)" ;;
  esac
done

case "${PHASE}" in
  help|"") usage ;;
  prereqs) phase_prereqs ;;
  clone) phase_clone ;;
  users) phase_users ;;
  azcoin) phase_azcoin ;;
  templar) phase_templar ;;
  wrappers) phase_wrappers ;;
  ledger) phase_ledger ;;
  timers) phase_timers ;;
  status) phase_status ;;
  secrets-hint) phase_secrets_hint ;;
  all) phase_all ;;
  *) die "unknown phase: ${PHASE}" ;;
esac
