#!/usr/bin/env bash
# Register (or upsert) an SC-node in the azpool ledger: sc_nodes row, identity
# mapping, and payout address. Does NOT send coins, call azc, or print secrets.
#
# Prefer git checkout: /home/benc/azpool/payouts/scripts/register_sc_node.sh
# On the support node under ProtectHome, deploy/copy to:
#   /opt/azcoin-super/src/azpool/payouts/scripts/register_sc_node.sh
#
# Usage:
#   sudo bash register_sc_node.sh \
#     --sc-node-id <id> \
#     --payout-address az1q... \
#     --match-value 'azc-XXXX.' \
#     [--display-name <name>] \
#     [--match-type prefix|exact|glob] \
#     [--payout-enabled] \
#     [--activate-address|--no-activate-address] \
#     [--label <label>] \
#     [--azpool-root <path>]
#
# See docs/runbooks/register-sc-node.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_AZPOOL_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OPT_AZPOOL_ROOT='/opt/azcoin-super/src/azpool'
COLLECTOR_ENV='/etc/azcoin-super/pool-ledger/collector.env'

SC_NODE_ID=''
DISPLAY_NAME=''
PAYOUT_ADDRESS=''
MATCH_TYPE='prefix'
MATCH_VALUE=''
PAYOUT_ENABLED='false'
ACTIVATE_ADDRESS='true'
ADDRESS_LABEL=''
AZPOOL_ROOT=''

usage() {
  cat <<'EOF'
Usage: sudo bash register_sc_node.sh --sc-node-id ID --payout-address az1q... --match-value PREFIX [options]

Required:
  --sc-node-id ID           SC node id (sc_nodes.id)
  --payout-address ADDR     Operator-provided az1q… payout address
  --match-value VALUE       Miner identity prefix (include trailing '.') or exact/glob

Optional:
  --display-name NAME       Default: same as --sc-node-id
  --match-type TYPE         prefix (default) | exact | glob
  --payout-enabled          Set payout_enabled=true (default: leave false / unchanged on re-run)
  --activate-address        Insert pending then activate+default (default)
  --no-activate-address     Leave address as pending_verification, not default
  --label LABEL             Address label (default: "<id> primary")
  --azpool-root PATH        Azpool checkout with payouts/migrations (default: script-relative)
  -h, --help                Show this help

Idempotent: safe to re-run. Does not print DATABASE_URL or passwords.
Does not enable credit/payout automation unless --payout-enabled is passed.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sc-node-id)
      SC_NODE_ID="${2:-}"
      shift 2
      ;;
    --display-name)
      DISPLAY_NAME="${2:-}"
      shift 2
      ;;
    --payout-address)
      PAYOUT_ADDRESS="${2:-}"
      shift 2
      ;;
    --match-type)
      MATCH_TYPE="${2:-}"
      shift 2
      ;;
    --match-value)
      MATCH_VALUE="${2:-}"
      shift 2
      ;;
    --payout-enabled)
      PAYOUT_ENABLED='true'
      shift
      ;;
    --activate-address)
      ACTIVATE_ADDRESS='true'
      shift
      ;;
    --no-activate-address)
      ACTIVATE_ADDRESS='false'
      shift
      ;;
    --label)
      ADDRESS_LABEL="${2:-}"
      shift 2
      ;;
    --azpool-root)
      AZPOOL_ROOT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (try --help)"
      ;;
  esac
done

[[ -n "$SC_NODE_ID" ]] || die "--sc-node-id is required"
[[ -n "$PAYOUT_ADDRESS" ]] || die "--payout-address is required"
[[ -n "$MATCH_VALUE" ]] || die "--match-value is required"

DISPLAY_NAME="${DISPLAY_NAME:-$SC_NODE_ID}"
ADDRESS_LABEL="${ADDRESS_LABEL:-${SC_NODE_ID} primary}"

case "$MATCH_TYPE" in
  prefix|exact|glob) ;;
  *) die "--match-type must be prefix, exact, or glob (got: $MATCH_TYPE)" ;;
esac

# Soft validation only — registry does not prove ownership.
if [[ ! "$PAYOUT_ADDRESS" =~ ^az1[a-z0-9]+$ ]]; then
  die "--payout-address must look like an az1… bech32 address (got: ${PAYOUT_ADDRESS:0:8}…)"
fi

if [[ ! "$SC_NODE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  die "--sc-node-id has invalid characters: $SC_NODE_ID"
fi

if [[ "$MATCH_TYPE" == "prefix" && "$MATCH_VALUE" != *. ]]; then
  echo "WARN: prefix match_value usually ends with '.' (pool identities like PREFIX.miner1)" >&2
fi

if [[ "$MATCH_TYPE" == "exact" ]]; then
  echo "WARN: exact matches the whole user_identity; pool identities usually carry a .minerN suffix," >&2
  echo "      so ${MATCH_VALUE}.miner1 will NOT match and may fall through to a broader prefix owned by another node." >&2
  echo "      Prefer: --match-type prefix --match-value '${MATCH_VALUE%.}.'" >&2
fi

if [[ -z "$AZPOOL_ROOT" ]]; then
  if [[ -f "${DEFAULT_AZPOOL_ROOT}/payouts/migrations/004_sc_node_payout_addresses.sql" ]]; then
    AZPOOL_ROOT="$DEFAULT_AZPOOL_ROOT"
  elif [[ -f "${OPT_AZPOOL_ROOT}/payouts/migrations/004_sc_node_payout_addresses.sql" ]]; then
    AZPOOL_ROOT="$OPT_AZPOOL_ROOT"
  else
    die "could not find azpool checkout (tried ${DEFAULT_AZPOOL_ROOT} and ${OPT_AZPOOL_ROOT}); pass --azpool-root"
  fi
fi

MIG_004="${AZPOOL_ROOT}/payouts/migrations/004_sc_node_payout_addresses.sql"
[[ -f "$MIG_004" ]] || die "missing migration: $MIG_004"

# Admin-readonly prefers /opt (azledger may not read /home under ProtectHome).
ADMIN_AZPOOL_ROOT="$OPT_AZPOOL_ROOT"
if [[ ! -x "${ADMIN_AZPOOL_ROOT}/.venv/bin/python" ]]; then
  ADMIN_AZPOOL_ROOT="$AZPOOL_ROOT"
fi

if [[ "$(id -u)" -ne 0 ]]; then
  die "run as root via: sudo bash $0 ..."
fi

[[ -f "$COLLECTOR_ENV" ]] || die "missing $COLLECTOR_ENV"

# Load collector.env as azledger; never echo DATABASE_URL.
run_psql() {
  # shellcheck disable=SC2016
  sudo -u azledger bash -c '
    set -euo pipefail
    set -a
    source /etc/azcoin-super/pool-ledger/collector.env
    set +a
    if [[ -z "${DATABASE_URL:-}" ]]; then
      echo "ERROR: DATABASE_URL not set after sourcing collector.env" >&2
      exit 1
    fi
    psql "$DATABASE_URL" "$@"
  ' -- "$@"
}

echo "==> Registering SC-node id=${SC_NODE_ID} match=${MATCH_TYPE}:${MATCH_VALUE} activate_address=${ACTIVATE_ADDRESS} payout_enabled_request=${PAYOUT_ENABLED}"

echo "==> Checking applied schema (SC-node registry tables)"
run_psql -v ON_ERROR_STOP=1 -c "
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'sc_nodes',
    'sc_node_identity_mappings',
    'sc_node_payout_addresses',
    'schema_migrations'
  )
ORDER BY tablename;
"

HAS_SC_NODES="$(
  run_psql -Atc "
    SELECT EXISTS (
      SELECT 1 FROM pg_tables
      WHERE schemaname = 'public' AND tablename = 'sc_nodes'
    );
  " | tr -d '[:space:]'
)"

HAS_PAYOUT_ADDR="$(
  run_psql -Atc "
    SELECT EXISTS (
      SELECT 1 FROM pg_tables
      WHERE schemaname = 'public' AND tablename = 'sc_node_payout_addresses'
    );
  " | tr -d '[:space:]'
)"

if [[ "$HAS_SC_NODES" != "t" ]]; then
  die "sc_nodes missing — apply migrations 001–003 first (collector prerequisite)"
fi

if [[ "$HAS_PAYOUT_ADDR" != "t" ]]; then
  echo "==> Applying migration 004_sc_node_payout_addresses.sql"
  run_psql -v ON_ERROR_STOP=1 -f "$MIG_004"
  run_psql -c '\d sc_node_payout_addresses'
else
  echo "==> sc_node_payout_addresses already present; skipping 004"
fi

HAS_SCHEMA_MIG="$(
  run_psql -Atc "
    SELECT EXISTS (
      SELECT 1 FROM pg_tables
      WHERE schemaname = 'public' AND tablename = 'schema_migrations'
    );
  " | tr -d '[:space:]'
)"
if [[ "$HAS_SCHEMA_MIG" == "t" ]]; then
  echo "==> schema_migrations (informational):"
  run_psql -c "SELECT * FROM schema_migrations ORDER BY 1;" || true
fi

echo "==> Upserting sc_nodes / identity mapping / payout address"
# Use psql :'var' quoting — never interpolate raw values into SQL text.
# payout_enabled: set on INSERT; on CONFLICT only bump if --payout-enabled was passed
# (avoids clobbering an enable-payouts flip on re-registration).
ACTIVATE_SQL=''
if [[ "$ACTIVATE_ADDRESS" == "true" ]]; then
  echo "==> Will activate address and set as active default (pending → active)"
  ACTIVATE_SQL=$(cat <<'EOSQL'
UPDATE sc_node_payout_addresses
SET status = 'inactive',
    is_default = false,
    updated_at = now()
WHERE sc_node_id = :'sc_node_id'
  AND is_default = true
  AND status = 'active'
  AND payout_address <> :'payout_address';

UPDATE sc_node_payout_addresses
SET status = 'active',
    is_default = true,
    verified_at = COALESCE(verified_at, now()),
    updated_at = now()
WHERE sc_node_id = :'sc_node_id'
  AND payout_address = :'payout_address';
EOSQL
)
else
  echo "==> Leaving address pending_verification (not default); re-run with --activate-address after ownership verification"
fi

run_psql -v ON_ERROR_STOP=1 \
  -v sc_node_id="$SC_NODE_ID" \
  -v display_name="$DISPLAY_NAME" \
  -v match_type="$MATCH_TYPE" \
  -v match_value="$MATCH_VALUE" \
  -v payout_address="$PAYOUT_ADDRESS" \
  -v address_label="$ADDRESS_LABEL" \
  -v payout_enabled="$PAYOUT_ENABLED" \
  <<SQL
BEGIN;

INSERT INTO sc_nodes (id, display_name, status, payout_enabled)
VALUES (:'sc_node_id', :'display_name', 'active', (:'payout_enabled')::boolean)
ON CONFLICT (id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    status = 'active',
    payout_enabled = CASE
      WHEN (:'payout_enabled')::boolean THEN true
      ELSE sc_nodes.payout_enabled
    END,
    updated_at = now();

INSERT INTO sc_node_identity_mappings (sc_node_id, match_type, match_value, status)
VALUES (:'sc_node_id', :'match_type', :'match_value', 'active')
ON CONFLICT (match_type, match_value) DO UPDATE
SET sc_node_id = EXCLUDED.sc_node_id,
    status = 'active';

INSERT INTO sc_node_payout_addresses (
  sc_node_id,
  payout_address,
  label,
  address_source,
  status,
  is_default
)
VALUES (
  :'sc_node_id',
  :'payout_address',
  :'address_label',
  'manual',
  'pending_verification',
  false
)
ON CONFLICT (payout_address) DO UPDATE
SET sc_node_id = EXCLUDED.sc_node_id,
    label = EXCLUDED.label,
    address_source = EXCLUDED.address_source,
    updated_at = now();

${ACTIVATE_SQL}

COMMIT;
SQL

echo "==> Verification SELECTs"
# psql only interpolates :'var' in stdin/-f input, not in -c strings.
run_psql -v ON_ERROR_STOP=1 -v sc_node_id="$SC_NODE_ID" <<'SQL'
SELECT id, display_name, status, payout_enabled, created_at, updated_at
FROM sc_nodes
WHERE id = :'sc_node_id';

SELECT id, sc_node_id, match_type, match_value, status, created_at
FROM sc_node_identity_mappings
WHERE sc_node_id = :'sc_node_id'
ORDER BY id;

SELECT id, sc_node_id, payout_address, label, address_source, status, is_default, verified_at, created_at, updated_at
FROM sc_node_payout_addresses
WHERE sc_node_id = :'sc_node_id'
ORDER BY id;
SQL

echo "==> Optional read-only admin JSON (no secrets)"
# shellcheck disable=SC2016
sudo -u azledger bash -c '
  set -euo pipefail
  set -a
  source /etc/azcoin-super/pool-ledger/collector.env
  set +a
  ROOT="$1"
  cd "$ROOT"
  PYTHONPATH="$ROOT" .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py sc-nodes
  PYTHONPATH="$ROOT" .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py mappings
  PYTHONPATH="$ROOT" .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-addresses
' -- "$ADMIN_AZPOOL_ROOT" \
  || echo "WARN: admin_readonly failed (venv/deps/ProtectHome); SQL verification above is sufficient."

echo "DONE: registered ${SC_NODE_ID} (${MATCH_TYPE}=${MATCH_VALUE})."
if [[ "$PAYOUT_ENABLED" != "true" ]]; then
  echo "Note: payout_enabled left false/unchanged. Enable separately when credit/payout automation is ready."
fi
if [[ "$ACTIVATE_ADDRESS" != "true" ]]; then
  echo "Note: address left pending_verification. Re-run with --activate-address after ownership verification."
fi
