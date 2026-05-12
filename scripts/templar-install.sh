#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AZCoin Template Provider Installer
# =============================================================================
# Purpose: Installs the azcoin-template-provider (SV2 Template Provider)
#
# Requirements:
#   - Must be run as root
#   - azcoin core must be installed first (for RPC password file)
#   - sv2-keygen.py must be in the same directory as this script
#   - The path to the azcoin-template-provider tarball MUST be passed as the first argument
#
# Usage:
#   sudo ./install-azcoin-template-provider.sh /path/to/azcoin-template-provider-*.tar.gz
#
# What this script does:
#   - Creates templar system user
#   - Installs the azcoin-template-provider binary to /usr/local/bin
#   - Creates secure config with auto-loaded RPC password and fresh Noise keys
#   - Sets up systemd service + logrotate
#   - Starts the service automatically
# =============================================================================

LOG_FILE="/var/log/azcoin-template-provider-install.log"

log() {
    echo "$*" | tee -a "$LOG_FILE"
}

log "=== Starting AZCoin Template Provider installation: $(date) ==="

# ===================== CHECKS =====================
if [[ $EUID -ne 0 ]]; then
    log "Error: Must run as root (use sudo)."
    exit 1
fi

if [[ $# -eq 0 ]]; then
    log "Error: Tarball path must be provided as first argument."
    log "Usage: $0 /path/to/azcoin-template-provider-*.tar.gz"
    exit 1
fi

TAR_FILE="$1"
if [[ ! -f "${TAR_FILE}" ]]; then
    log "Error: Tarball not found at ${TAR_FILE}"
    exit 1
fi

log "Using pre-checked tarball: ${TAR_FILE}"

# ===================== SV2 KEYGEN SCRIPT CHECK =====================
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
KEYGEN_SCRIPT="${SCRIPT_DIR}/sv2-keygen.py"

if [[ ! -f "$KEYGEN_SCRIPT" ]]; then
    log "ERROR: sv2-keygen.py not found in the same directory as this installer."
    log "       Please place sv2-keygen.py next to the install script."
    exit 1
fi

chmod +x "$KEYGEN_SCRIPT" 2>/dev/null || true
log "✓ Found and prepared sv2-keygen.py"

# ===================== USER & DIRECTORIES =====================
log "Creating system user and directories..."
if ! id "templar" &>/dev/null; then
    groupadd --system "templar" 2>/dev/null || true
    useradd --system --gid "templar" --shell /usr/sbin/nologin --comment "AZCoin Template Provider" "templar"
else
    log "User templar already exists."
fi

mkdir -p "/var/lib/templar" "/var/log/templar"
chown -R "templar:templar" "/var/lib/templar" "/var/log/templar"
chmod 0750 "/var/lib/templar"

# ===================== EXTRACT & INSTALL BINARY =====================
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

log "Extracting tarball..."
tar -xzf "${TAR_FILE}" -C "$TMP_DIR"

BINARY_PATH=$(find "$TMP_DIR" -type f -name "azcoin-template-provider" -executable | head -n 1)
if [[ -z "$BINARY_PATH" ]]; then
    log "Error: Could not find azcoin-template-provider binary in the tarball."
    exit 1
fi

log "Installing binary..."
install -m 0755 -o root -g root "$BINARY_PATH" "/usr/local/bin/azcoin-template-provider"

# ===================== CONFIG =====================
if [[ ! -f "/etc/templar/azcoin-template-provider.toml" ]]; then
    log "Creating default configuration..."
    mkdir -p "/etc/templar"

    # NOTE: azcoin must be installed first. The RPC password is automatically read.
    PASSWORD_FILE="/home/azcoin/templar-rpcpassword"
    if [[ ! -f "$PASSWORD_FILE" ]]; then
        log "ERROR: azcoin must be installed first!"
        log "       Expected password file not found: ${PASSWORD_FILE}"
        exit 1
    fi

    RPC_PASSWORD=$(cat "$PASSWORD_FILE" | tr -d '\n\r')
    log "✓ Loaded RPC password from ${PASSWORD_FILE}"

    # Generate fresh Noise keys
    log "Generating fresh Noise authority keys..."
    KEY_OUTPUT=$("$KEYGEN_SCRIPT")
    AUTH_PUBLIC=$(echo "$KEY_OUTPUT" | grep -o 'authority_public_key = "[^"]*"' | cut -d'"' -f2)
    AUTH_SECRET=$(echo "$KEY_OUTPUT" | grep -o 'authority_secret_key = "[^"]*"' | cut -d'"' -f2)

    cat > "/etc/templar/azcoin-template-provider.toml" << EOF
# azcoin-template-provider configuration

# RPC Connection (azcoind)
# JSON-RPC endpoint and credentials for connecting to azcoind.
rpc_url = "http://127.0.0.1:19332"
rpc_user = "templar"
rpc_password = "${RPC_PASSWORD}"

# ZMQ Notifications (Primary and required)
# Main mechanism for fast detection of new blocks and mempool changes.
zmq_endpoint_hashblock = "tcp://127.0.0.1:29332"
zmq_endpoint_rawtx     = "tcp://127.0.0.1:29335"
zmq_endpoint_sequence  = "tcp://127.0.0.1:29336"

# Polling (Safety Fallback)
# Periodic RPC polling as a safety fallback in case ZMQ fails.
poll_interval_ms = 10000

# Template Update Threshold
# Minimum fee increase threshold (in ZATS) before sending a new template to pools.
fee_threshold = 5000

# SV2 Template Provider
# Address where mining pools connect to this template provider.
tp_listen_address = "0.0.0.0:19442"

# Noise Protocol Authority Keypair
# Keys used for secure SV2 handshake (freshly generated by sv2-keygen.py).
authority_public_key = "${AUTH_PUBLIC}"
authority_secret_key = "${AUTH_SECRET}"

# Logging
log_file = "/var/log/templar/templar.log"
EOF

    chown templar:templar "/etc/templar/azcoin-template-provider.toml"
    chmod 600 "/etc/templar/azcoin-template-provider.toml"

    log "✅ Secure config created with fresh Noise keys (600, templar:templar)"
else
    log "Config already exists — skipping."
fi

# ===================== SYSTEMD SERVICE =====================
log "Installing systemd service..."
cat > "/etc/systemd/system/templar.service" << EOF
[Unit]
Description=AZCoin Template Provider (SV2)
After=network.target azcoind.service
Wants=azcoind.service

[Service]
Type=simple
User=templar
Group=templar
ExecStart=/usr/local/bin/azcoin-template-provider --config /etc/templar/azcoin-template-provider.toml
Restart=always
RestartSec=5
WorkingDirectory=/var/lib/templar

# Security hardening
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
RestrictRealtime=yes
ReadWritePaths=/var/lib/templar /var/log/templar

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable templar.service 2>/dev/null || true
systemctl start templar.service

# ===================== LOGROTATE =====================
log "Setting up logrotate..."
cat > "/etc/logrotate.d/templar" << EOF
/var/log/templar/templar.log {
    daily
    rotate 14
    size 100M
    copytruncate
    missingok
    notifempty
    compress
    delaycompress
    create 644 templar templar
    sharedscripts
    postrotate
        systemctl kill -s USR1 templar.service --kill-who=main 2>/dev/null || true
    endscript
}
EOF

# ===================== README =====================
log "Creating README file..."
cat > "/usr/local/share/doc/templar.txt" << EOF
AZCoin Template Provider
========================

Key Locations
-------------
- Binary:          /usr/local/bin/azcoin-template-provider (root:root 755)
- Config:          /etc/templar/azcoin-template-provider.toml (templar:templar 600)
- Data directory:  /var/lib/templar (templar:templar 750)
- Log file:        /var/log/templar/templar.log (templar:templar 644)
- Service:         /etc/systemd/system/templar.service (root:root 644)

Key Configuration Options
-------------------------
- fee_threshold = 5000                    → Minimum fee increase (ZATS) before sending new template
                                          Lower = more responsive | Higher = less bandwidth
- poll_interval_ms = 10000                → Polling frequency as safety fallback (in milliseconds)
                                          10000 ms (10 seconds) is the recommended default

Useful Commands
---------------
- Check status:    sudo systemctl status templar.service
- View logs:       sudo journalctl -u templar.service -f
- Tail log file:   tail -f /var/log/templar/templar.log
- Restart service: sudo systemctl restart templar.service
EOF

log "Installation complete! Service started and enabled."
log "Readme: /usr/local/share/doc/templar.txt"