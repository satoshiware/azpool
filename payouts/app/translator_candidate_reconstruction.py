from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping


HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class Sv1Authorize:
    worker_identity: str
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class Sv1NotifyJob:
    job_id: str
    prev_hash: str
    coinbase1: str
    coinbase2: str
    merkle_branches: tuple[str, ...]
    version: str
    nbits: str
    ntime: str
    clean_jobs: bool
    raw_json: dict[str, Any]
    version_rolling_mask: str | None = None


@dataclass(frozen=True)
class Sv1Submit:
    worker_identity: str | None
    job_id: str
    extranonce2: str
    ntime: str
    nonce: str
    raw_json: dict[str, Any]
    version: str | None = None


@dataclass(frozen=True)
class TranslatorCandidateBlockEvent:
    found_time: datetime
    found_time_unix: int
    blockhash: str
    worker_identity: str | None
    channel_id: int | None
    job_id: str
    extranonce2: str
    ntime: str
    nonce: str
    version: str
    prev_hash: str
    nbits: str
    source: str
    proof_type: str
    raw_submit_json: dict[str, Any]
    raw_job_json: dict[str, Any]

    def as_repository_event(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ReconstructionResult:
    block_found: bool
    candidate_hash: str
    target: int
    candidate_value: int
    header_version: str
    submit_version: str | None
    worker_identity: str | None
    prev_hash_display: str
    prev_hash_header_hex: str
    nbits: str
    ntime: str
    nonce: str
    sv1_extranonce1: str
    sv1_extranonce2: str
    sv1_full_extranonce: str
    translated_full_extranonce: str | None
    full_extranonce_used_for_reconstruction: str
    coinbase_tx_hash: str
    merkle_root: str
    header_hex: str
    meets_target: bool
    reason: str | None
    event: TranslatorCandidateBlockEvent | None = None


def parse_sv1_json_rpc(message: str | bytes | Mapping[str, Any]) -> dict[str, Any] | None:
    if isinstance(message, Mapping):
        return dict(message)
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_mining_authorize(message: str | bytes | Mapping[str, Any]) -> Sv1Authorize | None:
    payload = parse_sv1_json_rpc(message)
    if payload is None or payload.get("method") != "mining.authorize":
        return None
    params = payload.get("params")
    if not isinstance(params, list) or not params or not isinstance(params[0], str):
        return None
    return Sv1Authorize(worker_identity=params[0], raw_json=payload)


def parse_mining_notify(message: str | bytes | Mapping[str, Any]) -> Sv1NotifyJob | None:
    payload = parse_sv1_json_rpc(message)
    if payload is None or payload.get("method") != "mining.notify":
        return None
    params = payload.get("params")
    if not isinstance(params, list) or len(params) < 9:
        return None
    branches = params[4]
    if not isinstance(branches, list):
        return None
    if not all(isinstance(branch, str) for branch in branches):
        return None
    version_rolling_mask: str | None = None
    if len(params) >= 10 and isinstance(params[9], dict):
        vr = params[9].get("version-rolling")
        if isinstance(vr, dict) and isinstance(vr.get("mask"), str):
            raw_mask = vr["mask"].strip().lower()
            if len(raw_mask) == 8 and HEX_RE.fullmatch(raw_mask):
                version_rolling_mask = raw_mask
    return Sv1NotifyJob(
        job_id=str(params[0]),
        prev_hash=str(params[1]).lower(),
        coinbase1=str(params[2]).lower(),
        coinbase2=str(params[3]).lower(),
        merkle_branches=tuple(branch.lower() for branch in branches),
        version=str(params[5]).lower(),
        nbits=str(params[6]).lower(),
        ntime=str(params[7]).lower(),
        clean_jobs=bool(params[8]),
        raw_json=payload,
        version_rolling_mask=version_rolling_mask,
    )


def parse_mining_submit(message: str | bytes | Mapping[str, Any]) -> Sv1Submit | None:
    payload = parse_sv1_json_rpc(message)
    if payload is None or payload.get("method") != "mining.submit":
        return None
    params = payload.get("params")
    if not isinstance(params, list) or len(params) < 5:
        return None
    submit_version: str | None = None
    if len(params) >= 6 and params[5] is not None:
        raw_ver = str(params[5]).strip()
        if raw_ver:
            submit_version = raw_ver.lower()
    return Sv1Submit(
        worker_identity=str(params[0]) if params[0] is not None else None,
        job_id=str(params[1]),
        extranonce2=str(params[2]).lower(),
        ntime=str(params[3]).lower(),
        nonce=str(params[4]).lower(),
        raw_json=payload,
        version=submit_version,
    )


def double_sha256(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()


def coinbase_tx_hash_hex(coinbase: bytes) -> str:
    """Bitcoin-style txid: double-SHA256 of raw tx, reversed for display."""
    return double_sha256(coinbase)[::-1].hex()


def decode_nbits_target(nbits: str) -> int:
    _require_hex("nbits", nbits, expected_length=8)
    compact = int(nbits, 16)
    exponent = compact >> 24
    mantissa = compact & 0x007FFFFF
    if compact & 0x00800000:
        raise ValueError("negative compact targets are not supported")
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def reconstruct_coinbase(job: Sv1NotifyJob, extranonce1: str, extranonce2: str) -> bytes:
    _require_hex("coinbase1", job.coinbase1)
    _require_hex("coinbase2", job.coinbase2)
    _require_hex("extranonce1", extranonce1)
    _require_hex("extranonce2", extranonce2)
    return bytes.fromhex(job.coinbase1 + extranonce1.lower() + extranonce2.lower() + job.coinbase2)



def reconstruct_coinbase_with_full_extranonce(job: Sv1NotifyJob, full_extranonce_hex: str) -> bytes:
    _require_hex("coinbase1", job.coinbase1)
    _require_hex("coinbase2", job.coinbase2)
    _require_hex("full_extranonce", full_extranonce_hex)
    full = full_extranonce_hex.lower()
    return bytes.fromhex(job.coinbase1 + full + job.coinbase2)


def try_derive_translated_full_extranonce_for_pool(sv1_extranonce1: str, sv1_extranonce2: str) -> str | None:
    """Best-effort SV1 → pool/SV2 full extranonce (same byte width as subscribe extranonce1).

    Observed on production ``aztranslator``: pool ``SubmitSharesExtended.extranonce`` matches the
    last ``len(extranonce1)-len(extranonce2)`` *bytes* of subscribe ``extranonce1`` followed by
    submit ``extranonce2``, yielding a *single* field whose width equals ``len(extranonce1)``.

    Classic 4-byte / 4-byte subscribe has ``len(extranonce1)==len(extranonce2)``; this returns
    ``None`` so reconstruction keeps standard ``extranonce1||extranonce2`` between coinbase
    fragments. If a deployment diverges, instrument translator SV2 framing instead of guessing.
    """
    try:
        e1 = sv1_extranonce1.lower()
        e2 = sv1_extranonce2.lower()
        if len(e1) % 2 or len(e2) % 2:
            return None
        b1 = len(e1) // 2
        b2 = len(e2) // 2
        if b1 < 1 or b2 < 1 or b2 >= b1:
            return None
        suffix_bytes = b1 - b2
        if 2 * suffix_bytes > len(e1):
            return None
        translated = e1[-(2 * suffix_bytes) :] + e2
        if len(translated) != 2 * b1:
            return None
        return translated
    except (TypeError, ValueError):
        return None


def compute_merkle_root(coinbase: bytes, merkle_branches: tuple[str, ...] | list[str]) -> bytes:
    root = double_sha256(coinbase)
    for branch in merkle_branches:
        _require_hex("merkle_branch", branch, expected_length=64)
        root = double_sha256(root + bytes.fromhex(branch))
    return root


def prev_hash_header_bytes_from_display(prev_hash_display_hex: str) -> bytes:
    """Serialize ``mining.notify`` / human-display prev-block hash for the 80-byte header.

    ``prev_hash_display_hex`` matches explorer-style hex (big-endian integer as written).
    Bitcoin headers store the previous block hash as **full 32-byte reverse** of those
    bytes—not uint32 word–local swaps.
    """
    _require_hex("prev_hash_display", prev_hash_display_hex, expected_length=64)
    return bytes.fromhex(prev_hash_display_hex)[::-1]


def build_block_header(
    *,
    version: str,
    prev_hash_display: str,
    merkle_root: bytes,
    ntime: str,
    nbits: str,
    nonce: str,
) -> bytes:
    _require_hex("version", version, expected_length=8)
    _require_hex("ntime", ntime, expected_length=8)
    _require_hex("nbits", nbits, expected_length=8)
    _require_hex("nonce", nonce, expected_length=8)
    if len(merkle_root) != 32:
        raise ValueError("merkle_root must be 32 bytes")
    prev_hdr = prev_hash_header_bytes_from_display(prev_hash_display)
    return b"".join(
        [
            _uint32_le(version),
            prev_hdr,
            merkle_root,
            _uint32_le(ntime),
            _uint32_le(nbits),
            _uint32_le(nonce),
        ]
    )


def candidate_blockhash_from_header(header: bytes) -> str:
    if len(header) != 80:
        raise ValueError("block header must be 80 bytes")
    return double_sha256(header)[::-1].hex()


def merge_sv1_header_version(
    job_version: str,
    submit_version: str | None,
    version_rolling_mask: str | None,
) -> str:
    """Combine job/base block version with SV1 mining.submit version-rolling bits.

    When the pool sends a partial ``version`` on ``mining.submit``, it must be
    merged with the template version from ``mining.notify``, not used as the
    full 32-bit header version.
    """
    if submit_version is None:
        return job_version
    _require_hex("job_version", job_version, expected_length=8)
    _require_hex("submit_version", submit_version, expected_length=8)
    job_i = int(job_version, 16)
    submit_i = int(submit_version, 16)
    if version_rolling_mask is not None:
        _require_hex("version_rolling_mask", version_rolling_mask, expected_length=8)
        mask_i = int(version_rolling_mask, 16)
        combined = (job_i & ~mask_i) | (submit_i & mask_i)
    else:
        combined = job_i | submit_i
    return f"{combined & 0xFFFFFFFF:08x}"


def reconstruct_submit_candidate(
    *,
    job: Sv1NotifyJob,
    submit: Sv1Submit,
    extranonce1: str,
    found_time: datetime | None = None,
    worker_identity: str | None = None,
    channel_id: int | None = None,
) -> ReconstructionResult:
    if submit.job_id != job.job_id:
        raise ValueError("submit job_id does not match notify job_id")
    found_time = found_time or datetime.now(UTC)
    if found_time.tzinfo is None or found_time.utcoffset() is None:
        raise ValueError("found_time must be timezone-aware")

    enonce1 = extranonce1.lower()
    enonce2 = submit.extranonce2.lower()
    sv1_full_extranonce = enonce1 + enonce2
    translated_try = try_derive_translated_full_extranonce_for_pool(enonce1, enonce2)
    if translated_try is not None:
        coinbase = reconstruct_coinbase_with_full_extranonce(job, translated_try)
        full_extranonce_used_for_reconstruction = translated_try
    else:
        coinbase = reconstruct_coinbase(job, enonce1, enonce2)
        full_extranonce_used_for_reconstruction = sv1_full_extranonce
    merkle_root = compute_merkle_root(coinbase, job.merkle_branches)
    header_version = merge_sv1_header_version(
        job.version, submit.version, job.version_rolling_mask
    )
    prev_hash_header_hex = prev_hash_header_bytes_from_display(job.prev_hash).hex()
    header = build_block_header(
        version=header_version,
        prev_hash_display=job.prev_hash,
        merkle_root=merkle_root,
        ntime=submit.ntime,
        nbits=job.nbits,
        nonce=submit.nonce,
    )
    blockhash = candidate_blockhash_from_header(header)
    target = decode_nbits_target(job.nbits)
    candidate_value = int(blockhash, 16)
    block_found = candidate_value <= target
    coinbase_tx_hash = coinbase_tx_hash_hex(coinbase)
    merkle_root_hex = merkle_root.hex()
    header_hex = header.hex()
    identity = worker_identity if worker_identity is not None else submit.worker_identity
    reason: str | None = None if block_found else "candidate_hash_above_nbits_target"
    forensic = dict(
        submit_version=submit.version,
        worker_identity=identity,
        prev_hash_display=job.prev_hash,
        prev_hash_header_hex=prev_hash_header_hex,
        nbits=job.nbits,
        ntime=submit.ntime,
        nonce=submit.nonce,
        sv1_extranonce1=enonce1,
        sv1_extranonce2=enonce2,
        sv1_full_extranonce=sv1_full_extranonce,
        translated_full_extranonce=translated_try,
        full_extranonce_used_for_reconstruction=full_extranonce_used_for_reconstruction,
        coinbase_tx_hash=coinbase_tx_hash,
        merkle_root=merkle_root_hex,
        header_hex=header_hex,
        meets_target=block_found,
        reason=reason,
    )
    if not block_found:
        return ReconstructionResult(
            block_found=False,
            candidate_hash=blockhash,
            target=target,
            candidate_value=candidate_value,
            header_version=header_version,
            **forensic,
        )

    return ReconstructionResult(
        block_found=True,
        candidate_hash=blockhash,
        target=target,
        candidate_value=candidate_value,
        header_version=header_version,
        **forensic,
        event=TranslatorCandidateBlockEvent(
            found_time=found_time,
            found_time_unix=int(found_time.timestamp()),
            blockhash=blockhash,
            worker_identity=identity,
            channel_id=channel_id,
            job_id=job.job_id,
            extranonce2=submit.extranonce2,
            ntime=submit.ntime,
            nonce=submit.nonce,
            version=header_version,
            prev_hash=job.prev_hash,
            nbits=job.nbits,
            source="sv1_capture_proxy",
            proof_type="translator_submit_reconstructed_block_hash",
            raw_submit_json=submit.raw_json,
            raw_job_json=job.raw_json,
        ),
    )


def _uint32_le(value: str) -> bytes:
    return int(value, 16).to_bytes(4, byteorder="little", signed=False)


def _require_hex(name: str, value: str, expected_length: int | None = None) -> None:
    if expected_length is not None and len(value) != expected_length:
        raise ValueError(f"{name} must be {expected_length} hex characters")
    if len(value) % 2 != 0 or HEX_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be hex")
