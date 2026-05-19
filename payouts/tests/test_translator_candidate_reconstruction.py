from __future__ import annotations

import re
from datetime import UTC, datetime

from app.translator_candidate_reconstruction import (
    build_block_header,
    candidate_blockhash_from_header,
    compute_merkle_root,
    decode_nbits_target,
    merge_sv1_header_version,
    parse_mining_authorize,
    parse_mining_notify,
    parse_mining_submit,
    prev_hash_header_bytes_from_display,
    reconstruct_coinbase,
    reconstruct_submit_candidate,
    try_derive_translated_full_extranonce_for_pool,
)


FOUND_TIME = datetime(2026, 5, 6, 17, 47, 45, tzinfo=UTC)
EXTRANONCE1 = "01020304"


def _notify(nbits: str = "2200ffff") -> dict:
    return {
        "id": None,
        "method": "mining.notify",
        "params": [
            "job-1",
            "00" * 32,
            "0100000001",
            "ffffffff",
            ["11" * 32, "22" * 32],
            "20000000",
            nbits,
            "65000000",
            True,
        ],
    }


def _submit(nonce: str = "00000000") -> dict:
    return {
        "id": 9,
        "method": "mining.submit",
        "params": ["baveetstudy.miner2", "job-1", "0a0b0c0d", "65000001", nonce],
    }


def test_mining_authorize_captures_worker_identity() -> None:
    parsed = parse_mining_authorize(
        {"id": 1, "method": "mining.authorize", "params": ["baveetstudy.miner2", "x"]}
    )

    assert parsed is not None
    assert parsed.worker_identity == "baveetstudy.miner2"


def test_mining_notify_parses_job_state() -> None:
    parsed = parse_mining_notify(_notify())

    assert parsed is not None
    assert parsed.job_id == "job-1"
    assert parsed.prev_hash == "00" * 32
    assert parsed.coinbase1 == "0100000001"
    assert parsed.coinbase2 == "ffffffff"
    assert parsed.merkle_branches == ("11" * 32, "22" * 32)
    assert parsed.version == "20000000"
    assert parsed.clean_jobs is True


def test_mining_submit_reconstructs_candidate_blockhash() -> None:
    job = parse_mining_notify(_notify())
    submit = parse_mining_submit(_submit())
    assert job is not None
    assert submit is not None

    result = reconstruct_submit_candidate(
        job=job,
        submit=submit,
        extranonce1=EXTRANONCE1,
        found_time=FOUND_TIME,
    )
    coinbase = reconstruct_coinbase(job, EXTRANONCE1, submit.extranonce2)
    merkle_root = compute_merkle_root(coinbase, job.merkle_branches)
    header = build_block_header(
        version=result.header_version,
        prev_hash_display=job.prev_hash,
        merkle_root=merkle_root,
        ntime=submit.ntime,
        nbits=job.nbits,
        nonce=submit.nonce,
    )

    assert result.candidate_hash == candidate_blockhash_from_header(header)
    assert re.fullmatch(r"[0-9a-f]{64}", result.candidate_hash)


def test_nbits_target_comparison_identifies_block_found() -> None:
    job = parse_mining_notify(_notify(nbits="2200ffff"))
    submit = parse_mining_submit(_submit())
    assert job is not None
    assert submit is not None

    result = reconstruct_submit_candidate(
        job=job,
        submit=submit,
        extranonce1=EXTRANONCE1,
        found_time=FOUND_TIME,
        channel_id=3,
    )

    assert result.block_found is True
    assert result.event is not None
    assert int(result.event.blockhash, 16) <= decode_nbits_target("2200ffff")


def test_non_block_share_is_not_classified_as_block_found() -> None:
    job = parse_mining_notify(_notify(nbits="01010000"))
    submit = parse_mining_submit(_submit())
    assert job is not None
    assert submit is not None

    result = reconstruct_submit_candidate(
        job=job,
        submit=submit,
        extranonce1=EXTRANONCE1,
        found_time=FOUND_TIME,
    )

    assert result.block_found is False
    assert result.event is None



def test_mining_submit_sixth_param_merged_with_job_base_version_for_header() -> None:
    job = parse_mining_notify(_notify())
    submit_lo = parse_mining_submit(
        {
            "id": 9,
            "method": "mining.submit",
            "params": ["baveetstudy.miner2", "job-1", "0a0b0c0d", "65000001", "00000000", "20000000"],
        }
    )
    submit_hi = parse_mining_submit(
        {
            "id": 9,
            "method": "mining.submit",
            "params": ["baveetstudy.miner2", "job-1", "0a0b0c0d", "65000001", "00000000", "20100000"],
        }
    )
    assert job is not None and submit_lo is not None and submit_hi is not None
    assert submit_hi.version == "20100000"
    r_lo = reconstruct_submit_candidate(job=job, submit=submit_lo, extranonce1=EXTRANONCE1, found_time=FOUND_TIME)
    r_hi = reconstruct_submit_candidate(job=job, submit=submit_hi, extranonce1=EXTRANONCE1, found_time=FOUND_TIME)
    assert r_lo.header_version == "20000000"
    assert r_hi.header_version == "20100000"
    assert r_lo.candidate_hash != r_hi.candidate_hash


def test_merge_sv1_header_version_sc2_may_2026_production_regression() -> None:
    assert merge_sv1_header_version("20000000", "00100000", None) == "20100000"
    assert merge_sv1_header_version("20000000", None, None) == "20000000"


def test_reconstruct_header_version_sc2_submit_bits_may_2026() -> None:
    job = parse_mining_notify(_notify())
    submit = parse_mining_submit(
        {
            "id": 9,
            "method": "mining.submit",
            "params": ["Ben.Cust", "job-1", "e8030000", "6a039be9", "16b092d4", "00100000"],
        }
    )
    assert job is not None and submit is not None
    result = reconstruct_submit_candidate(
        job=job,
        submit=submit,
        extranonce1=EXTRANONCE1,
        found_time=FOUND_TIME,
    )
    assert result.header_version == "20100000"
    assert submit.version == "00100000"


def test_merge_sv1_header_version_respects_pool_mask_when_present() -> None:
    mask = "001fffff"
    assert merge_sv1_header_version("20000000", "00100000", mask) == "20100000"


def test_parse_mining_notify_extracts_version_rolling_mask() -> None:
    payload = {
        "id": None,
        "method": "mining.notify",
        "params": [
            "job-1",
            "00" * 32,
            "0100000001",
            "ffffffff",
            ["11" * 32, "22" * 32],
            "20000000",
            "2200ffff",
            "65000000",
            True,
            {"version-rolling": {"mask": "001fffff", "min-bit": 13}},
        ],
    }
    job = parse_mining_notify(payload)
    assert job is not None
    assert job.version_rolling_mask == "001fffff"


def test_reconstructed_event_includes_worker_identity_and_safe_fields() -> None:
    job = parse_mining_notify(_notify())
    submit = parse_mining_submit(_submit())
    assert job is not None
    assert submit is not None

    result = reconstruct_submit_candidate(
        job=job,
        submit=submit,
        extranonce1=EXTRANONCE1,
        found_time=FOUND_TIME,
        worker_identity="authorized.worker",
    )

    assert result.event is not None
    event = result.event.as_repository_event()
    assert event["worker_identity"] == "authorized.worker"
    assert event["found_time_unix"] == 1778089665
    assert event["source"] == "sv1_capture_proxy"
    assert event["proof_type"] == "translator_submit_reconstructed_block_hash"
    assert re.fullmatch(r"[0-9a-f]{64}", event["blockhash"])
    assert {
        "payout_readiness",
        "confirmations",
        "maturity_status",
        "coinbase_total_sats",
        "ownership",
        "accepted",
        "rejected",
    }.isdisjoint(event)


def test_reconstruct_submit_candidate_populates_forensic_fields() -> None:
    job = parse_mining_notify(_notify())
    submit = parse_mining_submit(_submit())
    assert job is not None and submit is not None
    result = reconstruct_submit_candidate(
        job=job,
        submit=submit,
        extranonce1=EXTRANONCE1,
        found_time=FOUND_TIME,
        worker_identity="w.test",
    )
    assert result.worker_identity == "w.test"
    assert result.sv1_full_extranonce == (EXTRANONCE1 + submit.extranonce2).lower()
    assert result.translated_full_extranonce is None
    assert result.full_extranonce_used_for_reconstruction == result.sv1_full_extranonce
    assert len(result.coinbase_tx_hash) == 64
    assert len(result.merkle_root) == 64
    assert result.prev_hash_display == job.prev_hash
    assert (
        result.prev_hash_header_hex
        == prev_hash_header_bytes_from_display(job.prev_hash).hex()
    )
    assert len(result.header_hex) == 160
    assert result.meets_target == result.block_found
    assert result.reason is None or result.reason == "candidate_hash_above_nbits_target"


def test_prev_hash_serializes_as_full_little_endian_in_header_may_2026_regression() -> None:
    display = "00000000000000aa9a25ad4be42f99a00d3258a2989857f60db0810f975baae2"
    expected_header_prev = (
        "e2aa5b970f81b00df6579898a258320da0992fe44bad259aaa00000000000000"
    )
    assert prev_hash_header_bytes_from_display(display).hex() == expected_header_prev
    header = build_block_header(
        version="20040000",
        prev_hash_display=display,
        merkle_root=bytes(32),
        ntime="6a03ae56",
        nbits="01234567",
        nonce="914a114f",
    )
    header_hex = header.hex()
    version_prefix = int("20040000", 16).to_bytes(4, "little").hex()
    assert header_hex.startswith(version_prefix)
    assert header_hex[len(version_prefix) : len(version_prefix) + 64] == expected_header_prev


def test_try_derive_translated_full_extranonce_production_may_2026() -> None:
    sv1_e1 = "01000013000000000000000000000001"
    sv1_e2 = "31010000"
    expected = "00000000000000000000000131010000"
    assert try_derive_translated_full_extranonce_for_pool(sv1_e1, sv1_e2) == expected
    wrong_concat = (sv1_e1 + sv1_e2).lower()
    assert wrong_concat != expected


def test_try_derive_returns_none_for_classic_four_byte_subscribe() -> None:
    assert try_derive_translated_full_extranonce_for_pool("01020304", "0a0b0c0d") is None

