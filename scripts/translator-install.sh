#!/usr/bin/env bash
set -euo pipefail
# =====================================================================================
# AZCoin Translator Installer
#
# Purpose:
#   Installs and configures the Stratum V1 → V2 translator for the local AZCoin mining pool.
#
# Features:
#   - Downstream port: 3336 (V1 miners connect here)
#   - Pulls authority_pubkey from the pool's config
#   - Full logrotate setup with copytruncate
#   - Auto-starts (& enables) the translator service
#
# Usage:
#   ./translator-install.azcoin.sh <TRANSLATOR_TAR>
#
# Required Arguments:
#   1. TRANSLATOR_TAR → Full path to the translator_sv2 tar.gz file
#
# Important Notes:
#   - AZCoin SV2 Pool (azpool) must be installed first
#   - No monitoring API is enabled on the translator
# =====================================================================================

LOG_FILE="/var/log/translator-install.log"
log() {
    echo "$*" | tee -a "$LOG_FILE"
}

log "Starting AZCoin Translator setup [$(date)]"

# ===================== CHECKS =====================
if [[ $EUID -ne 0 ]]; then
    log "Error: Must run as root (sudo)."
    exit 1
fi

# Check that azpool is already installed
if [[ ! -f "/etc/azpool/azpool.toml" ]]; then
    log "Error: AZCoin SV2 Pool (azpool) not found. Please install azpool first."
    log "       Expected config: /etc/azpool/azpool.toml"
    exit 1
fi

# ===================== PARAMETER VALIDATION =====================
log "Starting parameter validation..."

if [[ $# -ne 1 ]]; then
    log "Error: Wrong number of arguments"
    log "Usage: $0 <TRANSLATOR_TAR>"
    exit 1
fi

TRANSLATOR_TAR="$1"

if [[ ! -f "${TRANSLATOR_TAR}" ]]; then
    log "Error: TRANSLATOR_TAR file not found: ${TRANSLATOR_TAR}"
    exit 1
fi

# ===================== EXTRACT UPSTREAM PUBKEY FROM POOL CONFIG =====================
log "Extracting authority public key from azpool config..."

if ! UPSTREAM_AUTHORITY_PUBKEY=$(grep -o 'authority_public_key = "[^"]*"' /etc/azpool/azpool.toml | cut -d'"' -f2); then
    log "Error: Could not read authority_public_key from /etc/azpool/azpool.toml"
    exit 1
fi

if [[ -z "${UPSTREAM_AUTHORITY_PUBKEY}" ]]; then
    log "Error: authority_public_key not found or empty in pool config"
    exit 1
fi

log "Successfully extracted upstream authority public key"

# ===================== CREATE SYSTEM USER =====================
if ! id "translator" &>/dev/null; then
    log "Creating system user: translator"
    groupadd --system translator 2>/dev/null || true
    useradd --system --gid translator \
            --shell /usr/sbin/nologin --comment "Translator Service" translator 2>/dev/null || true
else
    log "System user 'translator' already exists"
fi

# ===================== DIRECTORIES & FILES =====================
mkdir -p /etc/translator /var/log/translator
chown -R translator:translator /etc/translator /var/log/translator

touch /var/log/translator/translator.log
chown translator:translator /var/log/translator/translator.log
chmod 644 /var/log/translator/translator.log

# ===================== EXTRACT BINARY =====================
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

log "Extracting translator_sv2 binary..."
tar -xzf "${TRANSLATOR_TAR}" -C "$TMP_DIR"
find "$TMP_DIR" -type f -name "translator_sv2" -exec cp -v {} "/usr/local/bin/translator_sv2" \;
chown root:root /usr/local/bin/translator_sv2
chmod 755 /usr/local/bin/translator_sv2
log "translator_sv2 binary installed with root:root 755 permissions"

# ===================== CREATE CONFIG =====================
log "Creating configuration files..."

cat > /etc/translator/translator.toml << EOF
# Translator AZCoin Configuration File

# ── Downstream Settings (V1 miners connect here) ──
downstream_address = "0.0.0.0"
downstream_port = 3336

max_supported_version = 2
min_supported_version = 2
downstream_extranonce2_size = 4

# ── Upstream Identity ──
user_identity = "translator"

# ── Channel Mode ──
aggregate_channels = false

# ── Log Output ──
log_file = "/var/log/translator/translator.log"

# ── Protocol Extensions ──
supported_extensions = [
    0x0000, # Core protocol
    0x0001, # Extensions Negotiation
    0x0002, # Worker-Specific Hashrate Tracking
]
required_extensions = []

# ── Difficulty / Vardiff Configuration ──
[downstream_difficulty_config]
min_individual_miner_hashrate = 100_000_000_000.0
shares_per_minute = 6.0
enable_vardiff = true

# ── Job Keepalive ──
job_keepalive_interval_secs = 60

# ── Upstream SV2 Pool Connection (Local AZCoin Pool) ──
[[upstreams]]
address = "127.0.0.1"
port = 3337
authority_pubkey = ${UPSTREAM_AUTHORITY_PUBKEY}
EOF

# Secure permissions
chown translator:translator /etc/translator/translator.toml
chmod 600 /etc/translator/translator.toml
log "Config file created with 600 permissions (translator:translator)"

# ===================== SYSTEMD SERVICE =====================
log "Creating systemd service..."
cat > /etc/systemd/system/translator.service << EOF
[Unit]
Description=translator - AZCoin
After=network.target azpool.service

[Service]
Type=simple
User=translator
Group=translator
ExecStart=/usr/local/bin/translator_sv2 -c /etc/translator/translator.toml
Restart=always
RestartSec=5
StandardOutput=append:/var/log/translator/translator-stdout.log
StandardError=append:/var/log/translator/translator-stderr.log

ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
RestrictRealtime=yes
EOF

systemctl daemon-reload
systemctl enable --now translator 2>/dev/null || true
log "Service enabled and started."

# ===================== LOGROTATE =====================
log "Setting up logrotate..."
cat > /etc/logrotate.d/translator << 'EOF'
/var/log/translator/translator.log
/var/log/translator/translator-*.log {
    daily
    rotate 14
    size 100M
    copytruncate
    missingok
    notifempty
    compress
    delaycompress
    create 644 translator translator
    sharedscripts
    postrotate
        systemctl kill -s USR1 translator.service --kill-who=main 2>/dev/null || true
    endscript
}
EOF
chmod 644 /etc/logrotate.d/translator

# ===================== README =====================
log "Creating documentation..."
mkdir -p /usr/local/share/doc

cat > /usr/local/share/doc/translator.txt << EOF
AZCoin Translator (Stratum V1 → V2) for the local AZCoin pool.

System User: translator

Ports:
  - Downstream (V1 Miners): 3336

Key Files & Directories:
  - Binary:          /usr/local/bin/translator_sv2
  - Config:          /etc/translator/translator.toml
  - Service:         /etc/systemd/system/translator.service
  - Main Log:        /var/log/translator/translator.log          (app log)
  - Stdout Log:      /var/log/translator/translator-stdout.log   (systemd stdout)
  - Stderr Log:      /var/log/translator/translator-stderr.log   (systemd stderr)
  - Logrotate:       /etc/logrotate.d/translator

Key Configurations (/etc/translator/translator.toml):
  - user_identity                   Base username sent to the upstream AZCoin SV2 pool
  - min_individual_miner_hashrate   Minimum expected hashrate per miner (used for vardiff baseline)
  - shares_per_minute               Target share rate per miner (6.0 = ~1 share every 10 seconds)

Management Commands:
  systemctl status translator
  systemctl restart translator
  journalctl -u translator -f
  tail -f /var/log/translator/translator.log
  tail -f /var/log/translator/translator-stdout.log
  tail -f /var/log/translator/translator-stderr.log

Important Note:
  - No monitoring API is enabled on the translator.
  - Observe miner activity and hashrate at the AZCoin Pool level only.
  - Debug translator issues using the log files only.
EOF

log "Setup complete!"
log "translator-install log: ${LOG_FILE}"
log "Readme: /usr/local/share/doc/translator.txt"