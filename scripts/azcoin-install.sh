#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AZCoin Core (Solely for The Mining Pool's Back End) Installation Script
# =============================================================================
# Purpose: Install and configure a dedicated AZCoin Core node optimized as a
#          reliable backend for mining pool operations.
#
# Usage: ./azcoin-install.azpool.sh <tarball_path> <dbcache> <maxmempool> <port> <seednode> [enable]
#
# Parameters:
#   <tarball>      : Path to the azcoin-*.tar.gz file
#   <dbcache>      : dbcache size in MiB (recommended 8192-32768)
#   <maxmempool>   : maxmempool size in MiB (recommended 1024-2048)
#   <port>         : P2P listening port (default 19333)
#   <seednode>     : Seednode address for initial bootstrap (e.g. azcoin-seed.example.com)
#   [enable]       : Optional. Pass "enable" to activate automatic external IP cron updater
#
# Key Characteristics:
#   • Full mempool support + ZMQ notifications
#   • Wallet notify script installed and configured
#   • Named wallet 'wallet' automatically created
#   • Secure rpcauth authentication
#   • Automatic external IP detection and updating
#
# Role in the Ecosystem:
#     This node acts as the critical backend for the AZCoin mining pool.
#     It provides block templates, mempool data, and real-time ZMQ events
#     to the pool software while staying fully synced with the AZCoin network.
#
# Recommended Hardware:
#   - Server Quality Hardware
#   - Fast CPU
#   - 64 GB+ RAM
#   - Fast NVMe SSD (blockchain storage + high IOPS)
#   - Good Bandwidth
#
# This script is designed for Debian-based Linux distributions.
# See azcoin.txt (created at the end) for futher documentation.
# =============================================================================

LOG_FILE="/var/log/azcoin-install.azpool.log"
log() {
    echo "$*" | tee -a "$LOG_FILE"
}

log "Starting AZCoin Mining Pool Backend Installation: $(date)"

# ===================== ROOT CHECK =====================
if [[ $EUID -ne 0 ]]; then
    log "Error: Must run as root (use sudo)."
    exit 1
fi

# ===================== REQUIRED PARAMETERS =====================
TAR_FILE="${1:-}"
DBCACHE="${2:-}"
MAXMEMPOOL="${3:-}"
PORT="${4:-}"
SEEDNODE="${5:-}"
ENABLE_CRON="${6:-}"

if [[ -z "$TAR_FILE" ]] || [[ -z "$DBCACHE" ]] || [[ -z "$MAXMEMPOOL" ]] || \
   [[ -z "$PORT" ]] || [[ -z "$SEEDNODE" ]]; then
    log "Error: Missing required parameters."
    echo "Usage: $0 <tarball> <dbcache> <maxmempool> <port> <seednode> [enable]"
    exit 1
fi

if [[ ! -f "$TAR_FILE" ]]; then
    log "Error: Tar file not found: $TAR_FILE"
    exit 1
fi

# Validation
if ! [[ "$DBCACHE" =~ ^[0-9]+$ ]] || [[ "$DBCACHE" -lt 4096 ]]; then
    log "Error: dbcache must be >= 4096"; exit 1
fi
if ! [[ "$MAXMEMPOOL" =~ ^[0-9]+$ ]] || [[ "$MAXMEMPOOL" -lt 256 ]]; then
    log "Error: maxmempool must be >= 256"; exit 1
fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [[ "$PORT" -lt 1024 ]] || [[ "$PORT" -gt 65535 ]]; then
    log "Error: port must be between 1024-65535"; exit 1
fi

log "Parameters → dbcache=${DBCACHE} | maxmempool=${MAXMEMPOOL} | port=${PORT} | seednode=${SEEDNODE} | cron_updater=${ENABLE_CRON:-disabled}"

# ===================== CREATE USER/GROUP =====================
if ! id "azcoin" &>/dev/null; then
    log "Creating system user/group: azcoin/azcoin"
    groupadd --system azcoin
    useradd --system --gid azcoin --create-home --home-dir "/home/azcoin" \
            --shell /usr/sbin/nologin --comment "AZCoin Core daemon" "azcoin"
else
    log "User azcoin already exists."
fi

# ===================== EXTRACT & INSTALL BINARIES =====================
TMP_EXTRACT=$(mktemp -d)
trap 'rm -rf "${TMP_EXTRACT}"' EXIT

log "Extracting tarball..."
tar -xzf "${TAR_FILE}" -C "${TMP_EXTRACT}"

EXTRACTED_DIR=$(find "${TMP_EXTRACT}" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [[ ! -d "${EXTRACTED_DIR}" ]]; then
    log "Error: Could not find extracted directory after tar extraction"
    exit 1
fi

# Check required binaries/scripts
if [[ ! -x "${EXTRACTED_DIR}/bin/azcoind" ]]; then
    log "Error: azcoind not found in ${EXTRACTED_DIR}/bin/ — cannot continue"
    exit 1
fi

if [[ ! -x "${EXTRACTED_DIR}/bin/azcoin-cli" ]]; then
    log "Error: azcoin-cli not found in ${EXTRACTED_DIR}/bin/ — required for this setup"
    exit 1
fi

if [[ ! -f "${EXTRACTED_DIR}/share/rpcauth/rpcauth.py" ]]; then
    log "Error: rpcauth.py not found in ${EXTRACTED_DIR}/share/rpcauth/ — required for secure RPC auth generation"
    exit 1
fi

log "All required files found — installing binaries..."

# Install azcoind
install -m 0755 -o root -g root -D "${EXTRACTED_DIR}/bin/azcoind" /usr/local/bin/azcoind
log "Installed azcoind (root:root, 755)"

# Install azcoin-cli
install -m 0755 -o root -g root -D "${EXTRACTED_DIR}/bin/azcoin-cli" /usr/local/bin/azcoin-cli
log "Installed azcoin-cli (root:root, 755)"

# Install rpcauth.py (utility script)
install -m 0755 -o root -g root -D "${EXTRACTED_DIR}/share/rpcauth/rpcauth.py" /usr/local/bin/rpcauth.py
log "Installed rpcauth.py to /usr/local/bin/ (root:root, 755)"

# ===================== CONFIG =====================
if [[ ! -f /etc/azcoin/azcoin.conf ]]; then
    log "Generating RPC credentials (user: satoshi)..."
    RPCAUTH_OUTPUT=$(python3 /usr/local/bin/rpcauth.py satoshi 2>&1)
    RPCAUTH=$(echo "$RPCAUTH_OUTPUT" | grep -o '^rpcauth=.*')
    RPC_PASSWORD=$(echo "$RPCAUTH_OUTPUT" | tail -n 1 | tr -d '\r\n \t')

    # Safety check — fail if parsing didn't work
    if [[ -z "$RPCAUTH" || -z "$RPC_PASSWORD" ]]; then
        log "ERROR: Failed to parse rpcauth.py output — cannot continue"
        log "Full output from rpcauth.py:"
        log "$RPCAUTH_OUTPUT"
        exit 1
    fi
    log "rpcauth.py generated successfully: $RPCAUTH"

    log "Generating RPC credentials for coinbase (limited access)..."
    COINBASE_RPCAUTH_OUTPUT=$(python3 /usr/local/bin/rpcauth.py coinbase 2>&1)
    COINBASE_RPCAUTH=$(echo "$COINBASE_RPCAUTH_OUTPUT" | grep -o '^rpcauth=.*')
    COINBASE_PASSWORD=$(echo "$COINBASE_RPCAUTH_OUTPUT" | tail -n 1 | tr -d '\r\n \t')

    log "Generating RPC credentials for templar (limited access)..."
    TEMPLAR_RPCAUTH_OUTPUT=$(python3 /usr/local/bin/rpcauth.py templar 2>&1)
    TEMPLAR_RPCAUTH=$(echo "$TEMPLAR_RPCAUTH_OUTPUT" | grep -o '^rpcauth=.*')
    TEMPLAR_PASSWORD=$(echo "$TEMPLAR_RPCAUTH_OUTPUT" | tail -n 1 | tr -d '\r\n \t')

    # Minimal safety check
    if [[ -z "$COINBASE_RPCAUTH" || -z "$COINBASE_PASSWORD" || -z "$TEMPLAR_RPCAUTH" || -z "$TEMPLAR_PASSWORD" ]]; then
        log "ERROR: Failed to generate RPC credentials"; exit 1
    fi

    umask 077 # umask 077 → new files get 0600 (rw-------), dirs 0700 (rwx------)
    echo "${RPC_PASSWORD}" > "/home/azcoin/rpcpassword"
    echo "${COINBASE_PASSWORD}" > "/home/azcoin/coinbase-rpcpassword"
    echo "${TEMPLAR_PASSWORD}" > "/home/azcoin/templar-rpcpassword"
    umask 0022 # Restore standard umask (files 0644, dirs 0755)

    chown azcoin:azcoin "/home/azcoin/rpcpassword" "/home/azcoin/coinbase-rpcpassword" "/home/azcoin/templar-rpcpassword"
    chmod 640 "/home/azcoin/rpcpassword" "/home/azcoin/coinbase-rpcpassword" "/home/azcoin/templar-rpcpassword"
    log "RPC passwords saved (640 perms)"

    log "Creating configuration directory: /etc/azcoin"
    mkdir -p /etc/azcoin
    chown azcoin:azcoin /etc/azcoin
    chmod 755 "/etc/azcoin" # owner rwx, group rx, others none — secure but readable by group if needed
    log "Config dir created with ownership azcoin:azcoin and chmod 755"

    log "Creating azcoin.conf..."
    cat > /etc/azcoin/azcoin.conf << EOF
# AZCoin Configuration (for the Mining Pool Backend)

# Core daemon and RPC server
server=1

# Allow the 'azcoin' group to read the .cookie file (sets permissions to 640)
rpccookieperms=group

# Amount of RAM (in MiB) allocated to the UTXO cache
dbcache=${DBCACHE}

# Maximum memory (in MiB) used for the transaction mempool
maxmempool=${MAXMEMPOOL}

# RPC Authentication (secure hashed password)
${RPCAUTH}

# Restricted user for az-coinbase-updater and frontend (address generation + spent checks)
${COINBASE_RPCAUTH}

# Restricted user for templar (mining pool template provider)
${TEMPLAR_RPCAUTH}

# Will be automatically updated by externalip-updater.sh
externalip=PLACEHOLDER

# P2P listening port. If behind NAT, ensure port forward matches
port=${PORT}

# Used for initial bootstrap and peer discovery
seednode=${SEEDNODE}

# ZMQ Notifications (for Template Provider)
zmqpubhashblock=tcp://127.0.0.1:29332
zmqpubrawtx=tcp://127.0.0.1:29335
zmqpubsequence=tcp://127.0.0.1:29336

# Named wallet that will be auto-loaded
wallet=wallet

# Walletnotify: trigger script on tx events (unconf, conf, sends, RBF)
# Logs to /var/log/azcoin/wallet_events.log
walletnotify=/usr/local/bin/azcoin_wallet_event_append.sh %s %w

# Whitelist to limit 'coinbase' user access (used by az-coinbase-updater.sh)
rpcwhitelist=coinbase: \
    getnewaddress, \
    getaddressinfo, \
    deriveaddresses, \
    getreceivedbyaddress, \
    getblockchaininfo

# Templar whitelist (strictly limited)
rpcwhitelist=templar: \
    getblockchaininfo, \
    getblocktemplate, \
    submitblock, \
    getbesthash, \
    getblockheader
EOF

    chown azcoin:azcoin /etc/azcoin/azcoin.conf
    chmod 600 /etc/azcoin/azcoin.conf
fi

# ===================== WALLET NOTIFY SCRIPT =====================
log "Creating walletnotify script..."
cat > /usr/local/bin/azcoin_wallet_event_append.sh << 'EOF'
#!/usr/bin/env bash
# azcoin_wallet_event_append.sh - Append-only logger for walletnotify

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TXID="$1"
WALLET="$2"

echo "${TIMESTAMP} | ${TXID} | ${WALLET}" >> /var/log/azcoin/wallet_events.log

exit 0
EOF
chmod 755 /usr/local/bin/azcoin_wallet_event_append.sh

# ===================== EXTERNAL IP UPDATER =====================
log "Installing externalip-updater.sh..."
cat > /usr/local/bin/externalip-updater.sh << 'EOF'
#!/bin/bash
# externalip-updater.sh - AZCoin external IP updater

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (or with sudo)" >&2
    exit 1
fi

CONFIG_FILE="/etc/azcoin/azcoin.conf"
LOG_FILE="/var/log/azcoin/externalip-updater.log"
SERVICE_NAME="azcoind"

# ===================== CRON MANAGEMENT =====================
if [[ "$1" == "--enable" ]]; then
    echo "Enabling externalip-updater cron job..."
    crontab -l 2>/dev/null | grep -v "externalip-updater.sh" > /tmp/crontab.tmp 2>/dev/null || true
    echo "0 */6 * * * /usr/local/bin/externalip-updater.sh >> /var/log/azcoin/externalip-updater.log 2>&1" >> /tmp/crontab.tmp
    crontab /tmp/crontab.tmp
    rm -f /tmp/crontab.tmp
    echo "Cron job enabled (runs every 6 hours)"
    exit 0
fi

if [[ "$1" == "--disable" ]]; then
    echo "Disabling externalip-updater cron job..."
    crontab -l 2>/dev/null | grep -v "externalip-updater.sh" > /tmp/crontab.tmp 2>/dev/null || true
    crontab /tmp/crontab.tmp
    rm -f /tmp/crontab.tmp
    echo "Cron job disabled successfully"
    exit 0
fi

# ===================== NORMAL IP CHECK =====================
# List of reliable IPv4 public IP providers (in order of preference)
IP_PROVIDERS=(
    "https://api.ipify.org"
    "https://ifconfig.me"
    "https://icanhazip.com"
    "https://ipecho.net/plain"
    "https://api-ipv4.ip.sb/ip"
    "https://checkip.amazonaws.com"
    "https://ipv4.seeip.org"
    "https://ipv4.icanhazip.com"
    "https://4.ifconfig.co"
    "https://api.ip.sb/ip"
    "https://ip4.me/api"
)

get_public_ip() {
    for provider in "${IP_PROVIDERS[@]}"; do
        IP=$(curl -s -m 10 -4 "$provider" 2>/dev/null | tr -d ' \n')
        if [[ $IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "$IP"
            return 0
        else
            log "Failed to get IP from $provider"
        fi
    done
    echo ""
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Starting external IP check ==="

CURRENT_IP=$(get_public_ip)

if [[ -z "$CURRENT_IP" ]]; then
    log "ERROR: Could not detect public IPv4 address from any provider."
    exit 1
fi

CONFIG_IP=$(grep -E '^externalip=' "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d ' ')

log "Current public IPv4 : $CURRENT_IP"
log "Config externalip   : ${CONFIG_IP:-NONE}"

if [[ "$CURRENT_IP" != "$CONFIG_IP" ]]; then
    log "IP changed. Updating azcoin.conf..."

    # Remove old externalip line and add the new one (no backup)
    sed -i "s/^externalip=.*/externalip=$CURRENT_IP/" "$CONFIG_FILE"

    log "Updated externalip=$CURRENT_IP"

    # Skip restart if we just replaced the "PLACEHOLDER" on first install
    if [[ "$CONFIG_IP" == "PLACEHOLDER" ]]; then
        log "\"PLACEHOLDER\" replaced - skipping azcoind restart on initial setup."
    else
        log "Restarting $SERVICE_NAME..."
        systemctl restart "$SERVICE_NAME"

        if systemctl is-active --quiet "$SERVICE_NAME"; then
            log "SUCCESS: $SERVICE_NAME restarted with new IP."
        else
            log "ERROR: Failed to restart $SERVICE_NAME!"
        fi
    fi
else
    log "No change - public IP matches configuration."
fi

log "=== IP check completed ==="
EOF

chmod 755 /usr/local/bin/externalip-updater.sh

# Always run once
log "Running external IP updater (first run)..."
/usr/local/bin/externalip-updater.sh

# Enable cron only if requested
if [[ "${ENABLE_CRON}" == "enable" ]]; then
    log "Enabling cron job for external IP updater..."
    /usr/local/bin/externalip-updater.sh --enable
else
    log "Cron job for IP updater left disabled"
fi

# ===================== SYMLINK FOR FHS-COMPLIANT LOG LOCATION =====================
LOG_SYMLINK="/var/log/azcoin/debug.log"
mkdir -p /var/log/azcoin
ln -sfn /var/lib/azcoin/debug.log "${LOG_SYMLINK}"
chown -h azcoin:azcoin "${LOG_SYMLINK}"
log "Created FHS log symlink: ${LOG_SYMLINK} → /var/lib/azcoin/debug.log"

# ===================== LOGROTATE =====================
log "Configuring logrotate..."
cat > /etc/logrotate.d/azcoin << EOF
/var/lib/azcoin/debug.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0640 azcoin azcoin
    sharedscripts
    postrotate
        killall -USR1 azcoind 2>/dev/null || true
    endscript
}
EOF

# ===================== SYSTEMD SERVICE (HARDENED + ENHANCED) =====================
log "Installing ultra-hardened systemd service..."
cat > /etc/systemd/system/azcoind.service << EOF
[Unit]
Description=AZCoin Core (Mining Pool Backend)
After=network.target

[Service]
ExecStart=/usr/local/bin/azcoind -conf=/etc/azcoin/azcoin.conf -datadir=/var/lib/azcoin
User=azcoin
Group=azcoin
Type=forking
PIDFile=/var/lib/azcoin/azcoind.pid
Restart=always
RestartSec=60
TimeoutSec=300
LimitNOFILE=65536

# Hardening / sandboxing (AZCoin Core compatible + advanced)
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
NoNewPrivileges=yes
RestrictNamespaces=yes
RestrictRealtime=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
MemoryDenyWriteExecute=yes
ProtectHostname=yes
PrivateUsers=yes
CapabilityBoundingSet=
StateDirectory=azcoin
StateDirectoryMode=0710

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable azcoind.service
log "Systemd service installed (ultra-hardened). Check: systemd-analyze security azcoind"

# ===================== WALLET CREATION =====================
log "Starting azcoind temporarily to create wallet..."
systemctl start azcoind
sleep 2 # Brief wait for RPC socket

log "Waiting for azcoind RPC readiness (up to 300s)..."
READY=false
for i in {1..300}; do
    if sudo -u azcoin azcoin-cli -conf=/etc/azcoin/azcoin.conf -datadir=/var/lib/azcoin getblockchaininfo >/dev/null 2>&1; then
        READY=true
        log "azcoind RPC ready after ${i} seconds."
        break
    fi
    printf "."
    sleep 1
done
echo "" >> "$LOG_FILE"

if ! $READY; then
    log "ERROR: RPC not ready after 5 minutes. Failing install. Check debug.log and hardware/resources."
    systemctl stop azcoind
    exit 1
fi

log "Creating wallet 'wallet' if missing"
WALLET_OUTPUT=$(sudo -u azcoin azcoin-cli -conf=/etc/azcoin/azcoin.conf -datadir=/var/lib/azcoin createwallet wallet 2>&1)
if echo "$WALLET_OUTPUT" | grep -q '"name": "wallet"'; then
    log "Wallet 'wallet' created successfully."
elif echo "$WALLET_OUTPUT" | grep -q "Database already exists"; then
    log "Wallet 'wallet' already exists — skipping."
else
    log "ERROR: Wallet creation failed unexpectedly!"
    log "Output from azcoin-cli:"
    log "$WALLET_OUTPUT"
    log "Failing install. Check azcoin.conf, RPC settings, disk space, or permissions."
    systemctl stop azcoind
    exit 1
fi

log "azcoind is now running (systemctl start azcoind was issued). You can stop/restart as needed."

# ===================== Create/Update Files w/ Correct Owners/Permissions =====================
chmod 640 /var/lib/azcoin/debug.log
touch /var/log/azcoin/wallet_events.log
chown azcoin:azcoin /var/log/azcoin/wallet_events.log
chmod 640 /var/log/azcoin/wallet_events.log

# ===================== Add azc alias if it does not already exist =====================
TARGET="/etc/bash.bashrc"
AZC_ALIAS="alias azc='sudo -u azcoin azcoin-cli -conf=/etc/azcoin/azcoin.conf -datadir=/var/lib/azcoin'"
if ! grep -Fxq "$AZC_ALIAS" "$TARGET"; then
    echo "$AZC_ALIAS" | tee -a "$TARGET" > /dev/null
    log "Added azc alias to $TARGET"
else
    log "azc alias already present"
fi

# ===================== README =====================
log "Creating system-wide documentation README..."
mkdir -p /usr/local/share/doc

cat > /usr/local/share/doc/azcoin.txt << EOF
# AZCoin (Solely for The Mining Pool's Back End) Node

This is a dedicated high-performance AZCoin Core node configured specifically as the backend for a mining pool.

Basic Status Commands:
- azc getblockchaininfo     # Best overall command - shows sync progress
- azc getnetworkinfo        # Network info and connection count
- azc getpeerinfo           # List all connected peers
- azc getmininginfo         # Mining-related information
- azc getwalletinfo         # Wallet status
- azc getbalances           # Wallet balances

Service Management:
- systemctl status azcoind                  # Check if the service is running
- journalctl -u azcoind -f                  # Live tail of systemd logs
- sudo systemctl restart azcoind            # Restart after changing azcoin.conf
- tail -n 100 /var/log/azcoin/debug.log     # View recent debug log

Resource Monitoring:
- free -h                   # RAM usage
- df -h /var/lib/azcoin     # Disk usage

Key Settings:
- dbcache                   # Amount of RAM for UTXO cache in MiB (higher = faster validation)
- externalip                # Automatically managed by updater script
- port                      # P2P listening port (default 19333)
- seednode                  # Seednode address for initial bootstrap
- maxmempool                # Memory allocated for transaction pool in MiB
Note: Restart azcoind after editing azcoin.conf

ZMQ Notifications:
- Raw transactions: tcp://127.0.0.1:29333
- Block hashes:     tcp://127.0.0.1:29334

Wallet Notifications:
- Script: /usr/local/bin/azcoin_wallet_event_append.sh
- Log: /var/log/azcoin/wallet_events.log (640 azcoin:azcoin)

External IP Updater Script:
Automatically checks the current public IPv4 4 times daily
If the IP differs from the one in azcoin.conf, it updates the file and restarts AZCoin Core
- Location:         /usr/local/bin/externalip-updater.sh
- Manually run:     externalip-updater.sh
- Enable cron:      externalip-updater.sh --enable
- Disable cron:     externalip-updater.sh --disable

Key Paths:
- Config file:      /etc/azcoin/azcoin.conf
- Blockchain data:  /var/lib/azcoin
- Logs:             /var/log/azcoin/debug.log (-> /var/lib/azcoin/debug.log)
- RPC Password:     /home/azcoin/rpcpassword (640 azcoin:azcoin)
- .cookie:          /var/lib/azcoin/.cookie (640 azcoin:azcoin)
- Wallet Directory  /var/lib/azcoin/wallet
- IP Updater Log:   /var/log/azcoin/externalip-updater.log
- Service File:     /etc/systemd/system/azcoind.service

Adding users to azcoin group: sudo usermod -aG azcoin <app_user_name>
EOF

log "Setup complete!"
log "azcoin-install.azpool setup log file: ${LOG_FILE}"
log "Readme file: /usr/local/share/doc/azcoin.txt"