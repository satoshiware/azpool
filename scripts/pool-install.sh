#!/usr/bin/env bash
set -euo pipefail
# =====================================================================================
# AZCoin SV2 Pool Installer
#
# Purpose:
#   Installs and configures the Stratum V2 Pool (pool_sv2) for AZCoin.
#
# Features:
#   - Native SV2 listener on port 3337
#   - Automatic keypair generation or derivation (if AUTHORITY_SECRET_KEY is not passed to this script)
#   - Full verbose configuration with detailed comments
#   - Coinbase uses placeholder (updated by az-coinbase-updater.sh)
#
# Usage:
#   ./pool-install.azcoin.sh <POOL_TAR> <TEMPLATE_PROVIDER_ADDR> <TEMPLATE_PROVIDER_PUBKEY>
#   ./pool-install.azcoin.sh <POOL_TAR> <TEMPLATE_PROVIDER_ADDR> <TEMPLATE_PROVIDER_PUBKEY> <AUTHORITY_SECRET_KEY>
#
# Required Arguments:
#   1. POOL_TAR                    → Full path to pool_sv2 tar.gz
#   2. TEMPLATE_PROVIDER_ADDR      → e.g. 127.0.0.1:8442
#   3. TEMPLATE_PROVIDER_PUBKEY    → Authority public key of the Template Provider
#   4. AUTHORITY_SECRET_KEY        → (Optional) Your pool's authority secret key
#
# Important Notes:
#   - This script installs, but does NOT start or enable the azpool service.
#   - sv2-keygen.py must be in the same directory.
# =====================================================================================

LOG_FILE="/var/log/pool-install.log"
log() {
    echo "$*" | tee -a "$LOG_FILE"
}

log "Starting AZCoin SV2 Pool setup [$(date)]"

# ===================== CHECKS =====================
if [[ $EUID -ne 0 ]]; then
    log "Error: Must run as root (sudo)."
    exit 1
fi

SCRIPT_DIR="$(dirname "$(realpath "$0")")"
KEYGEN_SCRIPT="${SCRIPT_DIR}/sv2-keygen.py"

if [[ ! -f "${KEYGEN_SCRIPT}" ]]; then
    log "Error: sv2-keygen.py not found in the same directory."
    exit 1
fi

# ===================== PARAMETER VALIDATION =====================
log "Starting parameter validation..."

if [[ $# -lt 3 || $# -gt 4 ]]; then
    log "Error: Wrong number of arguments"
    log "Usage:"
    log "  $0 <POOL_TAR> <TEMPLATE_PROVIDER_ADDR> <TEMPLATE_PROVIDER_PUBKEY>"
    log "  $0 <POOL_TAR> <TEMPLATE_PROVIDER_ADDR> <TEMPLATE_PROVIDER_PUBKEY> <AUTHORITY_SECRET_KEY>"
    exit 1
fi

POOL_TAR="$1"
TEMPLATE_PROVIDER_ADDR="$2"
TEMPLATE_PROVIDER_PUBKEY="$3"
AUTHORITY_SECRET_KEY="${4:-}"

if [[ ! -f "${POOL_TAR}" ]]; then
    log "Error: POOL_TAR file not found: ${POOL_TAR}"
    exit 1
fi

if [[ -z "${TEMPLATE_PROVIDER_ADDR}" ]]; then
    log "Error: TEMPLATE_PROVIDER_ADDR cannot be empty"
    exit 1
fi

if [[ -z "${TEMPLATE_PROVIDER_PUBKEY}" ]]; then
    log "Error: TEMPLATE_PROVIDER_PUBKEY cannot be empty"
    exit 1
fi

# ===================== KEY HANDLING =====================
log "Handling authority keys..."

if [[ -z "${AUTHORITY_SECRET_KEY}" ]]; then
    log "No secret key provided → Generating new keypair"
    KEY_OUTPUT=$(python3 "${KEYGEN_SCRIPT}" 2>&1)
    if [[ $? -ne 0 ]]; then
        log "Error generating keypair: ${KEY_OUTPUT}"
        exit 1
    fi
    AUTHORITY_PUBLIC_KEY=$(echo "$KEY_OUTPUT" | grep -o 'authority_public_key = "[^"]*"' | cut -d'"' -f2)
    AUTHORITY_SECRET_KEY=$(echo "$KEY_OUTPUT" | grep -o 'authority_secret_key = "[^"]*"' | cut -d'"' -f2)
    log "✅ New keypair generated successfully"
else
    log "Secret key provided → Deriving public key"
    KEY_OUTPUT=$(python3 "${KEYGEN_SCRIPT}" "${AUTHORITY_SECRET_KEY}" 2>&1)
    if [[ $? -ne 0 ]]; then
        log "Error deriving public key: ${KEY_OUTPUT}"
        exit 1
    fi
    AUTHORITY_PUBLIC_KEY=$(echo "$KEY_OUTPUT" | grep -o 'authority_public_key = "[^"]*"' | cut -d'"' -f2)
    log "✅ Public key derived successfully"
fi

log "Authority Public Key  : ${AUTHORITY_PUBLIC_KEY}"
# Secret key is intentionally NOT logged for security

# ===================== CREATE SYSTEM USER =====================
if ! id "azpool" &>/dev/null; then
    log "Creating system user: azpool"
    groupadd --system azpool 2>/dev/null || true
    useradd --system --gid azpool --shell /usr/sbin/nologin --comment "AZCoin SV2 Pool Service" azpool 2>/dev/null || true
else
    log "System user 'azpool' already exists"
fi

# ===================== DIRECTORIES & FILES =====================
mkdir -p /etc/azpool /var/log/azpool
chown -R azpool:azpool /etc/azpool /var/log/azpool

touch /var/log/azpool/azpool.log
chown azpool:azpool /var/log/azpool/azpool.log
chmod 644 /var/log/azpool/azpool.log

# ===================== EXTRACT BINARY =====================
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

log "Extracting pool_sv2 binary..."
tar -xzf "${POOL_TAR}" -C "$TMP_DIR"
find "$TMP_DIR" -type f -name "pool_sv2" -exec cp -v {} "/usr/local/bin/pool_sv2" \;
chown root:root /usr/local/bin/pool_sv2
chmod 755 /usr/local/bin/pool_sv2
log "pool_sv2 binary installed with root:root 755 permissions"

# ===================== CREATE CONFIG =====================
log "Creating configuration files..."

cat > /etc/azpool/azpool.toml << EOF
# AZCoin SV2 Pool Configuration

# === Security / Noise Protocol ===
# These keys are used for the secure Noise protocol handshake with SV2 miners.
# Generate a new keypair using this command: cargo run --release -p keygen --bin keygen
authority_public_key = "${AUTHORITY_PUBLIC_KEY}"
authority_secret_key = "${AUTHORITY_SECRET_KEY}"

# SV2 Noise certificate validity in seconds
# Controls how long the internal certificate used during handshake remains valid.
# 3600 = 1 hour (standard default). Can be increased to 86400 (24h) for lower overhead.
cert_validity_sec = 3600

# === Listener ===
# Address and port where native SV2 miners connect.
# BTC-SV1: 3333, BTC-SV2: 3334, JDP: 3335, AZCOIN-SV1: 3336, AZCOIN-SV2: 3337
listen_address = "0.0.0.0:3337"

# === Coinbase Output ===
# Coinbase payout is ALWAYS constructed by the pool using this descriptor.
# The SV2 Template Provider only supplies the block template (tx set, header data, etc.)
# and has no control over the final reward output.
#
# For AZCoin, you must use a "wpkh(...)" descriptor (with the raw pubkey in hex).
# The SRI SV2 Pool does not recognize bech32 addresses using the "addr(bc1q1...)" descriptor starting with "az1q...".
# Source new coinbase wpkh descriptor: azc getaddressinfo $(azc getnewaddress) | grep pubkey
#
# Changing this value requires a full pool restart.
coinbase_reward_script = "wpkh(FIRST_RUN_PLACEHOLDER)"

# === Pool Identity ===
# Unique identifier for this pool instance.
# Change only if you run multiple pool instances on the same machine.
server_id = 1

# String that appears in the coinbase tag of blocks mined by this pool.
pool_signature = ""

# === Logging ===
# Enable this option to set a predefined log file path.
# When enabled, logs will always be written to this file.
log_file = "/var/log/azpool/azpool.log"

# === Difficulty / Performance ===
# Target average share submission rate per SV2 channel. In essence, it governs the pool's target/difficulty calculations per channel (per miner in non-aggregator mode).
#
# Some channels have multiple targets/difficulties (e.g. translator/proxy [vardiff enabled] between the pool and miner).
# Let's review how this setup works (miner - translator/proxy [vardiff enabled] - pool):
#   Miners submit shares to the translator that meet the target/difficulty it received from upstream (either from the translator/Proxy or pool; in this case, the translator/proxy).
#   When the translator/proxy receives shares from the miner, they are locally registered and then validated against the pool's target/difficulty where they will be either forwarded or silently dropped.
#   Note: The TARGET | DIFFICULTY | SHARES_PER_MINUTE | SHARE_WEIGHT is always HIGHER | EASIER | FASTER | SMALLER (or ALL identical) for a downstream channel compared to its upstream channel.
#         This is enforced in code and ensures no payout value is missed (payouts are always fair).
#   With regards to how the target/difficulty is changed per miner, the translator/proxy updates each miner's target/difficulty every 60 seconds based on its own shares_per_minute configuration. EACH MINER IS ALWAYS LOCALLY CONTROLLED.
#   WARNING: When the downstream shares_per_minute is configured lower (slower) than the configuration of the upstream, problems will ensue. It will work fine, but not work as intended.
#
# Recommended Value = 6.0 (Same as Translator/Proxy)
# However, with aggregator mode disabled on the translator/proxy, the pool will be hit by all miners from each SC Node. In this case, it would smart to decrease it signficantly. Recommended setting is 1.0.
shares_per_minute = 1.0

# How many shares to batch before sending acknowledgment (performance tuning).
share_batch_size = 10

# === Extensions (SV2 Protocol) ===
# supported_extensions: list of extension IDs the pool announces it supports
# required_extensions: list of extension IDs that downstreams MUST support
supported_extensions = [
    0x0000,   # Core protocol
    0x0001,   # Extensions Negotiation
    0x0002,   # Worker-Specific Hashrate Tracking
]
required_extensions = []

# === Monitoring ===
# Enable API endpoints on a given port
monitoring_address = "0.0.0.0:9097"
monitoring_cache_refresh_secs = 15

# === Job Declaration Server (JDS) Settings ===
# JDS is an optional component that lets miners propose their own custom block templates and transaction sets.
# It acts as a middle layer for "custom job declaration" between miners and the pool, enabling more decentralized mining and dual block propagation.
#
# Why JDS is not standalone (coupled w/ this pool):
#   - Verify coinbase outputs, enforce the pool's reward share, validate pool signatures, and correctly attribute shares for payouts.
#   - The pool needs to know about every accepted job for accounting and reward distribution.
# jd_server_address = "0.0.0.0:34264"
# jd_server_enabled = false

# === Template Provider ===
# Connection to the external local Template Provider. Uses standard RPC connection w/ AZCoin Core.
[template_provider_type.Sv2Tp]
address = "${TEMPLATE_PROVIDER}"
public_key = "${TEMPLATE_PROVIDER_PUBKEY}"
EOF

# ===================== CONFIG PERMISSIONS =====================
chown azpool:azpool /etc/azpool/azpool.toml
chmod 600 /etc/azpool/azpool.toml
log "Config file created with 600 permissions (azpool:azpool - secret key protected)"

# ===================== SYSTEMD SERVICE =====================
log "Creating systemd service file..."
cat > /etc/systemd/system/azpool.service << EOF
[Unit]
Description=azpool - AZCoin SV2 Pool
After=network.target

[Service]
Type=simple
User=azpool
Group=azpool
ExecStart=/usr/local/bin/pool_sv2 -c /etc/azpool/azpool.toml
Restart=always
RestartSec=5
StandardOutput=append:/var/log/azpool/azpool-stdout.log
StandardError=append:/var/log/azpool/azpool-stderr.log

ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
RestrictRealtime=yes
EOF

systemctl daemon-reload
log "systemctl daemon-reload completed (service NOT started or enabled)"

# ===================== LOGROTATE =====================
log "Setting up logrotate..."
cat > /etc/logrotate.d/azpool << EOF
/var/log/azpool/azpool.log
/var/log/azpool/azpool-*.log {
    daily
    rotate 14
    size 100M
    copytruncate
    missingok
    notifempty
    compress
    delaycompress
    create 644 azpool azpool
    sharedscripts
    postrotate
        systemctl kill -s USR1 azpool.service --kill-who=main 2>/dev/null || true
    endscript
}
EOF
chmod 644 /etc/logrotate.d/azpool

# ===================== README =====================
log "Creating documentation..."
mkdir -p /usr/local/share/doc

cat > /usr/local/share/doc/azpool.txt << EOF
AZCoin SV2 Pool

System User: azpool

Ports:
  - SV2 Listener: 3337
  - Monitoring: 9097

Key Files
  - Binary:          /usr/local/bin/pool_sv2
  - Service File:    /etc/systemd/system/azpool.service
  - Config:          /etc/azpool/azpool.toml
  - Main Log:        /var/log/azpool/azpool.log          (app log)
  - Stdout Log:      /var/log/azpool/azpool-stdout.log   (systemd stdout)
  - Stderr Log:      /var/log/azpool/azpool-stderr.log   (systemd stderr)
  - Logrotate:       /etc/logrotate.d/azpool

Key Configurations (/etc/azpool/azpool.toml):
  - authority_public_key    Public key used by SV2 miners and SV2 translators for Noise handshake
  - authority_secret_key    Private key used to sign Noise handshake (keep secret!)
  - coinbase_reward_script  wpkh() descriptor controlling where block rewards go
  - shares_per_minute       Target share rate per miner/channel
  - address                 Template Provider RPC address
  - public_key              Template Provider's authority public key for secure connection

Management Commands:
  systemctl status azpool
  systemctl restart azpool
  journalctl -u azpool -f
  tail -f /var/log/azpool/azpool.log
  tail -f /var/log/azpool/azpool-stdout.log
  tail -f /var/log/azpool/azpool-stderr.log

Notes:
  • Make sure authority public/secret keys are the same across all pool instances.
    DON'T FORGET THE LOCAL TRANSLATOR (/etc/translator/translator.toml).
  • After initial installation, run the az-coinbase-updater.sh to set the coinbase_reward_script.
  • The initial installation does NOT start or enable the azpool service.
    After the coinbase_reward_script is updated enable and start azpool:
        systemctl enable azpool     # Enable to automatically start azpool service on boot or restart
        systemctl start azpool      # Start azpool service
EOF

log "AZPool installation finished successfully!"
log "pool-install log: ${LOG_FILE}"
log "Readme: /usr/local/share/doc/azpool.txt"
log "NOTE: Service was NOT started/enabled. Use external script: az-coinbase-updater.sh"