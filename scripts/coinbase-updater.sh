#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AZCoin Coinbase Updater
# =============================================================================
# Purpose: Automatically updates the coinbase_reward_script in azpool.toml
# with a fresh wpkh() descriptor (coinbase address) from the AZCoin node.
#
# This script is run periodically via cron. It only performs an update when necessary:
# 1. First run (FIRST_RUN_PLACEHOLDER detected)
# 2. Current coinbase address has received funds
#
# It connects remotely to the AZCoin node on the backend server via WireGuard
# (10.8.0.1:19332) using the restricted 'coinbase' RPC user.
#
# Behavior:
# - Only generates a new address when an update is actually needed
# - Updates the config and signals azpool to reload only on change
# - Safe to run frequently (idempotent when no change is required)
#
# Log file: /var/log/az-coinbase-updater.log
# =============================================================================

CONFIG_FILE="/etc/azpool/azpool.toml"
LOG_FILE="/var/log/az-coinbase-updater.log"
RPC_URL="http://10.8.0.1:19332"
RPC_USER="PLACEHOLDER_RPC_USER"
RPC_PASS="PLACEHOLDER_RPC_PASS"

log() {
    echo "$*" | tee -a "$LOG_FILE"
}

# RPC helper - Supports multiple parameters
rpc() {
    local method="$1"
    shift

    local params="[]"
    if [[ $# -gt 0 ]]; then
        params="["
        for arg in "$@"; do
            params+="${arg},"
        done
        params="${params%,}]"
    fi

    curl -s --user "${RPC_USER}:${RPC_PASS}" --data-binary \
        "{\"jsonrpc\": \"1.0\", \"id\": \"coinbase\", \"method\": \"${method}\", \"params\": ${params}}" \
        "$RPC_URL" | jq -r '.result // .error.message // "ERROR"'
}

log "=== Starting Coinbase Updater - $(date '+%Y-%m-%d %H:%M:%S') ==="

# Check credentials were injected
if [[ "$RPC_USER" == "PLACEHOLDER_RPC_USER" ]] || [[ "$RPC_PASS" == "PLACEHOLDER_RPC_PASS" ]]; then
    log "ERROR: RPC credentials were not injected during installation"
    exit 1
fi

# Get current descriptor
CURRENT_DESCRIPTOR=$(grep -o 'coinbase_reward_script = "[^"]*"' "$CONFIG_FILE" 2>/dev/null | cut -d'"' -f2 || echo "")

UPDATE_DESCRIPTOR=false
# Condition 1: First run or placeholder
if [[ -z "$CURRENT_DESCRIPTOR" ]] || [[ "$CURRENT_DESCRIPTOR" == *"FIRST_RUN_PLACEHOLDER"* ]]; then
    log "First run or placeholder detected → will update"
    UPDATE_DESCRIPTOR=true
fi

# Condition 2: Check if current address has received funds
if [[ "$UPDATE_DESCRIPTOR" == false ]] && [[ -n "$CURRENT_DESCRIPTOR" ]]; then
    log "Checking if current coinbase address has received funds..."

    CURRENT_PUBKEY=$(echo "$CURRENT_DESCRIPTOR" | sed -E 's/wpkh\((.*)\)/\1/')
    CURRENT_ADDRESS_JSON=$(rpc deriveaddresses "\"wpkh(${CURRENT_PUBKEY})\"")
    if [[ -z "$CURRENT_ADDRESS_JSON" ]] || [[ "$CURRENT_ADDRESS_JSON" == *error* ]]; then
        log "ERROR: Failed to derive address from pubkey: $CURRENT_PUBKEY"
        log "Raw response: $CURRENT_ADDRESS_JSON"
        exit 1
    fi

    CURRENT_ADDRESS=$(echo "$CURRENT_ADDRESS_JSON" | jq -r '.[0] // empty')
    if [[ -n "$CURRENT_ADDRESS" ]]; then
        RECEIVED=$(rpc getreceivedbyaddress "\"$CURRENT_ADDRESS\"" 0 true)

        if [[ -z "$RECEIVED" ]] || [[ "$RECEIVED" == *error* ]]; then
            log "ERROR: getreceivedbyaddress RPC failed"
            log "Raw response: $RECEIVED"
            exit 1
        fi

        if [[ "$RECEIVED" != "0" ]]; then
            log "Current address has received funds (${RECEIVED} BTC) → will rotate to new address"
            UPDATE_DESCRIPTOR=true
        else
            log "Current address has no received funds yet → no update needed"
        fi
    else
        log "ERROR: deriveaddresses returned no address for pubkey"
        exit 1
    fi
fi

# Continue?
if [[ "$UPDATE_DESCRIPTOR" == false ]]; then
    log "No update required at this time"
    exit 0
fi

# === Perform Update ===
log "Generating new address from AZCoin node..."

NEW_ADDRESS=$(rpc getnewaddress)
if [[ -z "$NEW_ADDRESS" ]] || [[ "$NEW_ADDRESS" == *error* ]]; then
    log "ERROR: Failed to generate new address"
    log "Raw response: $NEW_ADDRESS"
    exit 1
fi

ADDRESS_INFO=$(rpc getaddressinfo "\"$NEW_ADDRESS\"")
PUBKEY=$(echo "$ADDRESS_INFO" | jq -r '.pubkey // empty')
if [[ -z "$PUBKEY" ]] || [[ "$ADDRESS_INFO" == *error* ]]; then
    log "ERROR: Could not retrieve pubkey from new address"
    log "Raw response: $ADDRESS_INFO"
    exit 1
fi

# Safe update with automatic backup
log "Updating coinbase_reward_script to: wpkh(${PUBKEY})"
if sed -i.bak "s|coinbase_reward_script = \".*\"|coinbase_reward_script = \"wpkh(${PUBKEY})\"|" "$CONFIG_FILE"; then
    log "✓ Successfully updated config (backup created: ${CONFIG_FILE}.bak)"
else
    log "ERROR: Failed to update config file"
    exit 1
fi

# =============================================================================
# SERVICE RESTART LOGIC
# =============================================================================
# Note: In the future, azpool may support hot-reload of the coinbase_reward_script.
#       Restart is currently required because there is no reload mechanism available.
# =============================================================================
log "Restarting azpool service..."
if systemctl restart azpool; then
    sleep 2
    if systemctl is-active --quiet azpool; then
        log "✓ azpool restarted successfully and is running"
    else
        log "WARNING: azpool was restarted but is not active"
    fi
else
    log "ERROR: Failed to restart azpool service"
    exit 1
fi

log "=== Coinbase Updater completed successfully ==="

exit 0