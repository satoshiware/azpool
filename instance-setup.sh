#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AZCoin Pool Instance Setup Script
# =============================================================================
# Purpose: Fully install and configure a single AZCoin Pool Instance (SV2 Pool + Stratum V1→V2 Translator).
#
# Includes:
#   • Hardened 'satoshi' user
#   • WireGuard VPN client (hub-and-spoke)
#   • UFW firewall
#   • Static IP support (optional)
#
# Prerequisites (must be in the same directory as this script):
#   • azpool-instance.env          ← Main configuration file (Carefully review/update)
#   • azpool-install.azcoin.sh
#   • translator-install.azcoin.sh
#   • az-coinbase-updater.sh
#
# Key Features:
#   • Automatic download + SHA256 verification of components
#   • Complete environment validation
#   • Secure SSH (FIDO2 support, password auth disabled)
#   • Root login fully disabled
#   • Coinbase updater
#
# Post Setup:
#   • cat /home/satoshi/readme.txt        # General reference
#   • cat /home/satoshi/postsetup.txt     # CRITICAL! Follow this after setup.
# =============================================================================

AZPOOL_BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AZPOOL_BASE_DIR}/azpool-instance.env"
LOG_FILE="/var/log/azpool-instance-setup.log"

log() {
    echo "$*" | tee -a "$LOG_FILE"
}

log "AZCoin Pool Instance Setup Started - $(date '+%Y-%m-%d %H:%M:%S')"

# ===================== ROOT CHECK =====================
if [[ $EUID -ne 0 ]]; then
    log "Error: Must run as root (use sudo)"
    exit 1
fi

# ===================== LOAD ENVIRONMENT =====================
if [[ ! -f "$ENV_FILE" ]]; then
    log "ERROR: Environment file not found: $ENV_FILE"
    exit 1
fi

log "Loading configuration from $ENV_FILE"
set -a
source "$ENV_FILE"
set +a

# Safe env dump (masks secrets)
log "=== Environment File Content (secrets masked) ==="
sed 's/\(AUTHORITY_SECRET_KEY=\).*/\1[REDACTED]/; s/\(AZCOIN_RPC_PASS=\).*/\1[REDACTED]/' "$ENV_FILE" | tee -a "$LOG_FILE"
log "=== End of Environment File ==="

# ===================== SECURE ENVIRONMENT FILE =====================
log "Securing environment file (root:root 600) to protect secrets..."
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
log "azpool-instance.env secured successfully"

# ===================== ENVIRONMENT VALIDATION =====================
log "=== ENVIRONMENT VALIDATION ==="

[[ -n "${INSTANCE_NAME}" ]]             || { log "ERROR: INSTANCE_NAME is missing"; exit 1; }
[[ -n "${POOL_TAR_URL}" ]]              || { log "ERROR: POOL_TAR_URL is missing"; exit 1; }
[[ -n "${TRANSLATOR_TAR_URL}" ]]        || { log "ERROR: TRANSLATOR_TAR_URL is missing"; exit 1; }
[[ -n "${POOL_TAR_SHA256}" ]]           || { log "ERROR: POOL_TAR_SHA256 is missing"; exit 1; }
[[ -n "${TRANSLATOR_TAR_SHA256}" ]]     || { log "ERROR: TRANSLATOR_TAR_SHA256 is missing"; exit 1; }
[[ -n "${TEMPLATE_PROVIDER_ADDR}" ]]    || { log "ERROR: TEMPLATE_PROVIDER_ADDR is missing"; exit 1; }
[[ -n "${TEMPLATE_PROVIDER_PUBKEY}" ]]  || { log "ERROR: TEMPLATE_PROVIDER_PUBKEY is missing"; exit 1; }
[[ -n "${WG_SERVER_ENDPOINT}" ]]        || { log "ERROR: WG_SERVER_ENDPOINT is missing"; exit 1; }
[[ -n "${WG_SERVER_PUBLIC_KEY}" ]]      || { log "ERROR: WG_SERVER_PUBLIC_KEY is missing"; exit 1; }
[[ -n "${SATOSHI_FIDO2_KEY1}" ]]        || { log "ERROR: SATOSHI_FIDO2_KEY1 (primary) is required"; exit 1; }
[[ -n "${AZCOIN_RPC_USER}" ]]           || { log "ERROR: AZCOIN_RPC_USER is missing"; exit 1; }
[[ -n "${AZCOIN_RPC_PASS}" ]]           || { log "ERROR: AZCOIN_RPC_PASS is missing"; exit 1; }

# URL checks
for url_var in POOL_TAR_URL TRANSLATOR_TAR_URL; do
    url="${!url_var}"
    if [[ ! "$url" =~ ^https?:// ]]; then
        log "ERROR: ${url_var} must start with http:// or https://"
        exit 1
    fi
done

# SHA256 checks
for sha_var in POOL_TAR_SHA256 TRANSLATOR_TAR_SHA256; do
    sha="${!sha_var}"
    if [[ ! "$sha" =~ ^[a-f0-9]{64}$ ]]; then
        log "ERROR: ${sha_var} is not a valid 64-character SHA256 hash"
        exit 1
    fi
done

log "Environment validation passed."

# ===================== PYTHON CHECK =====================
log "=== Checking for Python 3 ==="
if ! command -v python3 &> /dev/null; then
    log "ERROR: python3 is required but not found on the system"
    log "Please install it with: apt-get install -y python3"
    exit 1
fi
log "✓ Python 3 detected"

# ===================== SCRIPT PRESENCE VERIFICATION + PERMISSIONS =====================
log "=== Verifying Required Installer Scripts ==="

REQUIRED_SCRIPTS=(
    "azpool-install.azcoin.sh"
    "translator-install.azcoin.sh"
    "az-coinbase-updater.sh"
)

for script in "${REQUIRED_SCRIPTS[@]}"; do
    if [[ ! -f "${AZPOOL_BASE_DIR}/${script}" ]]; then
        log "ERROR: Required script not found: ${script}"
        exit 1
    else
        chmod +x "${AZPOOL_BASE_DIR}/${script}"
        log "✓ Found and made executable: ${script}"
    fi
done

# ===================== DOWNLOAD & VERIFY =====================
download_and_verify() {
    local url=$1
    local dest=$2
    local expected=$3

    log "Downloading $(basename "$url")..."
    curl -L --fail -o "$dest" "$url"

    log "Verifying SHA256 for $(basename "$dest")..."
    local actual=$(sha256sum "$dest" | awk '{print $1}')
    if [[ "$actual" != "$expected" ]]; then
        log "ERROR: Checksum mismatch for $(basename "$dest")!"
        log "Expected: $expected"
        log "Got: $actual"
        rm -f "$dest"
        exit 1
    fi
    log "✓ Verified $(basename "$dest")"
}

log "=== Downloading Components ==="
POOL_TAR_LOCAL="${AZPOOL_BASE_DIR}/pool_sv2.tar.gz"
TRANSLATOR_TAR_LOCAL="${AZPOOL_BASE_DIR}/translator_sv2.tar.gz"

download_and_verify "${POOL_TAR_URL}" "${POOL_TAR_LOCAL}" "${POOL_TAR_SHA256}"
download_and_verify "${TRANSLATOR_TAR_URL}" "${TRANSLATOR_TAR_LOCAL}" "${TRANSLATOR_TAR_SHA256}"

# ===================== SYSTEM UPDATE =====================
log "=== System Update & Base Packages ==="
apt-get update -qq
apt-get install -y curl ufw wireguard wireguard-tools openssh-server jq python-is-python3
apt-get full-upgrade -y
apt-get autoremove -y
apt-get autoclean -y
log "System updated."

# ===================== HOSTNAME =====================
log "=== Setting Hostname ==="
hostnamectl set-hostname "${INSTANCE_NAME}"
echo "${INSTANCE_NAME}" > /etc/hostname
log "Hostname set to: ${INSTANCE_NAME}"

# ===================== STATIC IP (OPTIONAL) =====================
log "=== Configuring Static IP (optional) ==="
if [[ -n "${STATIC_IP:-}" ]]; then
    NETWORK_INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n1)
    if [[ -z "${NETWORK_INTERFACE}" ]]; then
        log "WARNING: Could not detect network interface. Static IP not applied."
    else
        log "Setting static IP ${STATIC_IP} on interface ${NETWORK_INTERFACE}"
        cat > /etc/netplan/01-netcfg.yaml << EOF
network:
  version: 2
  ethernets:
    ${NETWORK_INTERFACE}:
      dhcp4: no
      addresses: [${STATIC_IP}/24]
      gateway4: ${STATIC_GATEWAY}
      nameservers:
        addresses: [${STATIC_DNS}]
EOF
        netplan apply
        log "Static IP configured successfully on ${NETWORK_INTERFACE}."
    fi
else
    log "No static IP configured - using DHCP."
fi

# ===================== SSH HARDENING + SATOSHI USER =====================
log "=== Configuring SSH + Creating satoshi user ==="

if ! id "satoshi" &>/dev/null; then
    useradd -m -s /bin/bash satoshi
    echo "satoshi:satoshi" | chpasswd
    usermod -aG sudo satoshi
    log "User 'satoshi' created with password 'satoshi' and sudo rights."
else
    log "User 'satoshi' already exists."
fi

mkdir -p /home/satoshi/.ssh
chmod 700 /home/satoshi/.ssh
chown satoshi:satoshi /home/satoshi/.ssh

# Add FIDO2 keys with comments for tracking
for i in {1..4}; do
    key_var="SATOSHI_FIDO2_KEY${i}"
    if [[ -n "${!key_var}" ]]; then
        # Extract key (first two fields) and comment (everything after first #)
        KEY_LINE="${!key_var}"
        KEY=$(echo "$KEY_LINE" | awk '{print $1 " " $2}')
        COMMENT=$(echo "$KEY_LINE" | sed -E 's/^[^#]*# *//')

        if [[ -n "$COMMENT" && "$COMMENT" != "$KEY_LINE" ]]; then
            echo "${KEY} ${COMMENT}" >> /home/satoshi/.ssh/authorized_keys
            log "Added FIDO2 key ${i} (${COMMENT}) for satoshi."
        else
            echo "${KEY}" >> /home/satoshi/.ssh/authorized_keys
            log "Added FIDO2 key ${i} for satoshi."
        fi

    fi
done

chmod 600 /home/satoshi/.ssh/authorized_keys
chown satoshi:satoshi /home/satoshi/.ssh/authorized_keys

SSHD_CONFIG="/etc/ssh/sshd_config"
sed -i 's/^PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG" 2>/dev/null || echo "PermitRootLogin no" >> "$SSHD_CONFIG"
sed -i 's/^PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG" 2>/dev/null || echo "PasswordAuthentication no" >> "$SSHD_CONFIG"
sed -i 's/^#PubkeyAuthentication.*/PubkeyAuthentication yes/' "$SSHD_CONFIG"
sed -i 's/^#PubkeyAcceptedAlgorithms.*/PubkeyAcceptedAlgorithms +sk-ecdsa-sha2-nistp256@openssh.com/' "$SSHD_CONFIG" 2>/dev/null || true

systemctl restart ssh
log "SSH hardened (root login fully disabled, password login disabled, FIDO2 supported)."

# ===================== ROOT HARDENING =====================
log "=== Hardening root account ==="
usermod -s /usr/sbin/nologin root
passwd -l root 2>/dev/null || true
log "Root account fully disabled (nologin shell + locked password)."

# ===================== UFW =====================
log "=== Configuring UFW Firewall ==="
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw allow 3336/tcp comment "Translator (V1 miners)"
ufw allow 3337/tcp comment "AZCoin SV2 Pool"
ufw --force enable
log "UFW enabled."

# ===================== WIREGUARD CLIENT =====================
log "=== Setting up WireGuard Client to Backend ==="

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

if [[ ! -f /etc/wireguard/private.key ]]; then
    wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key
    chmod 600 /etc/wireguard/private.key
    log "WireGuard client keys generated."
else
    log "WireGuard client keys already exist."
fi

CLIENT_PUBLIC_KEY=$(cat /etc/wireguard/public.key)

cat > /etc/wireguard/wg0.conf << EOF
[Interface]
PrivateKey = $(cat /etc/wireguard/private.key)
Address = 10.8.0.254/32     # ← TEMPORARY PLACEHOLDER (backend admin will assign real IP)

[Peer]
PublicKey = ${WG_SERVER_PUBLIC_KEY}
AllowedIPs = 10.8.0.1/32   # Only the backend tunnel IP (strict hub-and-spoke)
Endpoint = ${WG_SERVER_ENDPOINT}
PersistentKeepalive = 25
EOF

chmod 600 /etc/wireguard/wg0.conf
systemctl enable --now wg-quick@wg0

log "WireGuard client activated."
log "Client Public Key (share with backend): ${CLIENT_PUBLIC_KEY}"

# ===================== COMPONENT INSTALLATION =====================
log "=== Installing AZCoin SV2 Pool ==="
if [[ -n "${AUTHORITY_SECRET_KEY:-}" ]]; then
    "${AZPOOL_BASE_DIR}/azpool-install.azcoin.sh" \
        "${POOL_TAR_LOCAL}" \
        "${TEMPLATE_PROVIDER_ADDR}" \
        "${TEMPLATE_PROVIDER_PUBKEY}" \
        "${AUTHORITY_SECRET_KEY}"
else
    "${AZPOOL_BASE_DIR}/azpool-install.azcoin.sh" \
        "${POOL_TAR_LOCAL}" \
        "${TEMPLATE_PROVIDER_ADDR}" \
        "${TEMPLATE_PROVIDER_PUBKEY}"
fi

log "=== Installing Translator ==="
"${AZPOOL_BASE_DIR}/translator-install.azcoin.sh" "${TRANSLATOR_TAR_LOCAL}"

# ===================== COINBASE UPDATER =====================
log "=== Installing Coinbase Updater ==="

# Copy script
cp "${AZPOOL_BASE_DIR}/az-coinbase-updater.sh" /usr/local/bin/az-coinbase-updater.sh

# Set very strict permissions (root only)
chmod 700 /usr/local/bin/az-coinbase-updater.sh
chown root:root /usr/local/bin/az-coinbase-updater.sh

log "✓ az-coinbase-updater.sh installed with 700 permissions (root only to protect AZCOIN_RPC_PASS credential)"

# Inject RPC credentials
log "Injecting AZCoin RPC credentials..."
sed -i "s|RPC_USER=.*|RPC_USER=\"${AZCOIN_RPC_USER}\"|" /usr/local/bin/az-coinbase-updater.sh
sed -i "s|RPC_PASS=.*|RPC_PASS=\"${AZCOIN_RPC_PASS}\"|" /usr/local/bin/az-coinbase-updater.sh

log "✓ RPC credentials injected"

# ===================== CRON + LOGROTATE (COINBASE UPDATER) =====================
log "=== Installing Cron & Logrotate for Coinbase Updater ==="

cat > /etc/cron.d/az-coinbase-updater << 'EOF'
# ================================================================
# AZCoin Pool - Coinbase Address Updater
# ================================================================

# ==================== ACTIVE (CURRENT) =====================
# Daily rotation @ 3:15 AM - Use this while restarts are required
15 3 * * * root /usr/local/bin/az-coinbase-updater.sh

# ==================== FUTURE (HOT-SWAP READY) ==============
# Uncomment this (comment the first) when hot-swap / reload SRI SV2 Pool capability is ready
# Every 2 minutes (recommended frequency with hot-swap)
# */10 * * * * root /usr/local/bin/az-coinbase-updater.sh
EOF

chmod 644 /etc/cron.d/az-coinbase-updater
log "✓ Coinbase updater cron installed (/etc/cron.d/az-coinbase-updater)"
log "   → Daily job is ACTIVE"
log "   → 2-minute job is commented (ready for hot-swap)"

# Install logrotate (10MB rotate, 2 backups)
cat > /etc/logrotate.d/az-coinbase-updater << 'EOF'
/var/log/az-coinbase-updater.log {
    size 10M
    rotate 2
    compress
    delaycompress
    missingok
    notifempty
    create 644 root root
}
EOF

chmod 644 /etc/logrotate.d/az-coinbase-updater
log "✓ Logrotate configured (10MB rotate, 2 backups kept)"

# ===================== GENERATE READMES =====================
log "=== Generating readme.txt and postsetup.txt ==="

AUTHORITY_PUBLIC_KEY=$(grep -o 'authority_public_key = "[^"]*"' /etc/azpool/azpool.toml 2>/dev/null | cut -d'"' -f2 || echo "ERROR_KEY_NOT_FOUND")

sudo -u satoshi cat > /home/satoshi/readme.txt << EOF
AZCoin Pool Instance
=======================================
IMPORTANT: Follow the post-install instructions (/home/satoshi/postsetup.txt) before using this pool instance.

Hostname: ${INSTANCE_NAME}

Authority Public Key: ${AUTHORITY_PUBLIC_KEY}
  → Stratum V2 Noise protocol public key.
  → Share this key with all SV2 miners / sc-nodes connecting to this pool.

Coinbase Updater:
  • Updates the coinbase output (payout address) for newly mined blocks to keep things fresh.
  • Current schedule → sudo nano /etc/cron.d/az-coinbase-updater
  • Run manually anytime → sudo az-coinbase-updater.sh

Documentation
─────────────
  • AZPool:           /usr/local/share/doc/azpool.txt
  • Translator:       /usr/local/share/doc/translator.txt

Key Locations
─────────────
  • Pool Config:          /etc/azpool/azpool.toml
  • Translator Config:    /etc/translator/translator.toml
  • WireGuard Config:     /etc/wireguard/wg0.conf
  • Coinbase Updater:     /usr/local/bin/az-coinbase-updater.sh

Logs
────
  • Coinbase Updater:     /var/log/az-coinbase-updater.log
  • Setup:                /var/log/azpool-instance-setup.log

Useful Commands
───────────────
  • systemctl status azpool translator wg-quick@wg0
  • journalctl -u azpool -f
  • journalctl -u translator -f
  • wg show
EOF

sudo -u satoshi cat > /home/satoshi/postsetup.txt << EOF
Post-Setup Checklist (AZCoin Pool Instance)
===========================================
[ ] 1. Authority Key Verification
    Public Key: ${AUTHORITY_PUBLIC_KEY}

    → If this is the FIRST pool instance (key was generated during setup), you can safely ignore this step.
    → If this is NOT the first instance, verify this public key exactly matches the one from the first instance.

[ ] 2. WireGuard Connection (On This Client Machine)
    • Copy THIS machine's WireGuard Public Key (safe to share): cat /etc/wireguard/public.key
    • On the BACKEND server run: sudo manage-wireguard-clients
        → Choose option 1 (Add a new client)
        → Enter a descriptive name (e.g. pool-instance-01)
        → Paste the Public Key above
    • Backend will assign a unique IP (e.g. 10.8.0.5/32). Copy it.
    • Back on THIS machine: sudo nano /etc/wireguard/wg0.conf
        → Update the Address line with the IP from backend
    • Restart WireGuard: sudo systemctl restart wg-quick@wg0
    • Test connectivity: ping -c 3 10.8.0.1 && wg show

[ ] 3. Coinbase Updater
    • Run coinbase updater manually: sudo az-coinbase-updater.sh
    • Verify success and that coinbase_reward_script is now set in /etc/azpool/azpool.toml
    • Enabled/Start Pool Service: sudo systemctl enable --now azpool
    • Restart the machine for good measure: sudo reboot now

[ ] 5. Final Verification from Backend
    • From the backend server, confirm it can reach this instance: ping -c 3 [assigned-wireguard-ip] && wg show
    • Test Pool Status Endpoint (most important check): curl http://[assigned-wireguard-ip]:9097/api/v1/status
        → You should receive a JSON response with pool status information.

Congratulations! This pool instance is fully operational and ready to accept miners.
EOF

log "✅ readme.txt and postsetup.txt generated successfully."

echo ""
echo "======================================================================"
log "=== AZCoin Pool Instance Setup Completed Successfully! ==="
echo "======================================================================"
echo "Setup Log file:       ${LOG_FILE}"
echo "Readme:               /home/satoshi/readme.txt"
echo "Post-Setup Guide:     /home/satoshi/postsetup.txt"
echo "WireGuard Public Key: ${CLIENT_PUBLIC_KEY}"
echo ""
echo "NEXT STEP:"
echo "   Please follow the post-install checklist: cat /home/satoshi/postsetup.txt"
exit 0