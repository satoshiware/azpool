#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AZPool Backend Full Setup Script
# =============================================================================
# Purpose: Fully install and configure the AZPool backend server.
#          Includes AZCoin node, Templar SV2 Template Provider, Payouts Engine,
#          WireGuard VPN, UFW firewall, hardened SSH (FIDO2 keys), and dedicated
#          'satoshi' sudo user.
#
# Prerequisites (must be in the same directory as this script):
#   • azpool-backend.env          ← Main configuration file (Carefully review/update)
#   • azcoin-install.azpool.sh
#   • templar-install.sh
#   • payouts-install.azpool.sh
#
# Key Features:
#   • Complete environment validation and dependency checks
#   • Secure SSH (FIDO2 keys w/ password auth off)
#   • WireGuard VPN server + interactive client manager (manage-wireguard-clients.sh)
#   • Automatic download + SHA256 verification: AZCoin, Templar, & Payouts
#   • root login disabled
#
# Post Setup:
#   • sudo manage-wireguard-clients     # Add/Remove pool instances
#   • cat /home/satoshi/readme.txt      # See documentation
# =============================================================================

AZPOOL_BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AZPOOL_BASE_DIR}/azpool-backend.env"
LOG_FILE="/var/log/azpool-backend-setup.log"

log() {
    echo "$*" | tee -a "$LOG_FILE"
}

log "AZPool Backend Setup Started - $(date '+%Y-%m-%d %H:%M:%S')"

# ===================== ROOT CHECK =====================
if [[ $EUID -ne 0 ]]; then
    log "Error: Must run as root (use sudo)"
    exit 1
fi

# ===================== LOAD ENVIRONMENT =====================
if [[ ! -f "$ENV_FILE" ]]; then
    log "ERROR: Environment file not found: $ENV_FILE"
    log "Please create azpool-backend.env in the same directory."
    exit 1
fi

log "Loading configuration from $ENV_FILE"
set -a
source "$ENV_FILE"
set +a

# Dump env content to log for future reference
log "=== Environment File Content ==="
cat "$ENV_FILE" | tee -a "$LOG_FILE"
log "=== End of Environment File ==="

# ===================== ENVIRONMENT VALIDATION =====================
log "=== Validating Environment Variables ==="

[[ -n "${AZCOIN_TARBALL_URL}" ]]                || { log "ERROR: AZCOIN_TARBALL_URL is missing"; exit 1; }
[[ -n "${AZCOIN_TARBALL_SHA256}" ]]             || { log "ERROR: AZCOIN_TARBALL_SHA256 is missing"; exit 1; }
[[ -n "${TEMPLATE_PROVIDER_TARBALL_URL}" ]]     || { log "ERROR: TEMPLATE_PROVIDER_TARBALL_URL is missing"; exit 1; }
[[ -n "${TEMPLATE_PROVIDER_TARBALL_SHA256}" ]]  || { log "ERROR: TEMPLATE_PROVIDER_TARBALL_SHA256 is missing"; exit 1; }
[[ -n "${PAYOUT_ENGINE_TARBALL_URL}" ]]         || { log "ERROR: PAYOUT_ENGINE_TARBALL_URL is missing"; exit 1; }
[[ -n "${PAYOUT_ENGINE_TARBALL_SHA256}" ]]      || { log "ERROR: PAYOUT_ENGINE_TARBALL_SHA256 is missing"; exit 1; }
[[ -n "${WG_PORT}" ]]                           || { log "ERROR: WG_PORT is missing"; exit 1; }
[[ -n "${AZCOIN_P2P_PORT}" ]]                   || { log "ERROR: AZCOIN_P2P_PORT is missing"; exit 1; }
[[ -n "${AZCOIN_IP_UPDATER_CRON_ENABLE}" ]]     || { log "ERROR: AZCOIN_IP_UPDATER_CRON_ENABLE is missing"; exit 1; }
[[ -n "${AZCOIN_SEEDNODE_DNS}" ]]               || { log "ERROR: AZCOIN_SEEDNODE_DNS is missing"; exit 1; }
[[ -n "${SATOSHI_FIDO2_KEY1}" ]]                || { log "ERROR: SATOSHI_FIDO2_KEY1 (primary) is required"; exit 1; }
[[ -n "${AZPOOL_HOSTNAME}" ]]                   || { log "ERROR: AZPOOL_HOSTNAME is required"; exit 1; }

# Number checks
[[ "${AZCOIN_DBCACHE}" =~ ^[0-9]+$ ]]           || { log "ERROR: AZCOIN_DBCACHE must be a number"; exit 1; }
[[ "${AZCOIN_MAXMEMPOOL}" =~ ^[0-9]+$ ]]        || { log "ERROR: AZCOIN_MAXMEMPOOL must be a number"; exit 1; }
[[ "${AZCOIN_P2P_PORT}" =~ ^[0-9]+$ ]]          || { log "ERROR: AZCOIN_P2P_PORT must be a number"; exit 1; }
[[ "${TEMPLATE_PROVIDER_PORT}" =~ ^[0-9]+$ ]]   || { log "ERROR: TEMPLATE_PROVIDER_PORT must be a number"; exit 1; }
[[ "${WG_PORT}" =~ ^[0-9]+$ ]]                  || { log "ERROR: WG_PORT must be a number"; exit 1; }

# URL checks
for url_var in AZCOIN_TARBALL_URL TEMPLATE_PROVIDER_TARBALL_URL PAYOUT_ENGINE_TARBALL_URL; do
    url="${!url_var}"
    if [[ ! "$url" =~ ^https?:// ]]; then
        log "ERROR: ${url_var} must start with http:// or https://"
        exit 1
    fi
done

# SHA256 checks
for sha_var in AZCOIN_TARBALL_SHA256 TEMPLATE_PROVIDER_TARBALL_SHA256 PAYOUT_ENGINE_TARBALL_SHA256; do
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
    "azcoin-install.azpool.sh"
    "templar-install.sh"
    "payouts-install.azpool.sh"
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

log "All required scripts verified and made executable."

# ===================== DOWNLOAD & VERIFY =====================
download_and_verify() {
    local url=$1
    local dest=$2
    local expected=$3
    local file=$(basename "$url")

    log "Downloading ${file}..."
    curl -L --fail -o "$dest" "$url"

    log "Verifying SHA256 for ${file}..."
    local actual=$(sha256sum "$dest" | awk '{print $1}')
    if [[ "$actual" != "$expected" ]]; then
        log "ERROR: Checksum mismatch for ${file}!"
        log "Expected: $expected"
        log "Got: $actual"
        rm -f "$dest"
        exit 1
    fi
    log "✓ Verified ${file}"
}

mkdir -p "${AZPOOL_BASE_DIR}"
cd "${AZPOOL_BASE_DIR}" || { log "ERROR: Could not cd into ${AZPOOL_BASE_DIR}"; exit 1; }

log "=== Downloading Components ==="
download_and_verify "$AZCOIN_TARBALL_URL" "azcoin.tar" "$AZCOIN_TARBALL_SHA256"
download_and_verify "$TEMPLATE_PROVIDER_TARBALL_URL" "template-provider.tar" "$TEMPLATE_PROVIDER_TARBALL_SHA256"
download_and_verify "$PAYOUT_ENGINE_TARBALL_URL" "payout-engine.tar" "$PAYOUT_ENGINE_TARBALL_SHA256"

# ===================== SYSTEM UPDATE =====================
log "=== System Update & Base Packages ==="
apt-get update -qq
apt-get install -y curl ufw wireguard wireguard-tools openssh-server python-is-python3
apt-get full-upgrade -y
apt-get autoremove -y
apt-get autoclean -y
log "System updated."

# ===================== HOSTNAME =====================
log "=== Setting Hostname ==="
hostnamectl set-hostname "${AZPOOL_HOSTNAME}"
echo "${AZPOOL_HOSTNAME}" > /etc/hostname
log "Hostname set to: ${AZPOOL_HOSTNAME}"

# ===================== STATIC IP (OPTIONAL) =====================
log "=== Configuring Static IP (optional) ==="
if [[ -n "${STATIC_IP}" ]]; then
    NETWORK_INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n1)
    if [[ -z "${NETWORK_INTERFACE}" ]]; then
        log "WARNING: Could not detect network interface. Static IP not applied."
    else
        log "Setting static IP ${STATIC_IP} on detected interface ${NETWORK_INTERFACE}"
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
ufw allow "${WG_PORT}/udp" comment "WireGuard"
ufw allow "${AZCOIN_P2P_PORT}/tcp" comment "AZCoin P2P"
ufw allow "${TEMPLATE_PROVIDER_PORT}/tcp" comment "Template Provider"
ufw --force enable
log "UFW enabled (SSH + AZCoin P2P + WireGuard + Template Provider)."

# ===================== WIREGUARD =====================
log "=== Setting up WireGuard Server ==="

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

if [[ ! -f /etc/wireguard/private.key ]]; then
    wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key
    chmod 600 /etc/wireguard/private.key
    log "WireGuard server keys generated."
else
    log "WireGuard server keys already exist."
fi

SERVER_PRIVATE_KEY=$(cat /etc/wireguard/private.key)
SERVER_PUBLIC_KEY=$(cat /etc/wireguard/public.key)

cat > /etc/wireguard/wg0.conf << EOF
[Interface]
PrivateKey = ${SERVER_PRIVATE_KEY}
Address = 10.8.0.1/24
ListenPort = ${WG_PORT}
SaveConfig = true

PostUp = ufw route allow in on wg0
EOF

chmod 600 /etc/wireguard/wg0.conf

sysctl -w net.ipv4.ip_forward=1
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

systemctl enable --now wg-quick@wg0
log "WireGuard server (wg0) activated."
log "Server Public Key (share this with clients): ${SERVER_PUBLIC_KEY}"

# ===================== WIREGUARD CLIENT HELPER =====================
log "=== Installing WireGuard Client Manager ==="
cat > /usr/local/bin/manage-wireguard-clients.sh << 'WIREGUARD_HELPER'
#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Error: Must run as root (use sudo)"
    exit 1
fi

WG_PORT=$(grep -E '^ListenPort =' /etc/wireguard/wg0.conf | awk '{print $3}' || echo "51820")

echo "=== WireGuard Client Manager ==="

while true; do
    echo ""
    echo "1) Add a new client (client generates its own keys)"
    echo "2) Remove a client"
    echo "3) List current clients"
    echo "4) Exit"
    read -p "Choose an option (1-4): " choice

    case $choice in
        1)
            echo ""
            echo "Suggested names:"
            echo "   sc-node-manager"
            echo "   pool-instance-01"
            echo "   pool-instance-02"
            echo ""
            read -p "Enter client name: " CLIENT_NAME
            [[ -z "$CLIENT_NAME" ]] && { echo "No name entered."; continue; }

            echo ""
            echo "On the CLIENT machine, run this command to generate keys:"
            echo "   wg genkey | tee client-private.key | wg pubkey > client-public.key"
            echo ""
            read -p "Paste the CLIENT PUBLIC KEY here: " CLIENT_PUBLIC
            [[ -z "$CLIENT_PUBLIC" ]] && { echo "No public key entered."; continue; }

            LAST_IP=$(grep -o '10.8.0.[0-9]*' /etc/wireguard/wg0.conf | tail -n1 | cut -d. -f4 || echo "1")
            NEXT_IP=$((LAST_IP + 1))
            CLIENT_IP="10.8.0.${NEXT_IP}"

            PUBLIC_IP=$(curl -s https://api.ipify.org 2>/dev/null || \
                        curl -s https://ifconfig.me 2>/dev/null || \
                        curl -s https://api.myip.com | grep -o '[0-9.]*' 2>/dev/null || \
                        curl -s https://ipinfo.io/ip 2>/dev/null || \
                        curl -s https://icanhazip.com 2>/dev/null || \
                        curl -s https://checkip.amazonaws.com 2>/dev/null || \
                        echo "YOUR_PUBLIC_IP")

            cat >> /etc/wireguard/wg0.conf << EOF

[Peer]
# ${CLIENT_NAME}
PublicKey = ${CLIENT_PUBLIC}
AllowedIPs = ${CLIENT_IP}/32
EOF

            systemctl restart wg-quick@wg0

            echo ""
            echo "=== Client Configuration for ${CLIENT_NAME} ==="
            cat << EOF
Address = ${CLIENT_IP}/24
Endpoint = ${PUBLIC_IP}:${WG_PORT}

────────────────────────────────────
Server Tunnel IP (for reaching services like AZCoin RPC): 10.8.0.1
Server Public Key: $(cat /etc/wireguard/public.key)
EOF
            ;;

        2)
            echo ""
            echo "Current clients:"
            grep -E "^# " /etc/wireguard/wg0.conf || echo "No clients found."
            echo ""
            read -p "Enter client name to remove: " CLIENT_NAME
            [[ -z "$CLIENT_NAME" ]] && continue

            sed -i "/# ${CLIENT_NAME}/,/^$/d" /etc/wireguard/wg0.conf
            systemctl restart wg-quick@wg0
            echo "Client ${CLIENT_NAME} removed."
            ;;

        3)
            echo ""
            echo "Current clients:"
            grep -E "^# " /etc/wireguard/wg0.conf || echo "No clients found."
            ;;

        4)
            echo "Exiting."
            exit 0
            ;;

        *)
            echo "Invalid option."
            ;;
    esac
done
WIREGUARD_HELPER

chmod +x /usr/local/bin/manage-wireguard-clients.sh
log "WireGuard client manager installed to /usr/local/bin/manage-wireguard-clients.sh"

# ===================== COMPONENT INSTALLATION =====================
log "=== Installing AZCoin Node ==="
./azcoin-install.azpool.sh \
    "${AZPOOL_BASE_DIR}/azcoin.tar" \
    "${AZCOIN_DBCACHE}" \
    "${AZCOIN_MAXMEMPOOL}" \
    "${AZCOIN_P2P_PORT}" \
    "${AZCOIN_SEEDNODE_DNS}" \
    "${AZCOIN_IP_UPDATER_CRON_ENABLE}"

log "=== Installing Templar (SV2 Template Provider) ==="
./templar-install.sh "${AZPOOL_BASE_DIR}/template-provider.tar" "${TEMPLATE_PROVIDER_PORT}"

log "=== Installing Payouts Engine ==="
./payouts-install.azpool.sh "${AZPOOL_BASE_DIR}/payout-engine.tar"

# ===================== GENERATE README =====================
log "=== Generating README (/home/satoshi/readme.txt) ==="
sudo -u satoshi cat > /home/satoshi/readme.txt << 'EOF'
AZPool Backend - Management Guide
=================================

Additional component documentation is available in:
/usr/local/share/doc/
  - azcoin.txt
  - templar.txt
  - payouts.txt

WireGuard VPN Client Management
-------------------------------
sudo manage-wireguard-clients

Options:
- 1) Add a new client (client generates its own keys)
- 2) Remove a client
- 3) List current clients

After adding a client, update the client config as needed with the information displayed (e.g. Address, Endpoint, Server Public Key, etc.).
Server Tunnel IP (for internal services like AZCoin RPC): 10.8.0.1
See the WireGuard config for more details.

Useful Commands:
-------------
- Check WireGuard status:   systemctl status wg-quick@wg0
- Live WireGuard logs:      journalctl -u wg-quick@wg0 -f
- List Open Ports:          sudo ufw status # SSH, AZCoin P2P, and WireGuard

Key Locations
-------------
- WireGuard config:         /etc/wireguard/wg0.conf
- FIDO2 / SSH Keys:         /home/satoshi/.ssh/authorized_keys (sudo systemctl restart ssh # after updating)
- Setup log:                /var/log/azpool-backend-setup.log
EOF

log "=== AZPool Backend Setup Completed Successfully! ==="

echo ""
echo "Log file: ${LOG_FILE}"
echo "README created at: ${AZPOOL_BASE_DIR}/README.txt"

exit 0