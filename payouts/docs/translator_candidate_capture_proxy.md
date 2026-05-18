# Translator Candidate Capture Proxy

This proxy is a dry-run-first ingestion path for reconstructed translator candidate block events.
It forwards SV1 TCP bytes unchanged between miners and the translator, parses line-delimited JSON-RPC opportunistically, and inserts into `translator_candidate_blocks` only when a reconstructed submit hash meets the job `nbits` target.

Do not put this inline with live mining until it has been tested with the exact SC-2 miner and translator topology.

## Required Environment

- `TRANSLATOR_CAPTURE_UPSTREAM_HOST`
- `TRANSLATOR_CAPTURE_UPSTREAM_PORT`
- `POSTGRES_LEDGER_DATABASE_URL` when `TRANSLATOR_CAPTURE_DRY_RUN=false`

Optional:

- `TRANSLATOR_CAPTURE_LISTEN_HOST`, default `127.0.0.1`
- `TRANSLATOR_CAPTURE_LISTEN_PORT`, default `3333`
- `TRANSLATOR_CAPTURE_DRY_RUN`, default `true`
- `TRANSLATOR_CAPTURE_LOG_LEVEL`, default `INFO`
- `TRANSLATOR_CAPTURE_CHANNELS_URL`, parsed for future channel lookup work but not used in this PR

## Flow

miners -> capture proxy -> translator

The proxy captures `mining.authorize`, `mining.notify`, the subscribe response `extranonce1`, and `mining.submit`.
It reconstructs the coinbase, merkle root, block header, and candidate blockhash from notify and submit data.

## SV1 `mining.submit` version field (version rolling)

When present, the optional 6th parameter on `mining.submit` is **version-rolling bits**, not the full 32-bit block header `version`.

Those bits are combined with the **base/template `version` from `mining.notify`**:

- If `mining.notify` includes a BIP310-style `version-rolling.mask` in the extended params object, the header version is `(notify.version & ~mask) | (submit_version & mask)`.
- Otherwise, the header version is `notify.version | submit_version` (bitwise OR).
- If `mining.submit` omits the 6th parameter, the header uses `notify.version` only.

Logs use **`submit_version=`** for the raw submit bits and **`version=`** for the **final merged** header version used in the reconstruction.

## ``mining.notify`` previous block hash (display vs header bytes)

`mining.notify` carries the previous block hash as **human-style hex** (the same big-endian-looking 64-character string block explorers print). **Bitcoin block headers** store that value as **32 raw bytes in reverse byte order** (full little-endian of the 256-bit integer), i.e. ``bytes.fromhex(display_hex)[::-1]``—**not** independent byte swaps within 4-byte words with preserved word order.

Logs use **`prev_hash_display=`** for the notify string and **`prev_hash_header_hex=`** for the 64 hex characters that appear **immediately after** the 4-byte little-endian `version` inside reconstructed **`header_hex=`**.

## SV1 extranonce vs pool / SV2 ``SubmitSharesExtended``

The subscribe response exposes **SV1 ``extranonce1``** (bytes) and miners send **``extranonce2``**. Classically the serialized coinbase inserts **``extranonce1 ‖ extranonce2``** between ``coinbase1`` and ``coinbase2`` (**``sv1_full_extranonce``** in logs).

Upstream, **``aztranslator``** can advertise a *long* SV1 ``extranonce1`` while the pool’s SV2 share carries a *fixed-width* **full extranonce** (same byte length as that long ``extranonce1``). On SC‑2 / ubuntu01 this has matched **the last ``len(extranonce1) − len(extranonce2)`` bytes of SV1 ``extranonce1``, followed by ``extranonce2``** (**``translated_full_extranonce``**).

When **``len(extranonce1) == len(extranonce2)``** (typical 4‑byte / 4‑byte subscribe), no translation applies and reconstruction uses the classic splice.

Reconstruction always logs **``full_extranonce_used_for_reconstruction``** (either translated or SV1 full). If a deployment disagrees with pool logs, capture translator SV2 wire bytes or extend instrumentation—do not assume the suffix rule without confirmation.


## Rollout

Start in dry-run mode and inspect logs for reconstructed candidate events.
Only after dry-run validation should `TRANSLATOR_CAPTURE_DRY_RUN=false` be used with a configured ledger Postgres URL.

Rollback is to stop the proxy and point miners back to the translator directly.

## Troubleshooting / correlation

Use the same wall-clock window (UTC) across systems.

1. **Pool / ubuntu01**: Locate `Block Found` or `SubmitSharesExtended` (or equivalent) lines. Note `job_id`, `nonce`, `ntime`, `version`, `extranonce` / extranonce2 span, and `blockhash`.

2. **Capture proxy journal**: For each downstream `mining_submit_seen` log, match `job_id`, `extranonce2`, `ntime`, `nonce`, `submit_version` (raw rolling bits from submit when present), and `version` (merged header version). Confirm `job_state_exists` and `extranonce1_exists`; if either is false, reconstruction will not run and no `candidate_reconstructed` line will appear for that submit.

3. **`candidate_reconstructed`**: Forensic keys include `sv1_extranonce1`, `sv1_extranonce2`, `sv1_full_extranonce`, `translated_full_extranonce` (``-`` when unused), `full_extranonce_used_for_reconstruction`, plus **`prev_hash_display`** (notify / explorer-style hex), **`prev_hash_header_hex`** (32-byte prev hash as in the reconstructed 80-byte header, immediately after LE `version`), `nbits`, `coinbase_tx_hash`, `merkle_root`, **`header_hex`**, **`blockhash`**, **`meets_target`**, `target`, `reason`, `version`, and `submit_version`. Compare **`full_extranonce_used_for_reconstruction`** to the pool’s reported extranonce when hashes diverge.

4. **`translator_candidate_blocks`**: Rows appear only when `TRANSLATOR_CAPTURE_DRY_RUN=false`, a repository is configured, and insert attempted. Correlate by `blockhash` / `job_id` with `candidate_insert_succeeded`; if you see `candidate_insert_failed`, inspect the `error` field in that log line and the following exception stack.

## Journal / journald (auditing)

INFO messages embed key=value pairs in the log **message** (not only in structured fields), so `journalctl` and default formatters show reconstruction details.

Examples:

```bash
sudo journalctl -u azcoin-translator-capture-proxy -f | grep -E 'event=(mining_submit_seen|candidate_reconstructed|candidate_insert_)'
```

Narrow to reconstructed submits and inserts:

```bash
sudo journalctl -u azcoin-translator-capture-proxy --since today \
  | grep -E 'event=candidate_reconstructed|event=candidate_insert_'
```

Match a specific winning `blockhash` and merged `version` / `submit_version`:

```bash
sudo journalctl -u azcoin-translator-capture-proxy --since today \
  | grep -E 'event=candidate_reconstructed .*blockhash=0000000000000082b60487dd794fddacffa04d8c8b34c2556133b4a2b04d9b59'
```

Forensic slice for comparing pool winning submits vs proxy reconstruction (adjust service name and UTC window):

```bash
sudo journalctl -u azcoin-translator-capture-proxy.service --since "<time>" --until "<time>" --no-pager \
  | grep -Ei 'event=candidate_reconstructed|sv1_full_extranonce|translated_full_extranonce|full_extranonce_used_for_reconstruction|coinbase_tx_hash|merkle_root|prev_hash_display|prev_hash_header_hex|header_hex|blockhash|meets_target'
```
