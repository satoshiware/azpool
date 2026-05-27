from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

CREDIT_MATURITY_STATUS = "mature"
CREDIT_RUN_STATUSES = frozenset({"draft", "reviewed", "void"})
CREDIT_STATUSES = frozenset({"draft", "reviewed", "void"})

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_READONLY_SQL_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|VACUUM|CALL"
    r")\b",
    re.IGNORECASE,
)

_CREDIT_INSERT_TABLES = frozenset(
    {
        "sc_node_reward_credit_runs",
        "sc_node_reward_credits",
        "sc_node_reward_credit_run_events",
    }
)


@dataclass(frozen=True)
class CreditCoverage:
    coverage_start: datetime
    coverage_end: datetime
    pool_coverage_start: datetime | None
    pool_coverage_end: datetime | None
    reward_coverage_start: datetime | None
    reward_coverage_end: datetime | None
    coverage_gap: bool
    operator_selected: bool


@dataclass(frozen=True)
class ScNodeCreditPreview:
    sc_node_id: str
    sc_node_display_name: str | None
    work_delta_total: Decimal
    work_share: Decimal
    credit_amount: Decimal


@dataclass(frozen=True)
class UnmappedWorkPreview:
    work_delta_total: Decimal
    accepted_delta_total: Decimal
    delta_rows: int


@dataclass(frozen=True)
class CreditRunPreview:
    wallet_name: str
    coverage: CreditCoverage
    reward_event_count: int
    reward_amount_total: Decimal
    mapped_work_total: Decimal
    unmapped_work: UnmappedWorkPreview
    sc_node_credits: tuple[ScNodeCreditPreview, ...]
    allocation_allowed: bool
    refusal_reason: str | None


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_insert_sql_targets_credit_tables_only(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "insert into" not in lowered:
        raise ValueError("insert SQL must contain INSERT INTO")
    for token in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
        if token not in _CREDIT_INSERT_TABLES:
            raise ValueError(f"insert SQL must not target table: {token}")


def normalize_wallet_name(value: str) -> str:
    trimmed = str(value).strip()
    if not trimmed:
        raise ValueError("wallet_name is required")
    return trimmed


def parse_coverage_timestamp(value: str, *, field_name: str) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_pool_work_coverage_sql() -> str:
    sql = """
SELECT
  MIN(observed_from) AS pool_coverage_start,
  MAX(observed_to) AS pool_coverage_end
FROM pool_share_work_deltas
WHERE observed_from IS NOT NULL
  AND observed_to IS NOT NULL
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_mature_reward_coverage_sql() -> str:
    sql = """
SELECT
  MIN(event_time) AS reward_coverage_start,
  MAX(event_time) AS reward_coverage_end
FROM support_wallet_reward_events
WHERE wallet_name = %(wallet_name)s
  AND maturity_status = 'mature'
  AND event_time IS NOT NULL
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_eligible_mature_rewards_sql() -> str:
    sql = """
SELECT
  id AS reward_event_id,
  txid,
  amount,
  event_time
FROM support_wallet_reward_events
WHERE wallet_name = %(wallet_name)s
  AND maturity_status = 'mature'
  AND event_time IS NOT NULL
  AND event_time < %(coverage_end)s
  AND (
    (
      %(exclude_coverage_start_boundary)s IS FALSE
      AND event_time >= %(coverage_start)s
    )
    OR (
      %(exclude_coverage_start_boundary)s IS TRUE
      AND event_time > %(coverage_start)s
    )
  )
ORDER BY event_time, id
""".strip()
    _assert_readonly_sql(sql)
    assert "maturity_status = 'mature'" in sql
    assert "event_time < %(coverage_end)s" in sql
    return sql


def build_prior_credit_run_coverage_end_match_sql() -> str:
    sql = """
SELECT EXISTS (
  SELECT 1
  FROM sc_node_reward_credit_runs r
  WHERE r.wallet_name = %(wallet_name)s
    AND r.coverage_end = %(coverage_start)s
) AS exclude_coverage_start_boundary
""".strip()
    _assert_readonly_sql(sql)
    return sql


def reward_event_time_in_coverage(
    event_time: datetime,
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    exclude_coverage_start_boundary: bool = False,
) -> bool:
    if event_time >= coverage_end:
        return False
    if exclude_coverage_start_boundary:
        return event_time > coverage_start
    return event_time >= coverage_start


def build_sc_node_work_share_sql() -> str:
    sql = """
SELECT
  d.sc_node_id,
  n.display_name AS sc_node_display_name,
  COALESCE(SUM(d.work_delta), 0) AS work_delta_total
FROM pool_share_work_deltas d
LEFT JOIN sc_nodes n ON n.id = d.sc_node_id
WHERE d.sc_node_id IS NOT NULL
  AND d.observed_from < %(coverage_end)s
  AND d.observed_to > %(coverage_start)s
GROUP BY d.sc_node_id, n.display_name
ORDER BY work_delta_total DESC, d.sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    assert "observed_from" in sql
    assert "observed_to" in sql
    assert "observed_at" not in sql
    assert "sc_node_id IS NOT NULL" in sql
    return sql


def build_unmapped_work_sql() -> str:
    sql = """
SELECT
  COALESCE(SUM(work_delta), 0) AS work_delta_total,
  COALESCE(SUM(accepted_delta), 0) AS accepted_delta_total,
  COUNT(*)::bigint AS delta_rows
FROM pool_share_work_deltas
WHERE sc_node_id IS NULL
  AND observed_from < %(coverage_end)s
  AND observed_to > %(coverage_start)s
""".strip()
    _assert_readonly_sql(sql)
    assert "sc_node_id IS NULL" in sql
    assert "observed_from" in sql
    assert "observed_to" in sql
    return sql


def build_insert_credit_run_sql() -> str:
    sql = """
INSERT INTO sc_node_reward_credit_runs (
  run_label,
  wallet_name,
  maturity_status,
  coverage_start,
  coverage_end,
  reward_event_count,
  reward_amount_total,
  mapped_work_total,
  unmapped_work_total,
  status,
  notes
) VALUES (
  %(run_label)s,
  %(wallet_name)s,
  %(maturity_status)s,
  %(coverage_start)s,
  %(coverage_end)s,
  %(reward_event_count)s,
  %(reward_amount_total)s,
  %(mapped_work_total)s,
  %(unmapped_work_total)s,
  %(status)s,
  %(notes)s
)
RETURNING id
""".strip()
    _assert_insert_sql_targets_credit_tables_only(sql)
    return sql


def build_insert_credit_sql() -> str:
    sql = """
INSERT INTO sc_node_reward_credits (
  credit_run_id,
  sc_node_id,
  reward_amount_total,
  work_delta_total,
  work_share,
  credit_amount,
  credit_status
) VALUES (
  %(credit_run_id)s,
  %(sc_node_id)s,
  %(reward_amount_total)s,
  %(work_delta_total)s,
  %(work_share)s,
  %(credit_amount)s,
  %(credit_status)s
)
""".strip()
    _assert_insert_sql_targets_credit_tables_only(sql)
    return sql


def build_insert_credit_run_event_sql() -> str:
    sql = """
INSERT INTO sc_node_reward_credit_run_events (
  credit_run_id,
  reward_event_id
) VALUES (
  %(credit_run_id)s,
  %(reward_event_id)s
)
""".strip()
    _assert_insert_sql_targets_credit_tables_only(sql)
    return sql


def build_existing_reward_event_links_sql() -> str:
    sql = """
SELECT
  e.reward_event_id,
  e.credit_run_id
FROM sc_node_reward_credit_run_events e
WHERE e.reward_event_id = ANY(%(reward_event_ids)s)
ORDER BY e.reward_event_id, e.credit_run_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_payout_plans_for_credit_run_sql() -> str:
    sql = """
SELECT
  id,
  credit_run_id,
  status
FROM sc_node_payout_plans
WHERE credit_run_id = %(credit_run_id)s
ORDER BY id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_executions_for_credit_run_sql() -> str:
    sql = """
SELECT
  pe.id,
  pe.status,
  pp.credit_run_id
FROM sc_node_payout_production_executions pe
JOIN sc_node_payout_plans pp ON pp.id = pe.payout_plan_id
WHERE pp.credit_run_id = %(credit_run_id)s
ORDER BY pe.id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_credit_runs_sql() -> str:
    sql = """
SELECT
  id,
  run_label,
  wallet_name,
  maturity_status,
  coverage_start,
  coverage_end,
  reward_event_count,
  reward_amount_total,
  mapped_work_total,
  unmapped_work_total,
  status,
  notes,
  created_at,
  updated_at
FROM sc_node_reward_credit_runs
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_credit_run_details_sql(credit_run_id: int) -> str:
    safe_id = int(credit_run_id)
    sql = f"""
SELECT
  r.id,
  r.run_label,
  r.wallet_name,
  r.maturity_status,
  r.coverage_start,
  r.coverage_end,
  r.reward_event_count,
  r.reward_amount_total,
  r.mapped_work_total,
  r.unmapped_work_total,
  r.status,
  r.notes,
  r.created_at,
  r.updated_at
FROM sc_node_reward_credit_runs r
WHERE r.id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_credit_run_credits_sql(credit_run_id: int) -> str:
    safe_id = int(credit_run_id)
    sql = f"""
SELECT
  c.id,
  c.credit_run_id,
  c.sc_node_id,
  n.display_name AS sc_node_display_name,
  c.reward_amount_total,
  c.work_delta_total,
  c.work_share,
  c.credit_amount,
  c.credit_status,
  c.created_at,
  c.updated_at
FROM sc_node_reward_credits c
LEFT JOIN sc_nodes n ON n.id = c.sc_node_id
WHERE c.credit_run_id = {safe_id}
ORDER BY c.credit_amount DESC, c.sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_credit_run_events_sql(credit_run_id: int) -> str:
    safe_id = int(credit_run_id)
    sql = f"""
SELECT
  e.id,
  e.credit_run_id,
  e.reward_event_id,
  r.txid,
  r.amount,
  r.event_time,
  r.maturity_status,
  e.created_at
FROM sc_node_reward_credit_run_events e
JOIN support_wallet_reward_events r ON r.id = e.reward_event_id
WHERE e.credit_run_id = {safe_id}
ORDER BY r.event_time, e.reward_event_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def resolve_default_coverage(
    *,
    pool_coverage_start: datetime | None,
    pool_coverage_end: datetime | None,
    reward_coverage_start: datetime | None,
    reward_coverage_end: datetime | None,
) -> CreditCoverage | None:
    if pool_coverage_start is None or pool_coverage_end is None:
        return None
    if reward_coverage_start is None or reward_coverage_end is None:
        return None
    coverage_start = max(pool_coverage_start, reward_coverage_start)
    coverage_end = min(pool_coverage_end, reward_coverage_end)
    return CreditCoverage(
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pool_coverage_start=pool_coverage_start,
        pool_coverage_end=pool_coverage_end,
        reward_coverage_start=reward_coverage_start,
        reward_coverage_end=reward_coverage_end,
        coverage_gap=coverage_start >= coverage_end,
        operator_selected=False,
    )


def resolve_operator_coverage(
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    pool_coverage_start: datetime | None,
    pool_coverage_end: datetime | None,
    reward_coverage_start: datetime | None,
    reward_coverage_end: datetime | None,
) -> CreditCoverage:
    if coverage_start >= coverage_end:
        raise ValueError("coverage_start must be before coverage_end")
    return CreditCoverage(
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pool_coverage_start=pool_coverage_start,
        pool_coverage_end=pool_coverage_end,
        reward_coverage_start=reward_coverage_start,
        reward_coverage_end=reward_coverage_end,
        coverage_gap=False,
        operator_selected=True,
    )


def evaluate_write_draft_duplicate_refusal(
    *,
    existing_links: list[Mapping[str, Any]],
    payout_plans: list[Mapping[str, Any]],
    production_executions: list[Mapping[str, Any]],
) -> str | None:
    if not existing_links:
        return None
    first = existing_links[0]
    credit_run_id = _to_int(first.get("credit_run_id"))
    base = f"reward event already linked to credit_run_id={credit_run_id}"
    if production_executions:
        exec_ids = ", ".join(str(_to_int(row.get("id"))) for row in production_executions)
        return (
            f"{base}; credit_run has production execution(s) "
            f"[{exec_ids}] — duplicate draft refused"
        )
    if payout_plans:
        plan_ids = ", ".join(str(_to_int(row.get("id"))) for row in payout_plans)
        return (
            f"{base}; credit_run has payout plan(s) [{plan_ids}] — duplicate draft refused"
        )
    return (
        f"{base}; existing unpaid duplicate draft — manual cleanup required before re-draft"
    )


def evaluate_write_draft_coverage_refusal(
    *,
    coverage: CreditCoverage,
    explicit_coverage: bool,
    allow_default_coverage: bool,
) -> str | None:
    if not explicit_coverage and not allow_default_coverage:
        return (
            "write-draft requires --coverage-start and --coverage-end, "
            "or --allow-default-coverage"
        )
    if coverage.coverage_gap:
        return "coverage gap: selected window is invalid or has no overlap"
    return None


def evaluate_allocation_refusal(
    *,
    reward_event_count: int,
    reward_amount_total: Decimal,
    mapped_work_total: Decimal,
    coverage_gap: bool,
) -> str | None:
    if coverage_gap:
        return "coverage gap: pool telemetry and mature rewards do not overlap"
    if reward_event_count <= 0 or reward_amount_total <= 0:
        return "no eligible mature rewards in coverage window"
    if mapped_work_total <= 0:
        return "mapped_work_total is zero; refusing credit allocation"
    return None


def build_credit_run_preview(
    *,
    wallet_name: str,
    coverage: CreditCoverage,
    reward_rows: list[Mapping[str, Any]],
    sc_node_rows: list[Mapping[str, Any]],
    unmapped_row: Mapping[str, Any] | None,
) -> CreditRunPreview:
    reward_amount_total = sum(
        (_to_decimal(row.get("amount")) for row in reward_rows),
        Decimal("0"),
    )
    reward_event_count = len(reward_rows)
    mapped_work_total = sum(
        (_to_decimal(row.get("work_delta_total")) for row in sc_node_rows),
        Decimal("0"),
    )
    unmapped = UnmappedWorkPreview(
        work_delta_total=_to_decimal(
            unmapped_row.get("work_delta_total") if unmapped_row else 0
        ),
        accepted_delta_total=_to_decimal(
            unmapped_row.get("accepted_delta_total") if unmapped_row else 0
        ),
        delta_rows=_to_int(unmapped_row.get("delta_rows") if unmapped_row else 0),
    )
    refusal_reason = evaluate_allocation_refusal(
        reward_event_count=reward_event_count,
        reward_amount_total=reward_amount_total,
        mapped_work_total=mapped_work_total,
        coverage_gap=coverage.coverage_gap,
    )
    sc_node_credits: list[ScNodeCreditPreview] = []
    if refusal_reason is None and mapped_work_total > 0:
        for row in sc_node_rows:
            work_delta_total = _to_decimal(row.get("work_delta_total"))
            if work_delta_total <= 0:
                continue
            work_share = _quantize_share(work_delta_total / mapped_work_total)
            credit_amount = _quantize_amount(reward_amount_total * work_share)
            sc_node_credits.append(
                ScNodeCreditPreview(
                    sc_node_id=str(row["sc_node_id"]),
                    sc_node_display_name=row.get("sc_node_display_name"),
                    work_delta_total=work_delta_total,
                    work_share=work_share,
                    credit_amount=credit_amount,
                )
            )
    return CreditRunPreview(
        wallet_name=wallet_name,
        coverage=coverage,
        reward_event_count=reward_event_count,
        reward_amount_total=reward_amount_total,
        mapped_work_total=mapped_work_total,
        unmapped_work=unmapped,
        sc_node_credits=tuple(sc_node_credits),
        allocation_allowed=refusal_reason is None,
        refusal_reason=refusal_reason,
    )


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)


def _serialize_numeric(value: Decimal) -> str:
    return format(value, "f")


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def _quantize_share(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)


def credit_coverage_to_dict(coverage: CreditCoverage) -> dict[str, Any]:
    return {
        "coverage_start": _serialize_datetime(coverage.coverage_start),
        "coverage_end": _serialize_datetime(coverage.coverage_end),
        "pool_coverage_start": _serialize_datetime(coverage.pool_coverage_start),
        "pool_coverage_end": _serialize_datetime(coverage.pool_coverage_end),
        "reward_coverage_start": _serialize_datetime(coverage.reward_coverage_start),
        "reward_coverage_end": _serialize_datetime(coverage.reward_coverage_end),
        "coverage_gap": coverage.coverage_gap,
        "operator_selected": coverage.operator_selected,
    }


def credit_run_preview_to_dict(preview: CreditRunPreview) -> dict[str, Any]:
    return {
        "wallet_name": preview.wallet_name,
        "coverage_start": _serialize_datetime(preview.coverage.coverage_start),
        "coverage_end": _serialize_datetime(preview.coverage.coverage_end),
        "coverage": credit_coverage_to_dict(preview.coverage),
        "reward_event_count": preview.reward_event_count,
        "reward_amount_total": _serialize_numeric(preview.reward_amount_total),
        "mapped_work_total": _serialize_numeric(preview.mapped_work_total),
        "unmapped_work_total": _serialize_numeric(preview.unmapped_work.work_delta_total),
        "unmapped_work": {
            "work_delta_total": _serialize_numeric(preview.unmapped_work.work_delta_total),
            "accepted_delta_total": _serialize_numeric(
                preview.unmapped_work.accepted_delta_total
            ),
            "delta_rows": preview.unmapped_work.delta_rows,
        },
        "sc_node_credits": [
            {
                "sc_node_id": credit.sc_node_id,
                "sc_node_display_name": credit.sc_node_display_name,
                "work_delta_total": _serialize_numeric(credit.work_delta_total),
                "work_share": _serialize_numeric(credit.work_share),
                "credit_amount": _serialize_numeric(credit.credit_amount),
            }
            for credit in preview.sc_node_credits
        ],
        "allocation_allowed": preview.allocation_allowed,
        "refusal_reason": preview.refusal_reason,
    }


def row_to_credit_run_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _to_int(row["id"]),
        "run_label": row.get("run_label"),
        "wallet_name": row.get("wallet_name"),
        "maturity_status": row.get("maturity_status"),
        "coverage_start": _serialize_datetime(row.get("coverage_start")),
        "coverage_end": _serialize_datetime(row.get("coverage_end")),
        "reward_event_count": _to_int(row.get("reward_event_count")),
        "reward_amount_total": _serialize_numeric(_to_decimal(row.get("reward_amount_total"))),
        "mapped_work_total": _serialize_numeric(_to_decimal(row.get("mapped_work_total"))),
        "unmapped_work_total": _serialize_numeric(_to_decimal(row.get("unmapped_work_total"))),
        "status": row.get("status"),
        "notes": row.get("notes"),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }


def row_to_credit_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _to_int(row["id"]),
        "credit_run_id": _to_int(row.get("credit_run_id")),
        "sc_node_id": str(row["sc_node_id"]),
        "sc_node_display_name": row.get("sc_node_display_name"),
        "reward_amount_total": _serialize_numeric(_to_decimal(row.get("reward_amount_total"))),
        "work_delta_total": _serialize_numeric(_to_decimal(row.get("work_delta_total"))),
        "work_share": _serialize_numeric(_to_decimal(row.get("work_share"))),
        "credit_amount": _serialize_numeric(_to_decimal(row.get("credit_amount"))),
        "credit_status": row.get("credit_status"),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }


def row_to_credit_run_event_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _to_int(row["id"]),
        "credit_run_id": _to_int(row.get("credit_run_id")),
        "reward_event_id": _to_int(row.get("reward_event_id")),
        "txid": row.get("txid"),
        "amount": _serialize_numeric(_to_decimal(row.get("amount"))),
        "event_time": _serialize_datetime(row.get("event_time")),
        "maturity_status": row.get("maturity_status"),
        "created_at": _serialize_datetime(row.get("created_at")),
    }
