#!/usr/bin/env bash
set -euo pipefail

AZCOIN_CLI="${AZCOIN_CLI:-/usr/local/bin/azcoin-cli}"
CONF="${AZCOIN_CONF:-/etc/azcoin/azcoin.conf}"
DATADIR="${AZCOIN_DATADIR:-/var/lib/azcoin}"

if [ "$#" -lt 2 ]; then
  echo "ERROR: expected -rpcwallet=wallet getbalances|gettransaction|listtransactions|listunspent" >&2
  exit 2
fi

if [ "$1" != "-rpcwallet=wallet" ]; then
  echo "ERROR: only -rpcwallet=wallet is allowed" >&2
  exit 2
fi

cmd="$2"

case "$cmd" in
  getbalances)
    if [ "$#" -ne 2 ]; then
      echo "ERROR: getbalances accepts no extra args" >&2
      exit 2
    fi
    exec "$AZCOIN_CLI" -conf="$CONF" -datadir="$DATADIR" -rpcwallet=wallet getbalances
    ;;

  gettransaction)
    if [ "$#" -ne 3 ]; then
      echo "ERROR: gettransaction requires txid" >&2
      exit 2
    fi
    txid="$3"
    case "$txid" in
      ''|*[!0-9a-fA-F]*)
        echo "ERROR: txid must be hex" >&2
        exit 2
        ;;
    esac
    exec "$AZCOIN_CLI" -conf="$CONF" -datadir="$DATADIR" -rpcwallet=wallet gettransaction "$txid"
    ;;

  listtransactions)
    # Allowed shapes:
    # -rpcwallet=wallet listtransactions
    # -rpcwallet=wallet listtransactions <label>
    # -rpcwallet=wallet listtransactions <label> <count>
    # -rpcwallet=wallet listtransactions <label> <count> <skip>
    # -rpcwallet=wallet listtransactions <label> <count> <skip> <include_watchonly>
    if [ "$#" -gt 6 ]; then
      echo "ERROR: listtransactions accepts at most label count skip include_watchonly" >&2
      exit 2
    fi

    if [ "$#" -ge 4 ]; then
      label="$3"
      if [ "$label" != "*" ]; then
        echo "ERROR: only label '*' is allowed for listtransactions" >&2
        exit 2
      fi
    fi

    if [ "$#" -ge 5 ]; then
      count="$4"
      case "$count" in
        ''|*[!0-9]*)
          echo "ERROR: count must be numeric" >&2
          exit 2
          ;;
      esac
      if [ "$count" -gt 5000 ]; then
        echo "ERROR: count must be <= 5000" >&2
        exit 2
      fi
    fi

    if [ "$#" -ge 6 ]; then
      skip="$5"
      case "$skip" in
        ''|*[!0-9]*)
          echo "ERROR: skip must be numeric" >&2
          exit 2
          ;;
      esac
    fi

    if [ "$#" -eq 7 ]; then
      include_watchonly="$6"
      case "$include_watchonly" in
        true|false) ;;
        *)
          echo "ERROR: include_watchonly must be true or false" >&2
          exit 2
          ;;
      esac
    fi

    exec "$AZCOIN_CLI" -conf="$CONF" -datadir="$DATADIR" "$@"
    ;;

  listunspent)
    # Allowed shapes:
    # -rpcwallet=wallet listunspent
    # -rpcwallet=wallet listunspent <minconf>
    # -rpcwallet=wallet listunspent <minconf> <maxconf>
    if [ "$#" -gt 4 ]; then
      echo "ERROR: listunspent accepts at most minconf maxconf" >&2
      exit 2
    fi

    if [ "$#" -ge 3 ]; then
      minconf="$3"
      case "$minconf" in
        ''|*[!0-9]*)
          echo "ERROR: minconf must be numeric" >&2
          exit 2
          ;;
      esac
    fi

    if [ "$#" -eq 4 ]; then
      maxconf="$4"
      case "$maxconf" in
        ''|*[!0-9]*)
          echo "ERROR: maxconf must be numeric" >&2
          exit 2
          ;;
      esac
    fi

    exec "$AZCOIN_CLI" -conf="$CONF" -datadir="$DATADIR" "$@"
    ;;

  *)
    echo "ERROR: only getbalances, gettransaction, listtransactions, and listunspent are allowed" >&2
    exit 2
    ;;
esac
