from __future__ import annotations

import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime

from payouts.collector.app.config import (
    CollectorSettings,
    ConfigError,
    load_settings,
    resolve_pool_instances,
)
from payouts.collector.app.db import (
    connect,
    counters_from_previous,
    ensure_pool_instance,
    fetch_active_identity_mappings,
    fetch_active_pool_instances,
    fetch_previous_snapshot,
    finish_collector_run,
    insert_delta,
    insert_snapshot,
    start_collector_run,
)
from payouts.collector.app.identity import resolve_sc_node_id
from payouts.collector.app.delta import SnapshotCounters, compute_delta, is_counter_reset
from payouts.collector.app.pool_client import (
    PoolMonitoringError,
    fetch_client_channels,
    fetch_clients,
    fetch_health,
    normalize_channels,
)

logger = logging.getLogger(__name__)


def _client_id(item: dict) -> int | None:
    for key in ("client_id", "id"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _finish_failed_run(
    database_url: str,
    run_id: int,
    totals: dict[str, int],
    error_message: str,
) -> None:
    with connect(database_url) as conn:
        finish_collector_run(
            conn,
            run_id,
            status="failed",
            pools_checked=totals["pools_checked"],
            snapshots_written=totals["snapshots_written"],
            deltas_written=totals["deltas_written"],
            resets_detected=totals["resets_detected"],
            error_message=error_message,
        )
        conn.commit()


def collect_once(settings: CollectorSettings) -> dict[str, int]:
    observed_at = datetime.now(UTC)
    totals = {
        "pools_checked": 0,
        "snapshots_written": 0,
        "deltas_written": 0,
        "resets_detected": 0,
    }

    with connect(settings.database_url) as conn:
        db_pools = fetch_active_pool_instances(conn)
        pool_instances = resolve_pool_instances(db_pools, settings.env_pool_instances)

        run_id = start_collector_run(conn)
        conn.commit()
        identity_mappings = fetch_active_identity_mappings(conn)

        try:
            for pool in pool_instances:
                totals["pools_checked"] += 1
                with conn.transaction():
                    ensure_pool_instance(conn, pool)
                    fetch_health(pool.base_url, timeout_seconds=settings.request_timeout_seconds)

                    clients = fetch_clients(
                        pool.base_url,
                        offset=0,
                        limit=settings.clients_page_limit,
                        timeout_seconds=settings.request_timeout_seconds,
                    )

                    for client in clients:
                        client_id = _client_id(client)
                        if client_id is None:
                            continue

                        channels_payload = fetch_client_channels(
                            pool.base_url,
                            client_id,
                            offset=0,
                            limit=settings.channels_page_limit,
                            timeout_seconds=settings.request_timeout_seconds,
                        )
                        channels = normalize_channels(client_id, channels_payload)

                        for channel in channels:
                            channel = replace(
                                channel,
                                sc_node_id=resolve_sc_node_id(
                                    channel.user_identity,
                                    identity_mappings,
                                ),
                            )
                            previous = fetch_previous_snapshot(
                                conn,
                                pool_instance_id=pool.id,
                                client_id=channel.client_id,
                                channel_type=channel.channel_type,
                                channel_id=channel.channel_id,
                            )

                            insert_snapshot(
                                conn,
                                pool_instance_id=pool.id,
                                channel=channel,
                                observed_at=observed_at,
                            )
                            totals["snapshots_written"] += 1

                            if previous is None:
                                continue

                            previous_counters = counters_from_previous(previous)
                            current_counters = SnapshotCounters(
                                shares_accepted=channel.shares_accepted,
                                share_work_sum=channel.share_work_sum,
                                last_share_sequence_number=channel.last_share_sequence_number,
                                observed_at=observed_at,
                            )

                            if is_counter_reset(previous_counters, current_counters):
                                totals["resets_detected"] += 1
                                logger.warning(
                                    "counter reset detected pool=%s client=%s channel=%s/%s identity=%s",
                                    pool.id,
                                    channel.client_id,
                                    channel.channel_type,
                                    channel.channel_id,
                                    channel.user_identity,
                                )
                                continue

                            delta = compute_delta(
                                pool_instance_id=pool.id,
                                client_id=channel.client_id,
                                channel_type=channel.channel_type,
                                channel_id=channel.channel_id,
                                previous=previous_counters,
                                current=current_counters,
                            )
                            if delta is None:
                                continue

                            inserted = insert_delta(
                                conn,
                                pool_instance_id=pool.id,
                                client_id=channel.client_id,
                                channel_type=channel.channel_type,
                                channel_id=channel.channel_id,
                                user_identity=channel.user_identity,
                                sc_node_id=channel.sc_node_id,
                                delta=delta,
                            )
                            if inserted:
                                totals["deltas_written"] += 1

            finish_collector_run(
                conn,
                run_id,
                status="success",
                pools_checked=totals["pools_checked"],
                snapshots_written=totals["snapshots_written"],
                deltas_written=totals["deltas_written"],
                resets_detected=totals["resets_detected"],
            )
            conn.commit()
        except (PoolMonitoringError, Exception) as exc:
            _finish_failed_run(settings.database_url, run_id, totals, str(exc))
            raise

    return totals


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 1

    try:
        totals = collect_once(settings)
    except PoolMonitoringError as exc:
        logger.error("pool monitoring error: %s", exc)
        return 1
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 1
    except Exception:
        logger.exception("collector run failed")
        return 1

    logger.info(
        "collector run complete pools=%s snapshots=%s deltas=%s resets=%s",
        totals["pools_checked"],
        totals["snapshots_written"],
        totals["deltas_written"],
        totals["resets_detected"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
