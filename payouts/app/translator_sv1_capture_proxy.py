from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from app.translator_candidate_reconstruction import (
    Sv1NotifyJob,
    merge_sv1_header_version,
    parse_mining_authorize,
    parse_mining_notify,
    parse_mining_submit,
    parse_sv1_json_rpc,
    reconstruct_submit_candidate,
)


LOGGER = logging.getLogger(__name__)
MAX_BUFFER_BYTES = 1024 * 1024


def _kv(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _kv_message(parts: list[tuple[str, Any]]) -> str:
    return " ".join(f"{k}={_kv(v)}" for k, v in parts)


class TranslatorCandidateBlockRepository(Protocol):
    def insert_translator_candidate_block(self, event: Any) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class TranslatorSv1CaptureProxyConfig:
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    postgres_database_url: str | None
    dry_run: bool
    log_level: str
    channels_url: str | None = None


def load_config_from_env(environ: dict[str, str] | None = None) -> TranslatorSv1CaptureProxyConfig:
    env = os.environ if environ is None else environ
    listen_host = env.get("TRANSLATOR_CAPTURE_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    listen_port = _parse_port(env.get("TRANSLATOR_CAPTURE_LISTEN_PORT", "3333"), "TRANSLATOR_CAPTURE_LISTEN_PORT")
    upstream_host = env.get("TRANSLATOR_CAPTURE_UPSTREAM_HOST", "").strip()
    upstream_port_value = env.get("TRANSLATOR_CAPTURE_UPSTREAM_PORT", "").strip()
    dry_run = _parse_bool(env.get("TRANSLATOR_CAPTURE_DRY_RUN", "true"))
    postgres_database_url = env.get("POSTGRES_LEDGER_DATABASE_URL", "").strip() or None

    if not upstream_host:
        raise ValueError("TRANSLATOR_CAPTURE_UPSTREAM_HOST is required")
    if not upstream_port_value:
        raise ValueError("TRANSLATOR_CAPTURE_UPSTREAM_PORT is required")
    upstream_port = _parse_port(upstream_port_value, "TRANSLATOR_CAPTURE_UPSTREAM_PORT")
    if not dry_run and not postgres_database_url:
        raise ValueError("POSTGRES_LEDGER_DATABASE_URL is required when TRANSLATOR_CAPTURE_DRY_RUN=false")

    return TranslatorSv1CaptureProxyConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        postgres_database_url=postgres_database_url,
        dry_run=dry_run,
        log_level=env.get("TRANSLATOR_CAPTURE_LOG_LEVEL", "INFO").strip() or "INFO",
        channels_url=env.get("TRANSLATOR_CAPTURE_CHANNELS_URL", "").strip() or None,
    )


class TranslatorSv1SessionProcessor:
    def __init__(
        self,
        *,
        repository: TranslatorCandidateBlockRepository | None,
        dry_run: bool = True,
        channel_lookup: Callable[[str | None], int | None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.dry_run = dry_run
        self.channel_lookup = channel_lookup or (lambda worker_identity: None)
        self.logger = logger or LOGGER
        self.worker_identity: str | None = None
        self.extranonce1: str | None = None
        self.jobs: dict[str, Sv1NotifyJob] = {}
        self._downstream_buffer = b""
        self._upstream_buffer = b""

    def process_downstream_bytes(self, data: bytes) -> None:
        self._downstream_buffer = self._process_lines(
            self._downstream_buffer + data,
            self._process_downstream_line,
        )

    def process_upstream_bytes(self, data: bytes) -> None:
        self._upstream_buffer = self._process_lines(
            self._upstream_buffer + data,
            self._process_upstream_line,
        )

    def _process_lines(self, buffer: bytes, handler: Callable[[bytes], None]) -> bytes:
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip(b"\r")
            if not line:
                continue
            try:
                handler(line)
            except Exception:
                self.logger.exception("translator SV1 capture parser failed; forwarding continues")
        if len(buffer) > MAX_BUFFER_BYTES:
            self.logger.warning("translator SV1 capture parser buffer exceeded limit; dropping buffered parse data")
            return b""
        return buffer

    def _process_downstream_line(self, line: bytes) -> None:
        authorize = parse_mining_authorize(line)
        if authorize is not None:
            self.worker_identity = authorize.worker_identity
            self.logger.debug("captured SV1 worker identity %s", authorize.worker_identity)
            return

        submit = parse_mining_submit(line)
        if submit is None:
            return
        job = self.jobs.get(submit.job_id)
        job_state_exists = job is not None
        extranonce1_exists = self.extranonce1 is not None
        worker_identity = self.worker_identity or submit.worker_identity
        if job is not None:
            merged_version = merge_sv1_header_version(
                job.version, submit.version, job.version_rolling_mask
            )
        else:
            merged_version = submit.version
        self.logger.info(
            _kv_message(
                [
                    ("event", "mining_submit_seen"),
                    ("worker_identity", worker_identity),
                    ("job_id", submit.job_id),
                    ("extranonce2", submit.extranonce2),
                    ("ntime", submit.ntime),
                    ("nonce", submit.nonce),
                    ("version", merged_version),
                    ("submit_version", submit.version),
                    ("job_state_exists", job_state_exists),
                    ("extranonce1_exists", extranonce1_exists),
                ]
            ),
            extra={
                "event": "mining_submit_seen",
                "worker_identity": worker_identity,
                "job_id": submit.job_id,
                "extranonce2": submit.extranonce2,
                "ntime": submit.ntime,
                "nonce": submit.nonce,
                "version": merged_version,
                "submit_version": submit.version,
                "job_state_exists": job_state_exists,
                "extranonce1_exists": extranonce1_exists,
            },
        )
        if job is None:
            self.logger.debug("ignoring SV1 submit for unknown job_id %s", submit.job_id)
            return
        if self.extranonce1 is None:
            self.logger.debug("ignoring SV1 submit for job_id %s until extranonce1 is known", submit.job_id)
            return

        channel_id = self._safe_channel_lookup(worker_identity)
        result = reconstruct_submit_candidate(
            job=job,
            submit=submit,
            extranonce1=self.extranonce1,
            found_time=datetime.now(UTC),
            worker_identity=worker_identity,
            channel_id=channel_id,
        )
        self.logger.info(
            _kv_message(
                [
                    ("event", "candidate_reconstructed"),
                    ("job_id", submit.job_id),
                    ("worker_identity", result.worker_identity),
                    ("version", result.header_version),
                    ("submit_version", result.submit_version),
                    ("prev_hash_display", result.prev_hash_display),
                    ("prev_hash_header_hex", result.prev_hash_header_hex),
                    ("nbits", result.nbits),
                    ("ntime", result.ntime),
                    ("nonce", result.nonce),
                    ("sv1_extranonce1", result.sv1_extranonce1),
                    ("sv1_extranonce2", result.sv1_extranonce2),
                    ("sv1_full_extranonce", result.sv1_full_extranonce),
                    ("translated_full_extranonce", result.translated_full_extranonce),
                    ("full_extranonce_used_for_reconstruction", result.full_extranonce_used_for_reconstruction),
                    ("coinbase_tx_hash", result.coinbase_tx_hash),
                    ("merkle_root", result.merkle_root),
                    ("header_hex", result.header_hex),
                    ("blockhash", result.candidate_hash),
                    ("target", result.target),
                    ("meets_target", result.meets_target),
                    ("reason", result.reason),
                ]
            ),
            extra={
                "event": "candidate_reconstructed",
                "job_id": submit.job_id,
                "worker_identity": result.worker_identity,
                "reconstructed_hash": result.candidate_hash,
                "blockhash": result.candidate_hash,
                "target": result.target,
                "nbits": result.nbits,
                "meets_target": result.meets_target,
                "reason": result.reason,
                "version": result.header_version,
                "submit_version": result.submit_version,
                "nonce": result.nonce,
                "ntime": result.ntime,
                "prev_hash_display": result.prev_hash_display,
                "prev_hash_header_hex": result.prev_hash_header_hex,
                "sv1_extranonce1": result.sv1_extranonce1,
                "sv1_extranonce2": result.sv1_extranonce2,
                "sv1_full_extranonce": result.sv1_full_extranonce,
                "translated_full_extranonce": result.translated_full_extranonce,
                "full_extranonce_used_for_reconstruction": result.full_extranonce_used_for_reconstruction,
                "coinbase_tx_hash": result.coinbase_tx_hash,
                "merkle_root": result.merkle_root,
                "header_hex": result.header_hex,
            },
        )
        if not result.block_found or result.event is None:
            self.logger.debug("SV1 submit candidate hash %s did not meet target", result.candidate_hash)
            return
        if self.dry_run:
            return
        if self.repository is None:
            self.logger.error("translator candidate block found but repository is not configured")
            return
        self.logger.info(
            _kv_message(
                [
                    ("event", "candidate_insert_attempted"),
                    ("blockhash", result.event.blockhash),
                    ("job_id", result.event.job_id),
                ]
            ),
            extra={
                "event": "candidate_insert_attempted",
                "job_id": result.event.job_id,
                "blockhash": result.event.blockhash,
            },
        )
        try:
            inserted = self.repository.insert_translator_candidate_block(result.event)
            row_id = inserted.get("id") if isinstance(inserted, dict) else None
            self.logger.info(
                _kv_message(
                    [
                        ("event", "candidate_insert_succeeded"),
                        ("blockhash", result.event.blockhash),
                        ("id", row_id),
                    ]
                ),
                extra={
                    "event": "candidate_insert_succeeded",
                    "job_id": result.event.job_id,
                    "blockhash": result.event.blockhash,
                    "id": row_id,
                },
            )
        except Exception as exc:
            self.logger.info(
                _kv_message(
                    [
                        ("event", "candidate_insert_failed"),
                        ("blockhash", result.event.blockhash),
                        ("error_type", type(exc).__name__),
                        ("error", str(exc)),
                    ]
                ),
                extra={
                    "event": "candidate_insert_failed",
                    "job_id": result.event.job_id,
                    "blockhash": result.event.blockhash,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            self.logger.exception("failed to insert translator candidate block; forwarding continues")

    def _process_upstream_line(self, line: bytes) -> None:
        self._capture_subscribe_extranonce(line)
        notify = parse_mining_notify(line)
        if notify is None:
            return
        self.jobs[notify.job_id] = notify
        self.logger.debug("captured SV1 notify job_id %s", notify.job_id)

    def _capture_subscribe_extranonce(self, line: bytes) -> None:
        payload = parse_sv1_json_rpc(line)
        if payload is None or payload.get("method") is not None:
            return
        result = payload.get("result")
        if not isinstance(result, list) or len(result) < 2 or not isinstance(result[1], str):
            return
        self.extranonce1 = result[1].lower()
        self.logger.debug("captured SV1 extranonce1")

    def _safe_channel_lookup(self, worker_identity: str | None) -> int | None:
        try:
            return self.channel_lookup(worker_identity)
        except Exception:
            self.logger.exception("translator channel lookup failed; forwarding continues")
            return None


class TranslatorSv1CaptureProxy:
    def __init__(
        self,
        *,
        config: TranslatorSv1CaptureProxyConfig,
        repository: TranslatorCandidateBlockRepository | None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.logger = logger or LOGGER

    async def run_forever(self) -> None:
        server = await asyncio.start_server(
            self._handle_client,
            self.config.listen_host,
            self.config.listen_port,
        )
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        self.logger.info("translator SV1 capture proxy listening on %s", sockets)
        async with server:
            await server.serve_forever()

    async def _handle_client(
        self,
        downstream_reader: asyncio.StreamReader,
        downstream_writer: asyncio.StreamWriter,
    ) -> None:
        peer = downstream_writer.get_extra_info("peername")
        self.logger.info("accepted translator SV1 downstream connection from %s", peer)
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.config.upstream_host,
                self.config.upstream_port,
            )
        except Exception:
            self.logger.exception("failed to connect to translator upstream")
            downstream_writer.close()
            await downstream_writer.wait_closed()
            return

        session = TranslatorSv1SessionProcessor(
            repository=self.repository,
            dry_run=self.config.dry_run,
            logger=self.logger,
        )
        tasks = [
            asyncio.create_task(
                _pipe_bytes(
                    downstream_reader,
                    upstream_writer,
                    session.process_downstream_bytes,
                    self.logger,
                )
            ),
            asyncio.create_task(
                _pipe_bytes(
                    upstream_reader,
                    downstream_writer,
                    session.process_upstream_bytes,
                    self.logger,
                )
            ),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()

        upstream_writer.close()
        downstream_writer.close()
        await asyncio.gather(
            upstream_writer.wait_closed(),
            downstream_writer.wait_closed(),
            return_exceptions=True,
        )


async def _pipe_bytes(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    parser: Callable[[bytes], None],
    logger: logging.Logger,
) -> None:
    while True:
        data = await reader.read(65536)
        if not data:
            return
        writer.write(data)
        await writer.drain()
        try:
            parser(data)
        except Exception:
            logger.exception("translator SV1 capture parser failed; forwarding continues")


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_port(value: str, name: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
