from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_planner as planner

DEFAULT_RESERVE_PERCENT = Decimal("0.500000")
DEFAULT_MAX_SPEND_PERCENT = Decimal("0.500000")
DEFAULT_TARGET_SINGLE_TX_MAX_AMOUNT = Decimal("500")
DEFAULT_FALLBACK_CHUNK_AMOUNT = Decimal("25")
WALLET_BALANCE_SOURCE_AZC_GETBALANCES = "azc_getbalances"
WALLET_UTXO_SOURCE_AZC_LISTUNSPENT = "azc_listunspent"
WALLET_UTXO_SOURCE_UNAVAILABLE = "unavailable"

FRAGMENTATION_RISK_LOW = "LOW"
FRAGMENTATION_RISK_MEDIUM = "MEDIUM"
FRAGMENTATION_RISK_HIGH = "HIGH"
FRAGMENTATION_RISK_UNKNOWN = "UNKNOWN"

RECOMMENDED_EXECUTION_MODE_SINGLE = "single"
RECOMMENDED_EXECUTION_MODE_CHUNKED = "chunked"
RECOMMENDED_EXECUTION_MODE_HALT = "halt"

READONLY_WALLET_RPC_ALLOWLIST = frozenset({"getbalances", "listunspent"})

_LOW_INPUT_COUNT_THRESHOLD = 2
_MEDIUM_INPUT_COUNT_THRESHOLD = 8
_HIGH_UTXO_COUNT_THRESHOLD = 50

RESERVE_MODE_PERCENT = "percent"
RESERVE_MODE_AMOUNT = "amount"

PREFLIGHT_STATUS_DRAFT = "draft"
PREFLIGHT_STATUS_PASSED = "passed"
PREFLIGHT_STATUS_REFUSED = "refused"
PREFLIGHT_STATUS_VOID = "void"

ROW_STATUS_CHECKED = "checked"
ROW_STATUS_REFUSED = "refused"

_PRODUCTION_PREFLIGHT_INSERT_TABLES = frozenset(
    {
        "sc_node_payout_production_preflights",
        "sc_node_payout_production_preflight_rows",
    }
)

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|importprivkey|importmulti|settxfee|bumpfee|"
    r"privkey"
    r")\b",
    re.IGNORECASE,
)

_FORBIDDEN_READONLY_WALLET_RPC = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|importprivkey|importmulti|importwallet|"
    r"settxfee|bumpfee|encryptwallet|backupwallet|dumpwallet|privkey"
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


@dataclass(frozen=True)
class WalletBalance:
    trusted: Decimal
    immature: Decimal


@dataclass(frozen=True)
class ProductionPayoutPreflightRow:
    payout_plan_row_id: int
    sc_node_id: str
    payout_address: str
    payout_amount: Decimal
    registry_payout_address: str | None
    row_status: str
    refusal_reason: str | None


@dataclass(frozen=True)
class UtxoSnapshot:
    evidence_available: bool
    utxo_count: int
    max_observed_utxo_amount: Decimal | None
    utxo_amounts: tuple[Decimal, ...]
    wallet_utxo_source: str
    evidence_unavailable_reason: str | None


@dataclass(frozen=True)
class UtxoChunkingPolicy:
    spendable_balance: Decimal
    planned_payout_amount: Decimal
    reserve_requirement: Decimal
    available_after_reserve: Decimal
    utxo_count: int | None
    max_observed_utxo_amount: Decimal | None
    target_single_tx_max_amount: Decimal
    fallback_chunk_amount: Decimal
    recommended_chunk_size: Decimal
    estimated_chunk_count: int
    fragmentation_risk: str
    recommended_execution_mode: str
    refusal_reason: str | None
    wallet_utxo_source: str
    utxo_evidence_note: str | None


@dataclass(frozen=True)
class ProductionPayoutPreflightPreview:
    payout_plan_id: int
    source_wallet_name: str
    execution_allowed: bool
    refusal_reason: str | None
    wallet_balance: WalletBalance
    planned_amount_total: Decimal
    reserve_mode: str
    reserve_percent: Decimal
    reserve_amount: Decimal
    spendable_after_reserve: Decimal
    max_spend_percent: Decimal
    max_spend_allowed: Decimal
    operator_override: bool
    row_count: int
    rows: tuple[ProductionPayoutPreflightRow, ...]
    utxo_chunking_policy: UtxoChunkingPolicy


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def assert_allowed_readonly_wallet_rpc(method: str) -> None:
    normalized = str(method).strip().lower()
    if not normalized:
        raise ValueError("wallet RPC method is required")
    if _FORBIDDEN_READONLY_WALLET_RPC.search(normalized):
        raise ValueError(f"wallet RPC method is forbidden: {normalized}")
    if normalized not in READONLY_WALLET_RPC_ALLOWLIST:
        raise ValueError(f"wallet RPC method is not allowlisted: {normalized}")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_production_preflight_insert_sql(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "insert into" not in lowered:
        raise ValueError("production preflight SQL must INSERT")
    for token in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
        if token not in _PRODUCTION_PREFLIGHT_INSERT_TABLES:
            raise ValueError(f"production preflight SQL must not target table: {token}")


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def utxo_snapshot_unavailable(reason: str) -> UtxoSnapshot:
    return UtxoSnapshot(
        evidence_available=False,
        utxo_count=0,
        max_observed_utxo_amount=None,
        utxo_amounts=(),
        wallet_utxo_source=WALLET_UTXO_SOURCE_UNAVAILABLE,
        evidence_unavailable_reason=reason,
    )


def parse_listunspent_payload(payload: Any) -> UtxoSnapshot:
    if not isinstance(payload, list):
        raise ValueError("listunspent payload must be a JSON array")
    amounts: list[Decimal] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        spendable = item.get("spendable")
        if spendable is False:
            continue
        amount = _quantize_amount(planner._to_decimal(item.get("amount")))
        if amount <= 0:
            continue
        amounts.append(amount)
    if not amounts:
        return utxo_snapshot_unavailable("listunspent returned no spendable UTXOs")
    return UtxoSnapshot(
        evidence_available=True,
        utxo_count=len(amounts),
        max_observed_utxo_amount=max(amounts),
        utxo_amounts=tuple(sorted(amounts, reverse=True)),
        wallet_utxo_source=WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
        evidence_unavailable_reason=None,
    )


def estimate_input_count_for_amount(
    amount: Decimal,
    utxo_amounts: tuple[Decimal, ...],
) -> int:
    if amount <= 0:
        return 0
    if not utxo_amounts:
        return 0
    remaining = _quantize_amount(amount)
    count = 0
    for utxo_amount in utxo_amounts:
        if remaining <= 0:
            break
        count += 1
        remaining = _quantize_amount(remaining - utxo_amount)
    if remaining > 0:
        return len(utxo_amounts) + 1
    return count


def assess_fragmentation_risk(
    *,
    planned_amount: Decimal,
    utxo_snapshot: UtxoSnapshot,
) -> str:
    if not utxo_snapshot.evidence_available:
        return FRAGMENTATION_RISK_UNKNOWN
    if planned_amount <= 0:
        return FRAGMENTATION_RISK_UNKNOWN
    if utxo_snapshot.utxo_count <= 0:
        return FRAGMENTATION_RISK_UNKNOWN
    estimated_inputs = estimate_input_count_for_amount(
        planned_amount,
        utxo_snapshot.utxo_amounts,
    )
    max_utxo = utxo_snapshot.max_observed_utxo_amount
    if estimated_inputs > _MEDIUM_INPUT_COUNT_THRESHOLD:
        return FRAGMENTATION_RISK_HIGH
    if max_utxo is not None and planned_amount > max_utxo and estimated_inputs > 1:
        return FRAGMENTATION_RISK_HIGH
    if estimated_inputs > _LOW_INPUT_COUNT_THRESHOLD:
        return FRAGMENTATION_RISK_MEDIUM
    if (
        utxo_snapshot.utxo_count > _HIGH_UTXO_COUNT_THRESHOLD
        and estimated_inputs > 1
    ):
        return FRAGMENTATION_RISK_MEDIUM
    return FRAGMENTATION_RISK_LOW


def estimate_total_chunk_count(
    plan_rows: list[Mapping[str, Any]],
    chunk_size: Decimal,
) -> int:
    from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked_executor

    total = 0
    for row in plan_rows:
        amounts = chunked_executor.split_payout_amount_into_chunks(
            planner._to_decimal(row.get("payout_amount")),
            chunk_size,
        )
        total += len(amounts)
    return total


def build_utxo_chunking_policy(
    *,
    wallet_balance: WalletBalance,
    planned_amount_total: Decimal,
    reserve_amount: Decimal,
    spendable_after_reserve: Decimal,
    plan_rows: list[Mapping[str, Any]],
    utxo_snapshot: UtxoSnapshot,
    balance_execution_allowed: bool,
    balance_refusal_reason: str | None,
    target_single_tx_max_amount: Decimal = DEFAULT_TARGET_SINGLE_TX_MAX_AMOUNT,
    fallback_chunk_amount: Decimal = DEFAULT_FALLBACK_CHUNK_AMOUNT,
) -> UtxoChunkingPolicy:
    target_single = _quantize_amount(Decimal(str(target_single_tx_max_amount)))
    fallback_chunk = _quantize_amount(Decimal(str(fallback_chunk_amount)))
    if fallback_chunk <= 0:
        raise ValueError("fallback_chunk_amount must be greater than zero")
    if target_single <= 0:
        raise ValueError("target_single_tx_max_amount must be greater than zero")

    recommended_chunk_size = fallback_chunk
    if (
        utxo_snapshot.evidence_available
        and utxo_snapshot.max_observed_utxo_amount is not None
        and utxo_snapshot.max_observed_utxo_amount > 0
        and utxo_snapshot.max_observed_utxo_amount < fallback_chunk
    ):
        recommended_chunk_size = utxo_snapshot.max_observed_utxo_amount

    estimated_chunks = estimate_total_chunk_count(plan_rows, recommended_chunk_size)
    fragmentation_risk = assess_fragmentation_risk(
        planned_amount=planned_amount_total,
        utxo_snapshot=utxo_snapshot,
    )

    utxo_evidence_note: str | None = None
    if not utxo_snapshot.evidence_available:
        reason = utxo_snapshot.evidence_unavailable_reason or "UTXO evidence unavailable"
        utxo_evidence_note = (
            f"UTXO evidence is missing ({reason}); "
            "automation must not assume safe single-send"
        )

    policy_refusal: str | None = None
    recommended_mode = RECOMMENDED_EXECUTION_MODE_SINGLE

    if not balance_execution_allowed:
        recommended_mode = RECOMMENDED_EXECUTION_MODE_HALT
        policy_refusal = balance_refusal_reason
    elif planned_amount_total > target_single:
        recommended_mode = RECOMMENDED_EXECUTION_MODE_CHUNKED
        policy_refusal = (
            "planned payout exceeds target_single_tx_max_amount "
            f"({planner._serialize_numeric(target_single)}); "
            "single-send is not recommended"
        )
    elif fragmentation_risk == FRAGMENTATION_RISK_UNKNOWN:
        recommended_mode = RECOMMENDED_EXECUTION_MODE_CHUNKED
    elif fragmentation_risk in {FRAGMENTATION_RISK_MEDIUM, FRAGMENTATION_RISK_HIGH}:
        recommended_mode = RECOMMENDED_EXECUTION_MODE_CHUNKED
        if fragmentation_risk == FRAGMENTATION_RISK_HIGH:
            policy_refusal = (
                "high UTXO fragmentation risk; single-send may fail with "
                "transaction too large"
            )
    elif planned_amount_total <= target_single:
        recommended_mode = RECOMMENDED_EXECUTION_MODE_SINGLE

    return UtxoChunkingPolicy(
        spendable_balance=wallet_balance.trusted,
        planned_payout_amount=planned_amount_total,
        reserve_requirement=reserve_amount,
        available_after_reserve=spendable_after_reserve,
        utxo_count=utxo_snapshot.utxo_count if utxo_snapshot.evidence_available else None,
        max_observed_utxo_amount=utxo_snapshot.max_observed_utxo_amount,
        target_single_tx_max_amount=target_single,
        fallback_chunk_amount=fallback_chunk,
        recommended_chunk_size=recommended_chunk_size,
        estimated_chunk_count=estimated_chunks,
        fragmentation_risk=fragmentation_risk,
        recommended_execution_mode=recommended_mode,
        refusal_reason=policy_refusal,
        wallet_utxo_source=utxo_snapshot.wallet_utxo_source,
        utxo_evidence_note=utxo_evidence_note,
    )


def utxo_chunking_policy_to_dict(policy: UtxoChunkingPolicy) -> dict[str, Any]:
    return {
        "spendable_balance": planner._serialize_numeric(policy.spendable_balance),
        "planned_payout_amount": planner._serialize_numeric(policy.planned_payout_amount),
        "reserve_requirement": planner._serialize_numeric(policy.reserve_requirement),
        "available_after_reserve": planner._serialize_numeric(policy.available_after_reserve),
        "utxo_count": policy.utxo_count,
        "max_observed_utxo_amount": (
            planner._serialize_numeric(policy.max_observed_utxo_amount)
            if policy.max_observed_utxo_amount is not None
            else None
        ),
        "target_single_tx_max_amount": planner._serialize_numeric(
            policy.target_single_tx_max_amount
        ),
        "fallback_chunk_amount": planner._serialize_numeric(policy.fallback_chunk_amount),
        "recommended_chunk_size": planner._serialize_numeric(policy.recommended_chunk_size),
        "estimated_chunk_count": policy.estimated_chunk_count,
        "fragmentation_risk": policy.fragmentation_risk,
        "recommended_execution_mode": policy.recommended_execution_mode,
        "refusal_reason": policy.refusal_reason,
        "wallet_utxo_source": policy.wallet_utxo_source,
        "utxo_evidence_note": policy.utxo_evidence_note,
        "policy_note": (
            "fallback_chunk_amount is a safety default for chunked execution, "
            "not a protocol or business max; up to "
            f"{planner._serialize_numeric(policy.target_single_tx_max_amount)} AZC "
            "single-send is allowed only when UTXO/transaction-size policy says safe"
        ),
    }


def parse_wallet_balance_from_getbalances(payload: Mapping[str, Any]) -> WalletBalance:
    mine = payload.get("mine")
    if not isinstance(mine, Mapping):
        raise ValueError("getbalances payload must include mine object")
    trusted = planner._to_decimal(mine.get("trusted"))
    immature = planner._to_decimal(mine.get("immature"))
    if trusted < 0 or immature < 0:
        raise ValueError("trusted and immature balances must be non-negative")
    return WalletBalance(trusted=_quantize_amount(trusted), immature=_quantize_amount(immature))


def calculate_reserve(
    trusted_balance: Decimal,
    *,
    reserve_percent: Decimal | None = None,
    reserve_amount: Decimal | None = None,
    max_spend_percent: Decimal = DEFAULT_MAX_SPEND_PERCENT,
    reserve_mode: str = RESERVE_MODE_PERCENT,
) -> dict[str, Decimal]:
    percent = (
        DEFAULT_RESERVE_PERCENT if reserve_percent is None else Decimal(str(reserve_percent))
    )
    if percent < 0 or percent > 1:
        raise ValueError("reserve_percent must be between 0 and 1")
    max_percent = Decimal(str(max_spend_percent))
    if max_percent < 0 or max_percent > 1:
        raise ValueError("max_spend_percent must be between 0 and 1")

    if reserve_mode == RESERVE_MODE_AMOUNT and reserve_amount is not None:
        reserve = _quantize_amount(Decimal(str(reserve_amount)))
        mode = RESERVE_MODE_AMOUNT
    else:
        reserve = _quantize_amount(trusted_balance * percent)
        mode = RESERVE_MODE_PERCENT

    spendable_after_reserve = _quantize_amount(trusted_balance - reserve)
    if spendable_after_reserve < 0:
        spendable_after_reserve = Decimal("0")
    max_spend_allowed = _quantize_amount(trusted_balance * max_percent)

    return {
        "reserve_percent": percent,
        "reserve_amount": reserve,
        "spendable_after_reserve": spendable_after_reserve,
        "max_spend_percent": max_percent,
        "max_spend_allowed": max_spend_allowed,
        "reserve_mode_label": mode,
    }


def normalize_source_wallet_name(value: str) -> str:
    name = str(value).strip()
    if not name:
        raise ValueError("source_wallet_name is required")
    return name


def normalize_idempotency_key(value: str) -> str:
    key = str(value).strip()
    if not key:
        raise ValueError("idempotency_key is required")
    return key


def build_approved_payout_plan_sql(payout_plan_id: int) -> str:
    safe_id = int(payout_plan_id)
    sql = f"""
SELECT
  id,
  credit_run_id,
  wallet_name,
  status,
  reserve_fraction,
  trusted_balance_snapshot,
  reserve_amount,
  max_spendable_amount,
  planned_amount_total,
  row_count,
  notes,
  approved_at,
  approved_by,
  approval_note,
  approval_confirmation_hash,
  preflight_checked_at,
  preflight_status,
  preflight_note,
  cancelled_at,
  cancelled_by,
  cancellation_note,
  created_at,
  updated_at
FROM sc_node_payout_plans
WHERE id = {safe_id}
  AND status = 'approved'
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_approved_payout_plan_rows_sql(payout_plan_id: int) -> str:
    safe_id = int(payout_plan_id)
    sql = f"""
SELECT
  r.id,
  r.payout_plan_id,
  r.credit_id,
  r.sc_node_id,
  n.display_name AS sc_node_display_name,
  r.payout_address,
  r.payout_amount,
  r.row_status,
  r.created_at,
  r.updated_at
FROM sc_node_payout_plan_rows r
LEFT JOIN sc_nodes n ON n.id = r.sc_node_id
WHERE r.payout_plan_id = {safe_id}
  AND r.row_status = 'approved'
ORDER BY r.payout_amount DESC, r.sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_approved_payout_plan_rows_with_active_address_sql(payout_plan_id: int) -> str:
    safe_id = int(payout_plan_id)
    sql = f"""
SELECT
  r.id,
  r.payout_plan_id,
  r.sc_node_id,
  r.payout_address AS plan_payout_address,
  r.payout_amount,
  r.row_status,
  a.payout_address AS registry_payout_address,
  a.status AS registry_address_status,
  a.is_default AS registry_is_default
FROM sc_node_payout_plan_rows r
LEFT JOIN sc_node_payout_addresses a
  ON a.sc_node_id = r.sc_node_id
 AND a.is_default = true
 AND a.status = 'active'
WHERE r.payout_plan_id = {safe_id}
  AND r.row_status = 'approved'
ORDER BY r.payout_amount DESC, r.sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_existing_active_production_preflight_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  source_wallet_name,
  preflight_status,
  execution_allowed,
  refusal_reason,
  trusted_balance,
  immature_balance,
  planned_amount_total,
  reserve_mode,
  reserve_percent,
  reserve_amount,
  spendable_after_reserve,
  max_spend_percent,
  operator_override,
  wallet_balance_source,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_preflights
WHERE payout_plan_id = %(payout_plan_id)s
  AND preflight_status = 'passed'
  AND execution_allowed = true
ORDER BY id DESC
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_preflight_by_idempotency_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  source_wallet_name,
  preflight_status,
  execution_allowed,
  refusal_reason,
  trusted_balance,
  immature_balance,
  planned_amount_total,
  reserve_mode,
  reserve_percent,
  reserve_amount,
  spendable_after_reserve,
  max_spend_percent,
  operator_override,
  wallet_balance_source,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_preflights
WHERE payout_plan_id = %(payout_plan_id)s
  AND idempotency_key = %(idempotency_key)s
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_insert_production_preflight_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_production_preflights (
  payout_plan_id,
  source_wallet_name,
  preflight_status,
  execution_allowed,
  refusal_reason,
  trusted_balance,
  immature_balance,
  planned_amount_total,
  reserve_mode,
  reserve_percent,
  reserve_amount,
  spendable_after_reserve,
  max_spend_percent,
  operator_override,
  wallet_balance_source,
  idempotency_key,
  notes
) VALUES (
  %(payout_plan_id)s,
  %(source_wallet_name)s,
  %(preflight_status)s,
  %(execution_allowed)s,
  %(refusal_reason)s,
  %(trusted_balance)s,
  %(immature_balance)s,
  %(planned_amount_total)s,
  %(reserve_mode)s,
  %(reserve_percent)s,
  %(reserve_amount)s,
  %(spendable_after_reserve)s,
  %(max_spend_percent)s,
  %(operator_override)s,
  %(wallet_balance_source)s,
  %(idempotency_key)s,
  %(notes)s
)
RETURNING id
""".strip()
    _assert_production_preflight_insert_sql(sql)
    return sql


def build_insert_production_preflight_row_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_production_preflight_rows (
  production_preflight_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  refusal_reason
) VALUES (
  %(production_preflight_id)s,
  %(payout_plan_row_id)s,
  %(sc_node_id)s,
  %(payout_address)s,
  %(payout_amount)s,
  %(row_status)s,
  %(refusal_reason)s
)
""".strip()
    _assert_production_preflight_insert_sql(sql)
    return sql


def build_production_preflights_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  source_wallet_name,
  preflight_status,
  execution_allowed,
  refusal_reason,
  trusted_balance,
  immature_balance,
  planned_amount_total,
  reserve_mode,
  reserve_percent,
  reserve_amount,
  spendable_after_reserve,
  max_spend_percent,
  operator_override,
  wallet_balance_source,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_preflights
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_preflight_details_sql(production_preflight_id: int) -> str:
    safe_id = int(production_preflight_id)
    sql = f"""
SELECT
  id,
  payout_plan_id,
  source_wallet_name,
  preflight_status,
  execution_allowed,
  refusal_reason,
  trusted_balance,
  immature_balance,
  planned_amount_total,
  reserve_mode,
  reserve_percent,
  reserve_amount,
  spendable_after_reserve,
  max_spend_percent,
  operator_override,
  wallet_balance_source,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_preflights
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_preflight_rows_sql(production_preflight_id: int) -> str:
    safe_id = int(production_preflight_id)
    sql = f"""
SELECT
  id,
  production_preflight_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  refusal_reason,
  created_at,
  updated_at
FROM sc_node_payout_production_preflight_rows
WHERE production_preflight_id = {safe_id}
ORDER BY payout_amount DESC, sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def evaluate_row_address(
    *,
    plan_row: Mapping[str, Any],
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> tuple[str, str | None, str | None]:
    sc_node_id = str(plan_row["sc_node_id"])
    frozen = str(plan_row["payout_address"])
    address_rows = address_lookup.get(sc_node_id, [])
    registry_address, lookup_refusal = planner.resolve_active_default_payout_address(
        address_rows,
        sc_node_id=sc_node_id,
    )
    if lookup_refusal:
        return ROW_STATUS_REFUSED, None, lookup_refusal
    drift = plan_review.evaluate_address_drift(
        sc_node_id=sc_node_id,
        frozen_address=frozen,
        registry_address=registry_address,
    )
    if drift:
        return ROW_STATUS_REFUSED, registry_address, drift
    return ROW_STATUS_CHECKED, registry_address, None


def evaluate_production_preflight_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    wallet_balance: WalletBalance,
    planned_amount_total: Decimal,
    reserve_math: Mapping[str, Decimal],
    operator_override: bool,
    row_refusals: list[str],
) -> str | None:
    if plan is None:
        return "approved payout plan not found"
    if str(plan.get("status")) != plan_review.PLAN_STATUS_APPROVED:
        return "payout plan status must be approved"
    if not plan_rows:
        return "payout plan has no approved rows"
    for row in plan_rows:
        if str(row.get("row_status")) != plan_review.ROW_STATUS_APPROVED:
            return "all payout plan rows must be approved"
    if planned_amount_total <= 0:
        return "planned_amount_total must be greater than zero"
    if wallet_balance.trusted <= 0:
        return "trusted wallet balance must be greater than zero"
    if row_refusals:
        return "; ".join(row_refusals)
    if planned_amount_total > wallet_balance.trusted:
        return (
            "planned_amount_total exceeds current trusted wallet balance "
            f"({planner._serialize_numeric(wallet_balance.trusted)})"
        )
    spendable = planner._to_decimal(reserve_math.get("spendable_after_reserve"))
    max_allowed = planner._to_decimal(reserve_math.get("max_spend_allowed"))
    if not operator_override:
        if planned_amount_total > spendable:
            return (
                "planned_amount_total exceeds spendable_after_reserve "
                f"({planner._serialize_numeric(spendable)})"
            )
        if planned_amount_total > max_allowed:
            return (
                "planned_amount_total exceeds max_spend_percent cap "
                f"({planner._serialize_numeric(max_allowed)})"
            )
    return None


def build_production_preflight_preview(
    *,
    payout_plan_id: int,
    source_wallet_name: str,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    wallet_balance: WalletBalance,
    address_lookup: dict[str, list[Mapping[str, Any]]],
    operator_override: bool = False,
    reserve_percent: Decimal | None = None,
    reserve_amount: Decimal | None = None,
    max_spend_percent: Decimal = DEFAULT_MAX_SPEND_PERCENT,
    reserve_mode: str = RESERVE_MODE_PERCENT,
    utxo_snapshot: UtxoSnapshot | None = None,
    target_single_tx_max_amount: Decimal = DEFAULT_TARGET_SINGLE_TX_MAX_AMOUNT,
    fallback_chunk_amount: Decimal = DEFAULT_FALLBACK_CHUNK_AMOUNT,
) -> ProductionPayoutPreflightPreview:
    planned_amount_total = (
        planner._to_decimal(plan.get("planned_amount_total"))
        if plan is not None
        else Decimal("0")
    )
    reserve_raw = calculate_reserve(
        wallet_balance.trusted,
        reserve_percent=reserve_percent,
        reserve_amount=reserve_amount,
        max_spend_percent=max_spend_percent,
        reserve_mode=reserve_mode,
    )
    reserve_math = {
        "reserve_percent": reserve_raw["reserve_percent"],
        "reserve_amount": reserve_raw["reserve_amount"],
        "spendable_after_reserve": reserve_raw["spendable_after_reserve"],
        "max_spend_percent": reserve_raw["max_spend_percent"],
        "max_spend_allowed": reserve_raw["max_spend_allowed"],
    }
    mode_label = str(reserve_raw["reserve_mode_label"])

    preview_rows: list[ProductionPayoutPreflightRow] = []
    row_refusals: list[str] = []
    for row in plan_rows:
        row_status, registry_address, row_refusal = evaluate_row_address(
            plan_row=row,
            address_lookup=address_lookup,
        )
        if row_refusal:
            row_refusals.append(row_refusal)
        preview_rows.append(
            ProductionPayoutPreflightRow(
                payout_plan_row_id=planner._to_int(row["id"]),
                sc_node_id=str(row["sc_node_id"]),
                payout_address=str(row["payout_address"]),
                payout_amount=planner._to_decimal(row.get("payout_amount")),
                registry_payout_address=registry_address,
                row_status=row_status,
                refusal_reason=row_refusal,
            )
        )

    refusal = evaluate_production_preflight_refusal(
        plan=plan,
        plan_rows=plan_rows,
        wallet_balance=wallet_balance,
        planned_amount_total=planned_amount_total,
        reserve_math=reserve_math,
        operator_override=operator_override,
        row_refusals=row_refusals,
    )

    snapshot = (
        utxo_snapshot
        if utxo_snapshot is not None
        else utxo_snapshot_unavailable("listunspent not collected")
    )
    utxo_policy = build_utxo_chunking_policy(
        wallet_balance=wallet_balance,
        planned_amount_total=planned_amount_total,
        reserve_amount=reserve_math["reserve_amount"],
        spendable_after_reserve=reserve_math["spendable_after_reserve"],
        plan_rows=plan_rows,
        utxo_snapshot=snapshot,
        balance_execution_allowed=refusal is None,
        balance_refusal_reason=refusal,
        target_single_tx_max_amount=target_single_tx_max_amount,
        fallback_chunk_amount=fallback_chunk_amount,
    )

    return ProductionPayoutPreflightPreview(
        payout_plan_id=payout_plan_id,
        source_wallet_name=source_wallet_name,
        execution_allowed=refusal is None,
        refusal_reason=refusal,
        wallet_balance=wallet_balance,
        planned_amount_total=planned_amount_total,
        reserve_mode=mode_label,
        reserve_percent=reserve_math["reserve_percent"],
        reserve_amount=reserve_math["reserve_amount"],
        spendable_after_reserve=reserve_math["spendable_after_reserve"],
        max_spend_percent=reserve_math["max_spend_percent"],
        max_spend_allowed=reserve_math["max_spend_allowed"],
        operator_override=operator_override,
        row_count=len(preview_rows),
        rows=tuple(preview_rows),
        utxo_chunking_policy=utxo_policy,
    )


def production_preflight_preview_to_dict(
    preview: ProductionPayoutPreflightPreview,
) -> dict[str, Any]:
    return {
        "payout_plan_id": preview.payout_plan_id,
        "source_wallet_name": preview.source_wallet_name,
        "execution_allowed": preview.execution_allowed,
        "refusal_reason": preview.refusal_reason,
        "trusted_balance": planner._serialize_numeric(preview.wallet_balance.trusted),
        "immature_balance": planner._serialize_numeric(preview.wallet_balance.immature),
        "planned_amount_total": planner._serialize_numeric(preview.planned_amount_total),
        "reserve_mode": preview.reserve_mode,
        "reserve_percent": planner._serialize_numeric(preview.reserve_percent),
        "reserve_amount": planner._serialize_numeric(preview.reserve_amount),
        "spendable_after_reserve": planner._serialize_numeric(
            preview.spendable_after_reserve
        ),
        "max_spend_percent": planner._serialize_numeric(preview.max_spend_percent),
        "max_spend_allowed": planner._serialize_numeric(preview.max_spend_allowed),
        "operator_override": preview.operator_override,
        "wallet_balance_source": WALLET_BALANCE_SOURCE_AZC_GETBALANCES,
        "row_count": preview.row_count,
        "rows": [
            {
                "payout_plan_row_id": row.payout_plan_row_id,
                "sc_node_id": row.sc_node_id,
                "payout_address": row.payout_address,
                "registry_payout_address": row.registry_payout_address,
                "payout_amount": planner._serialize_numeric(row.payout_amount),
                "row_status": row.row_status,
                "refusal_reason": row.refusal_reason,
            }
            for row in preview.rows
        ],
        "accounting_note": (
            "production preflight audit only; not wallet execution or spend authorization"
        ),
        "utxo_chunking_policy": utxo_chunking_policy_to_dict(preview.utxo_chunking_policy),
    }


def row_to_production_preflight_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "payout_plan_id": planner._to_int(row.get("payout_plan_id")),
        "source_wallet_name": row.get("source_wallet_name"),
        "preflight_status": row.get("preflight_status"),
        "execution_allowed": bool(row.get("execution_allowed")),
        "refusal_reason": row.get("refusal_reason"),
        "trusted_balance": planner._serialize_numeric(
            planner._to_decimal(row.get("trusted_balance"))
        ),
        "immature_balance": planner._serialize_numeric(
            planner._to_decimal(row.get("immature_balance"))
        ),
        "planned_amount_total": planner._serialize_numeric(
            planner._to_decimal(row.get("planned_amount_total"))
        ),
        "reserve_mode": row.get("reserve_mode"),
        "reserve_percent": planner._serialize_numeric(
            planner._to_decimal(row.get("reserve_percent"))
        ),
        "reserve_amount": planner._serialize_numeric(
            planner._to_decimal(row.get("reserve_amount"))
        ),
        "spendable_after_reserve": planner._serialize_numeric(
            planner._to_decimal(row.get("spendable_after_reserve"))
        ),
        "max_spend_percent": planner._serialize_numeric(
            planner._to_decimal(row.get("max_spend_percent"))
        ),
        "operator_override": bool(row.get("operator_override")),
        "wallet_balance_source": row.get("wallet_balance_source"),
        "idempotency_key": row.get("idempotency_key"),
        "notes": row.get("notes"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }


def row_to_production_preflight_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "production_preflight_id": planner._to_int(row.get("production_preflight_id")),
        "payout_plan_row_id": planner._to_int(row.get("payout_plan_row_id")),
        "sc_node_id": str(row["sc_node_id"]),
        "payout_address": str(row["payout_address"]),
        "payout_amount": planner._serialize_numeric(
            planner._to_decimal(row.get("payout_amount"))
        ),
        "row_status": row.get("row_status"),
        "refusal_reason": row.get("refusal_reason"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }
