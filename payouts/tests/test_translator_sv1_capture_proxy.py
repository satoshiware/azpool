from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

import pytest

from app.translator_sv1_capture_proxy import (
    TranslatorSv1CaptureProxy,
    TranslatorSv1CaptureProxyConfig,
    TranslatorSv1SessionProcessor,
    load_config_from_env,
)


EXTRANONCE1_RESPONSE = (
    b'{"id":1,"result":[[["mining.set_difficulty","1"],["mining.notify","1"]],"01020304",4],"error":null}\n'
)
NOTIFY = (
    b'{"id":null,"method":"mining.notify","params":["job-1","'
    + b"00" * 32
    + b'","0100000001","ffffffff",["'
    + b"11" * 32
    + b'","'
    + b"22" * 32
    + b'"],"20000000","2200ffff","65000000",true]}\n'
)
SUBMIT = (
    b'{"id":9,"method":"mining.submit","params":["baveetstudy.miner2","job-1","0a0b0c0d","65000001","00000000"]}\n'
)
NON_BLOCK_NOTIFY = NOTIFY.replace(b'"2200ffff"', b'"01010000"')
AUTHORIZE = b'{"id":2,"method":"mining.authorize","params":["baveetstudy.miner2","x"]}\n'


class FakeRepository:
    def __init__(self, *, fail_on_duplicate: bool = False) -> None:
        self.rows = []
        self.blockhashes = set()
        self.fail_on_duplicate = fail_on_duplicate

    def insert_translator_candidate_block(self, event):
        row = event.as_repository_event() if hasattr(event, "as_repository_event") else asdict(event)
        if row["blockhash"] in self.blockhashes and self.fail_on_duplicate:
            raise RuntimeError("duplicate blockhash")
        if row["blockhash"] not in self.blockhashes:
            self.blockhashes.add(row["blockhash"])
            self.rows.append(row)
        row_id = next(i + 1 for i, r in enumerate(self.rows) if r["blockhash"] == row["blockhash"])
        return {**row, "id": row_id}


def _prime_processor(repository=None, *, dry_run=False) -> TranslatorSv1SessionProcessor:
    processor = TranslatorSv1SessionProcessor(repository=repository, dry_run=dry_run)
    processor.process_upstream_bytes(EXTRANONCE1_RESPONSE)
    processor.process_upstream_bytes(NOTIFY)
    processor.process_downstream_bytes(AUTHORIZE)
    return processor


def test_mining_authorize_associates_worker_identity_with_session() -> None:
    processor = TranslatorSv1SessionProcessor(repository=None, dry_run=True)

    processor.process_downstream_bytes(AUTHORIZE)

    assert processor.worker_identity == "baveetstudy.miner2"


def test_mining_notify_stores_job_state() -> None:
    processor = TranslatorSv1SessionProcessor(repository=None, dry_run=True)

    processor.process_upstream_bytes(NOTIFY)

    assert processor.jobs["job-1"].job_id == "job-1"
    assert processor.jobs["job-1"].nbits == "2200ffff"



def _records_by_event(caplog, name: str):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_mining_submit_seen_and_reconstruction_logs_normal_submit(caplog) -> None:
    repository = FakeRepository()
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor = _prime_processor(repository, dry_run=False)
        processor.process_downstream_bytes(SUBMIT)
    seen = _records_by_event(caplog, "mining_submit_seen")
    assert len(seen) == 1
    assert seen[0].job_id == "job-1"
    assert seen[0].job_state_exists is True
    assert seen[0].extranonce1_exists is True
    assert seen[0].version == "20000000"
    recon = _records_by_event(caplog, "candidate_reconstructed")
    assert len(recon) == 1
    assert recon[0].meets_target is True
    assert recon[0].reason is None
    rmsg = recon[0].getMessage()
    assert "event=candidate_reconstructed" in rmsg
    assert "blockhash=" in rmsg
    assert "meets_target=" in rmsg
    assert "version=" in rmsg
    assert "job_id=" in rmsg
    assert "submit_version=" in rmsg

    for _k in (
        "sv1_extranonce1=",
        "sv1_extranonce2=",
        "sv1_full_extranonce=",
        "translated_full_extranonce=",
        "full_extranonce_used_for_reconstruction=",
        "coinbase_tx_hash=",
        "merkle_root=",
        "prev_hash_display=",
        "prev_hash_header_hex=",
        "header_hex=",
        "version=",
        "submit_version=",
        "blockhash=",
        "meets_target=",
    ):
        assert _k in rmsg
    assert "candidate_insert_attempted" in [getattr(r, "event", None) for r in caplog.records]
    assert "candidate_insert_succeeded" in [getattr(r, "event", None) for r in caplog.records]
    ok = _records_by_event(caplog, "candidate_insert_succeeded")
    assert len(ok) == 1
    assert "id=" in ok[0].getMessage()
    assert "blockhash=" in ok[0].getMessage()


def test_mining_submit_seen_unknown_job_id(caplog) -> None:
    processor = TranslatorSv1SessionProcessor(repository=None, dry_run=True)
    processor.process_upstream_bytes(EXTRANONCE1_RESPONSE)
    processor.process_downstream_bytes(AUTHORIZE)
    bad_submit = SUBMIT.replace(b"job-1", b"job-unknown")
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor.process_downstream_bytes(bad_submit)
    seen = _records_by_event(caplog, "mining_submit_seen")
    assert len(seen) == 1
    assert seen[0].job_id == "job-unknown"
    assert seen[0].job_state_exists is False
    assert _records_by_event(caplog, "candidate_reconstructed") == []


def test_mining_submit_seen_missing_extranonce1(caplog) -> None:
    processor = TranslatorSv1SessionProcessor(repository=None, dry_run=True)
    processor.process_upstream_bytes(NOTIFY)
    processor.process_downstream_bytes(AUTHORIZE)
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor.process_downstream_bytes(SUBMIT)
    seen = _records_by_event(caplog, "mining_submit_seen")
    assert len(seen) == 1
    assert seen[0].extranonce1_exists is False
    assert _records_by_event(caplog, "candidate_reconstructed") == []


def test_candidate_reconstructed_non_target_meeting_submit(caplog) -> None:
    repository = FakeRepository()
    processor = TranslatorSv1SessionProcessor(repository=repository, dry_run=False)
    processor.process_upstream_bytes(EXTRANONCE1_RESPONSE)
    processor.process_upstream_bytes(NON_BLOCK_NOTIFY)
    processor.process_downstream_bytes(AUTHORIZE)
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor.process_downstream_bytes(SUBMIT)
    recon = _records_by_event(caplog, "candidate_reconstructed")
    assert len(recon) == 1
    assert recon[0].meets_target is False
    assert recon[0].reason == "candidate_hash_above_nbits_target"
    rmsg = recon[0].getMessage()
    assert "event=candidate_reconstructed" in rmsg
    assert "meets_target=false" in rmsg
    assert "blockhash=" in rmsg
    assert "version=" in rmsg

    for _k in (
        "sv1_extranonce1=",
        "sv1_extranonce2=",
        "sv1_full_extranonce=",
        "translated_full_extranonce=",
        "full_extranonce_used_for_reconstruction=",
        "coinbase_tx_hash=",
        "merkle_root=",
        "prev_hash_display=",
        "prev_hash_header_hex=",
        "header_hex=",
        "version=",
        "submit_version=",
        "blockhash=",
        "meets_target=",
    ):
        assert _k in rmsg
    assert repository.rows == []


def test_candidate_insert_failed_emits_info_event(caplog) -> None:
    repository = FakeRepository(fail_on_duplicate=True)
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor = _prime_processor(repository, dry_run=False)
        processor.process_downstream_bytes(SUBMIT)
        processor.process_downstream_bytes(SUBMIT)
    failed = _records_by_event(caplog, "candidate_insert_failed")
    assert len(failed) == 1
    assert failed[0].error
    assert failed[0].error_type == "RuntimeError"
    fmsg = failed[0].getMessage()
    assert "event=candidate_insert_failed" in fmsg
    assert "error_type=" in fmsg
    assert "error=" in fmsg


def test_dry_run_emits_reconstruction_but_no_insert_logs(caplog) -> None:
    repository = FakeRepository()
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor = _prime_processor(repository, dry_run=True)
        processor.process_downstream_bytes(SUBMIT)
    assert _records_by_event(caplog, "candidate_reconstructed")[0].meets_target is True
    assert _records_by_event(caplog, "candidate_insert_attempted") == []
    assert repository.rows == []


SUBMIT_SC2_VERSION_ROLL = (
    b'{"id":9,"method":"mining.submit","params":["Ben.Cust","job-1","e8030000","6a039be9","16b092d4","00100000"]}\n'
)


def test_candidate_reconstructed_logs_merged_version_sc2_production_bits(caplog) -> None:
    repository = FakeRepository()
    with caplog.at_level(logging.INFO, "app.translator_sv1_capture_proxy"):
        processor = _prime_processor(repository, dry_run=False)
        processor.process_downstream_bytes(SUBMIT_SC2_VERSION_ROLL)
    recon = _records_by_event(caplog, "candidate_reconstructed")[0]
    msg = recon.getMessage()
    assert "version=20100000" in msg
    assert "submit_version=00100000" in msg
    assert "job_id=job-1" in msg
    assert "blockhash=" in msg
    assert "meets_target=" in msg

    for _k in (
        "sv1_extranonce1=",
        "sv1_extranonce2=",
        "sv1_full_extranonce=",
        "translated_full_extranonce=",
        "full_extranonce_used_for_reconstruction=",
        "coinbase_tx_hash=",
        "merkle_root=",
        "prev_hash_display=",
        "prev_hash_header_hex=",
        "header_hex=",
        "version=",
        "submit_version=",
        "blockhash=",
        "meets_target=",
    ):
        assert _k in msg


def test_mining_submit_candidate_event_triggers_insert_when_target_is_met() -> None:
    repository = FakeRepository()
    processor = _prime_processor(repository)

    processor.process_downstream_bytes(SUBMIT)

    assert len(repository.rows) == 1
    row = repository.rows[0]
    assert row["worker_identity"] == "baveetstudy.miner2"
    assert row["source"] == "sv1_capture_proxy"
    assert row["proof_type"] == "translator_submit_reconstructed_block_hash"
    assert row["raw_submit_json"]["method"] == "mining.submit"
    assert row["raw_job_json"]["method"] == "mining.notify"


def test_non_block_share_does_not_insert() -> None:
    repository = FakeRepository()
    processor = TranslatorSv1SessionProcessor(repository=repository, dry_run=False)
    processor.process_upstream_bytes(EXTRANONCE1_RESPONSE)
    processor.process_upstream_bytes(NON_BLOCK_NOTIFY)
    processor.process_downstream_bytes(AUTHORIZE)

    processor.process_downstream_bytes(SUBMIT)

    assert repository.rows == []


def test_dry_run_does_not_insert() -> None:
    repository = FakeRepository()
    processor = _prime_processor(repository, dry_run=True)

    processor.process_downstream_bytes(SUBMIT)

    assert repository.rows == []


def test_duplicate_blockhash_insert_does_not_crash() -> None:
    repository = FakeRepository(fail_on_duplicate=True)
    processor = _prime_processor(repository)

    processor.process_downstream_bytes(SUBMIT)
    processor.process_downstream_bytes(SUBMIT)

    assert len(repository.rows) == 1


def test_channel_id_null_when_mapping_unavailable() -> None:
    repository = FakeRepository()
    processor = _prime_processor(repository)

    processor.process_downstream_bytes(SUBMIT)

    assert repository.rows[0]["channel_id"] is None


def test_no_payout_reward_ownership_maturity_fields_are_inserted() -> None:
    repository = FakeRepository()
    processor = _prime_processor(repository)

    processor.process_downstream_bytes(SUBMIT)

    assert {
        "payout_readiness",
        "confirmations",
        "maturity_status",
        "coinbase_total_sats",
        "ownership",
        "accepted",
        "rejected",
    }.isdisjoint(repository.rows[0])


def test_config_defaults_to_dry_run_and_requires_postgres_only_for_insert_mode() -> None:
    dry_config = load_config_from_env(
        {
            "TRANSLATOR_CAPTURE_UPSTREAM_HOST": "127.0.0.1",
            "TRANSLATOR_CAPTURE_UPSTREAM_PORT": "4444",
        }
    )
    assert dry_config.listen_host == "127.0.0.1"
    assert dry_config.listen_port == 3333
    assert dry_config.dry_run is True

    with pytest.raises(ValueError, match="POSTGRES_LEDGER_DATABASE_URL"):
        load_config_from_env(
            {
                "TRANSLATOR_CAPTURE_UPSTREAM_HOST": "127.0.0.1",
                "TRANSLATOR_CAPTURE_UPSTREAM_PORT": "4444",
                "TRANSLATOR_CAPTURE_DRY_RUN": "false",
            }
        )


def test_proxy_forwards_bytes_even_if_parser_errors() -> None:
    asyncio.run(_assert_proxy_forwards_bytes_even_if_parser_errors())


async def _assert_proxy_forwards_bytes_even_if_parser_errors() -> None:
    received_by_upstream = bytearray()

    async def upstream_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        received_by_upstream.extend(data)
        writer.write(b"upstream-response\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream_server = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    proxy_config = TranslatorSv1CaptureProxyConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream_port,
        postgres_database_url=None,
        dry_run=True,
        log_level="INFO",
    )
    proxy = TranslatorSv1CaptureProxy(config=proxy_config, repository=None)
    proxy_server = await asyncio.start_server(proxy._handle_client, "127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        invalid_json = b'{"method": "mining.submit", "params": [bad-json]}\n'
        writer.write(invalid_json)
        await writer.drain()
        response = await reader.readline()
        writer.close()
        await writer.wait_closed()

        assert bytes(received_by_upstream) == invalid_json
        assert response == b"upstream-response\n"
    finally:
        proxy_server.close()
        upstream_server.close()
        await proxy_server.wait_closed()
        await upstream_server.wait_closed()
