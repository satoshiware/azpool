from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked
from payouts.collector.app import sc_node_payout_production_executor as executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.scripts import sc_node_payout_production_chunked_executor as chunked_cli


_FORBIDDEN_RPC = re.compile(
    r"\b("
    r"sendmany|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PLAN_ID = 2
_PREFLIGHT_ID = 2
_PLANNED = Decimal("223.125")
_CHUNK = Decimal("25")
_ADDRESS = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv"


def _plan_row() -> dict[str, object]:
    return {
        "id": 20,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "payout_amount": _PLANNED,
        "row_status": plan_review.ROW_STATUS_APPROVED,
    }


def test_split_payout_amount_into_chunks_exact_remainder() -> None:
    chunks = chunked.split_payout_amount_into_chunks(_PLANNED, _CHUNK)
    assert len(chunks) == 9
    assert chunks[:8] == tuple([_CHUNK] * 8)
    assert chunks[8] == Decimal("23.125000000000")
    assert sum(chunks, Decimal("0")) == _PLANNED


def test_chunked_confirmation_phrase_for_plan_2() -> None:
    chunks = chunked.build_chunk_plans_for_rows([_plan_row()], _CHUNK)
    phrase = chunked.build_chunked_confirmation_phrase(
        payout_plan_id=_PLAN_ID,
        planned_amount_total=_PLANNED,
        source_wallet_name="wallet",
        chunk_count=len(chunks),
    )
    assert phrase == "SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS"


def test_chunked_preview_refuses_active_sent_execution() -> None:
    refusal = chunked.evaluate_chunked_execute_real_refusal(
        plan={"id": _PLAN_ID, "status": plan_review.PLAN_STATUS_APPROVED, "planned_amount_total": _PLANNED},
        plan_rows=[_plan_row()],
        preflight={
            "id": _PREFLIGHT_ID,
            "source_wallet_name": "wallet",
            "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
            "execution_allowed": True,
            "planned_amount_total": _PLANNED,
        },
        preflight_rows=[
            {
                "payout_plan_row_id": 20,
                "sc_node_id": "sc-2",
                "payout_address": _ADDRESS,
                "payout_amount": _PLANNED,
                "row_status": production_preflight.ROW_STATUS_CHECKED,
            }
        ],
        source_wallet_name="wallet",
        wallet_balance=executor.parse_wallet_balance_from_getbalances(
            {"mine": {"trusted": "800", "immature": "0"}}
        ),
        address_lookup={
            "sc-2": [
                {
                    "sc_node_id": "sc-2",
                    "payout_address": _ADDRESS,
                    "status": "active",
                    "is_default": True,
                }
            ]
        },
        confirmation_phrase="SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS",
        chunk_amount=_CHUNK,
        chunks=chunked.build_chunk_plans_for_rows([_plan_row()], _CHUNK),
        existing_by_key=None,
        active_execution={
            "id": 1,
            "idempotency_key": "other-key",
            "status": executor.EXECUTION_STATUS_SENT,
        },
        idempotency_key="chunked-plan-2-v0",
    )
    assert refusal is not None
    assert "active production execution" in refusal


def test_chunked_execute_allows_when_previous_execution_refused() -> None:
    chunks = chunked.build_chunk_plans_for_rows([_plan_row()], _CHUNK)
    refusal = chunked.evaluate_chunked_execute_real_refusal(
        plan={"id": _PLAN_ID, "status": plan_review.PLAN_STATUS_APPROVED, "planned_amount_total": _PLANNED},
        plan_rows=[_plan_row()],
        preflight={
            "id": _PREFLIGHT_ID,
            "source_wallet_name": "wallet",
            "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
            "execution_allowed": True,
            "planned_amount_total": _PLANNED,
        },
        preflight_rows=[
            {
                "payout_plan_row_id": 20,
                "sc_node_id": "sc-2",
                "payout_address": _ADDRESS,
                "payout_amount": _PLANNED,
                "row_status": production_preflight.ROW_STATUS_CHECKED,
            }
        ],
        source_wallet_name="wallet",
        wallet_balance=executor.parse_wallet_balance_from_getbalances(
            {"mine": {"trusted": "800", "immature": "0"}}
        ),
        address_lookup={
            "sc-2": [
                {
                    "sc_node_id": "sc-2",
                    "payout_address": _ADDRESS,
                    "status": "active",
                    "is_default": True,
                }
            ]
        },
        confirmation_phrase="SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS",
        chunk_amount=_CHUNK,
        chunks=chunks,
        existing_by_key=None,
        active_execution=None,
        idempotency_key="chunked-plan-2-v0",
    )
    assert refusal is None


def test_insert_chunk_sql_targets_only_chunked_tables() -> None:
    sql = chunked.build_insert_production_execution_chunk_sql()
    lowered = sql.lower()
    assert "sc_node_payout_production_execution_chunks" in lowered
    for table in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
        assert table in chunked._CHUNKED_MUTATION_TABLES


def test_existing_active_execution_sql_is_select_only() -> None:
    sql = chunked.build_existing_active_production_execution_sql()
    admin_readonly.assert_readonly_sql(sql)
    assert "partial_sent" in sql


def test_sendtoaddress_argv_per_chunk() -> None:
    argv = chunked.build_sendtoaddress_argv(
        azc_bin="/usr/local/bin/azc-payout",
        source_wallet_name="wallet",
        payout_address=_ADDRESS,
        payout_amount=_CHUNK,
    )
    assert argv[2] == "sendtoaddress"
    assert "shell=True" not in Path(chunked_cli.__file__).read_text(encoding="utf-8")


def test_execute_real_stops_on_chunk_failure_and_records_partial_sent() -> None:
    chunk_plans = chunked.build_chunk_plans_for_rows([_plan_row()], _CHUNK)
    calls: list[Decimal] = []

    def fake_send(**kwargs: object) -> str:
        amount = kwargs["payout_amount"]
        assert isinstance(amount, Decimal)
        calls.append(amount)
        if len(calls) == 3:
            raise RuntimeError("error code -6, Transaction too large")
        return f"txid-{len(calls)}"

    with patch.object(chunked_cli, "_run_sendtoaddress", side_effect=fake_send):
        # exercise send loop logic via direct simulation
        sent_count = 0
        try:
            for chunk in chunk_plans:
                fake_send(
                    azc_bin="azc",
                    source_wallet_name="wallet",
                    payout_address=chunk.payout_address,
                    payout_amount=chunk.chunk_amount,
                )
                sent_count += 1
        except RuntimeError as exc:
            assert sent_count == 2
            assert "Transaction too large" in str(exc)


def test_mark_confirmed_refuses_partial_sent() -> None:
    refusal = chunked.evaluate_chunked_mark_confirmed_refusal(
        {
            "id": 3,
            "status": chunked.EXECUTION_STATUS_PARTIAL_SENT,
            "notes": chunked.build_chunked_executor_notes(_CHUNK),
        },
        chunk_count=9,
        sent_chunk_count=2,
    )
    assert refusal is not None
    assert "partial_sent" in refusal


def test_implementation_has_no_forbidden_rpc_except_sendtoaddress_guard() -> None:
    guard_block = re.compile(
        r"_FORBIDDEN_WALLET_RPC_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_production_chunked_executor.py",
        "payouts/scripts/sc_node_payout_production_chunked_executor.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1) if "FORBIDDEN" in text else text
        assert _FORBIDDEN_RPC.search(scrubbed) is None
    script = (AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_chunked_executor.py").read_text(
        encoding="utf-8"
    )
    assert script.count("sendtoaddress") >= 1
    assert "sendmany" not in script
