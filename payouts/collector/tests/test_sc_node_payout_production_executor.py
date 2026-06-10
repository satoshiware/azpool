from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_production_executor as executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight


_FORBIDDEN_RPC = re.compile(
    r"\b("
    r"sendmany|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PLAN_ID = 1
_PREFLIGHT_ID = 1
_SOURCE_WALLET = "wallet"
_PLANNED = Decimal("121.875")
_CONFIRM = "SEND 121.875000000000 FROM wallet FOR PLAN 1"
_IDEMPOTENCY = "production-real-v0-plan-1"


def _getbalances_payload(trusted: str = "664.37481345") -> dict[str, object]:
    return {"mine": {"trusted": trusted, "immature": "0"}}


def _approved_plan() -> dict[str, object]:
    return {
        "id": _PLAN_ID,
        "status": plan_review.PLAN_STATUS_APPROVED,
        "planned_amount_total": _PLANNED,
        "wallet_name": _SOURCE_WALLET,
    }


def _passed_preflight() -> dict[str, object]:
    return {
        "id": _PREFLIGHT_ID,
        "payout_plan_id": _PLAN_ID,
        "source_wallet_name": _SOURCE_WALLET,
        "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
        "execution_allowed": True,
        "planned_amount_total": _PLANNED,
    }


def _plan_row() -> dict[str, object]:
    return {
        "id": 10,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "payout_amount": _PLANNED,
        "row_status": plan_review.ROW_STATUS_APPROVED,
    }


def _preflight_row() -> dict[str, object]:
    return {
        "payout_plan_row_id": 10,
        "sc_node_id": "sc-2",
        "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "payout_amount": _PLANNED,
        "row_status": production_preflight.ROW_STATUS_CHECKED,
    }


def _address_lookup(
    address: str = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
) -> dict[str, list[dict[str, object]]]:
    return {
        "sc-2": [
            {
                "sc_node_id": "sc-2",
                "payout_address": address,
                "status": "active",
                "is_default": True,
            }
        ]
    }


def _wallet_balance(trusted: str = "664.37481345") -> executor.WalletBalance:
    return executor.parse_wallet_balance_from_getbalances(_getbalances_payload(trusted))


def test_build_expected_confirmation_phrase_exact_format() -> None:
    phrase = executor.build_expected_confirmation_phrase(
        _PLAN_ID,
        _PLANNED,
        _SOURCE_WALLET,
    )
    assert phrase == _CONFIRM


def test_execute_real_refuses_wrong_confirmation_phrase() -> None:
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
        confirmation_phrase="SEND 1 FROM wallet FOR PLAN 1",
        existing_by_key=None,
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is not None
    assert "confirmation phrase mismatch" in refusal


def test_preview_allows_single_row_without_allow_multiple_flag() -> None:
    preview = executor.build_production_execution_preview(
        payout_plan_id=_PLAN_ID,
        production_preflight_id=_PREFLIGHT_ID,
        source_wallet_name=_SOURCE_WALLET,
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
    )
    assert preview.execution_allowed is True


def test_execute_real_refuses_multiple_rows_without_flag() -> None:
    row = _plan_row()
    row2 = dict(row)
    row2["id"] = 11
    preflight2 = dict(_preflight_row())
    preflight2["payout_plan_row_id"] = 11
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[row, row2],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row(), preflight2],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance("1000"),
        address_lookup=_address_lookup(),
        confirmation_phrase=_CONFIRM,
        existing_by_key=None,
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
        allow_multiple_rows=False,
    )
    assert refusal is not None
    assert "allow-multiple-rows" in refusal


def test_planned_amount_above_spendable_after_reserve_refuses() -> None:
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance("200"),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "spendable_after_reserve" in refusal


def test_planned_amount_above_trusted_balance_refuses() -> None:
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance("100"),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "trusted wallet balance" in refusal


def test_preview_refuses_without_passed_preflight() -> None:
    preflight = _passed_preflight()
    preflight["preflight_status"] = production_preflight.PREFLIGHT_STATUS_REFUSED
    preflight["execution_allowed"] = False
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=None,
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "preflight" in refusal


def test_preview_refuses_non_approved_plan() -> None:
    plan = _approved_plan()
    plan["status"] = plan_review.PLAN_STATUS_DRAFT
    refusal = executor.evaluate_preview_refusal(
        plan=plan,
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "approved" in refusal


def test_preview_refuses_address_drift() -> None:
    row = _plan_row()
    row["payout_address"] = "az1plan"
    preflight_row = _preflight_row()
    preflight_row["payout_address"] = "az1plan"
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[row],
        preflight=_passed_preflight(),
        preflight_rows=[preflight_row],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(address="az1registry"),
    )
    assert refusal is not None
    assert "drift" in refusal


def test_active_execution_with_different_idempotency_key_refuses() -> None:
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
        confirmation_phrase=_CONFIRM,
        existing_by_key=None,
        active_execution={
            "id": 5,
            "idempotency_key": "other-key",
            "status": executor.EXECUTION_STATUS_SENT,
        },
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is not None
    assert "active production execution" in refusal


def test_idempotent_replay_returns_none_refusal_when_existing_by_key() -> None:
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
        confirmation_phrase=_CONFIRM,
        existing_by_key={"id": 1, "idempotency_key": _IDEMPOTENCY},
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is None


def test_sendtoaddress_argv_is_explicit_list() -> None:
    argv = executor.build_sendtoaddress_argv(
        azc_bin="/tmp/azc",
        source_wallet_name=_SOURCE_WALLET,
        payout_address="az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        payout_amount=_PLANNED,
    )
    assert argv == [
        "/tmp/azc",
        "-rpcwallet=wallet",
        "sendtoaddress",
        "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "121.87500000",
    ]


def test_format_wallet_amount_down_truncates_to_8_decimals() -> None:
    assert executor.format_wallet_amount_down("1.004928799583") == "1.00492879"
    assert executor.format_wallet_amount_down("0.870071200416") == "0.87007120"
    assert executor.format_wallet_amount_down(Decimal("121.875")) == "121.87500000"


def test_sendtoaddress_argv_never_sends_raw_12_decimal_amounts() -> None:
    argv = executor.build_sendtoaddress_argv(
        azc_bin="/tmp/azc",
        source_wallet_name=_SOURCE_WALLET,
        payout_address="az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        payout_amount=Decimal("1.004928799583"),
    )
    assert argv[-1] == "1.00492879"
    assert "1.004928799583" not in argv


def test_script_preview_has_no_sendtoaddress() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    preview_block = source.split("def _cmd_preview")[1].split("def _cmd_execute_real")[0]
    assert "sendtoaddress" not in preview_block


def test_script_subprocess_run_does_not_use_shell_true() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run" in source


def test_script_execute_real_uses_sendtoaddress() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "_run_sendtoaddress" in source
    assert "execute-real" in source


def test_app_module_has_no_forbidden_wallet_rpcs_except_sendtoaddress_path() -> None:
    path = AZPOOL_ROOT / "payouts/collector/app/sc_node_payout_production_executor.py"
    text = path.read_text(encoding="utf-8")
    guard_patterns = (
        r"_FORBIDDEN_WALLET_RPC_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        r"_FORBIDDEN_MARK_CONFIRMED_RPC_KEYWORDS = re\.compile\([\s\S]*?\)\n",
    )
    send_block = re.search(
        r"def build_sendtoaddress_argv[\s\S]*?return argv\n",
        text,
    )
    gettransaction_block = re.search(
        r"def build_mark_confirmed_gettransaction_argv[\s\S]*?return argv\n",
        text,
    )
    scrubbed = text
    for pattern in guard_patterns:
        scrubbed = re.compile(pattern, re.MULTILINE).sub("", scrubbed, count=1)
    if send_block:
        scrubbed = scrubbed.replace(send_block.group(0), "")
    if gettransaction_block:
        scrubbed = scrubbed.replace(gettransaction_block.group(0), "")
    assert _FORBIDDEN_RPC.search(scrubbed) is None


def test_insert_sql_touches_only_production_execution_tables() -> None:
    for builder in (
        executor.build_insert_production_execution_sql,
        executor.build_insert_production_execution_row_sql,
        executor.build_mark_production_execution_sent_sql,
        executor.build_mark_production_execution_row_sent_sql,
    ):
        sql = builder()
        tables = set(
            re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", sql.lower())
        )
        assert tables <= {
            "sc_node_payout_production_executions",
            "sc_node_payout_production_execution_rows",
        }


def test_no_payout_plan_mutation_sql() -> None:
    module_source = (
        AZPOOL_ROOT / "payouts/collector/app/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "update sc_node_payout_plans" not in module_source.lower()
    for sql in (
        executor.build_production_executions_sql(),
        executor.build_production_execution_details_sql(1),
        executor.build_approved_payout_plan_for_execution_sql(1),
    ):
        assert "update sc_node_payout_plans" not in sql.lower()


def test_admin_production_executions_sql_is_select_only() -> None:
    sql = admin_readonly.build_production_executions_sql()
    assert "sc_node_payout_production_executions" in sql
    admin_readonly.assert_readonly_sql(sql)


def test_calculate_execution_guardrails_default_fifty_percent_reserve() -> None:
    trusted = Decimal("664.37481345")
    result = executor.calculate_execution_guardrails(
        trusted_balance=trusted,
        planned_amount_total=_PLANNED,
    )
    assert result["reserve_amount"] == Decimal("332.187406725000")
    assert result["spendable_after_reserve"] == Decimal("332.187406725000")


def _sent_execution(*, txid: str = "83096125cfa614208edbb04672902fcbb953338c80587c3326afa8719c15fbb8") -> dict[str, object]:
    return {
        "id": 5,
        "status": executor.EXECUTION_STATUS_SENT,
        "txid": txid,
    }


def test_mark_confirmed_requires_confirm_chain_evidence() -> None:
    refusal = executor.evaluate_mark_confirmed_chain_prereq_refusal(
        execution=_sent_execution(),
        confirm_chain_evidence=False,
        source_wallet_name=_SOURCE_WALLET,
    )
    assert refusal == "mark-confirmed requires --confirm-chain-evidence"


def test_mark_confirmed_refuses_missing_txid() -> None:
    execution = _sent_execution(txid="")
    refusal = executor.evaluate_mark_confirmed_chain_prereq_refusal(
        execution=execution,
        confirm_chain_evidence=True,
        source_wallet_name=_SOURCE_WALLET,
    )
    assert refusal == "production execution txid is required for chain evidence check"


def test_mark_confirmed_refuses_zero_confirmations() -> None:
    refusal = executor.evaluate_mark_confirmed_confirmations_refusal(
        confirmations=0,
        min_confirmations=1,
    )
    assert refusal == "gettransaction confirmations 0 < required 1"


def test_mark_confirmed_allows_confirmations_ge_min() -> None:
    refusal = executor.evaluate_mark_confirmed_confirmations_refusal(
        confirmations=1,
        min_confirmations=1,
    )
    assert refusal is None


def test_mark_confirmed_idempotent_if_already_confirmed() -> None:
    confirmed = {
        "id": 5,
        "status": executor.EXECUTION_STATUS_CONFIRMED,
        "txid": "83096125cfa614208edbb04672902fcbb953338c80587c3326afa8719c15fbb8",
    }
    refusal = executor.evaluate_mark_confirmed_refusal(confirmed)
    assert refusal is None


def test_mark_confirmed_gettransaction_argv_is_readonly_only() -> None:
    txid = "83096125cfa614208edbb04672902fcbb953338c80587c3326afa8719c15fbb8"
    argv = executor.build_mark_confirmed_gettransaction_argv(
        azc_bin="/usr/local/bin/azc-payout-readonly",
        source_wallet_name=_SOURCE_WALLET,
        txid=txid,
    )
    assert argv == [
        "/usr/local/bin/azc-payout-readonly",
        "-rpcwallet=wallet",
        "gettransaction",
        txid,
    ]
    assert _FORBIDDEN_RPC.search(" ".join(argv)) is None


def test_mark_confirmed_script_block_uses_gettransaction_not_sendtoaddress() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    mark_block = source.split("def _cmd_mark_confirmed")[1].split("def _cmd_details")[0]
    assert "gettransaction" in mark_block
    assert "sendtoaddress" not in mark_block
    assert "walletpassphrase" not in mark_block


# ---------------------------------------------------------------------------
# execute-real multi-row behavior (script-level, mocked DB + mocked wallet).
# These tests never touch a real wallet RPC or a real database.
# ---------------------------------------------------------------------------

import json
from types import SimpleNamespace

from payouts.collector.app import payout_addresses
from payouts.scripts import sc_node_payout_production_executor as executor_cli


_MR_PLAN_ID = 14
_MR_PREFLIGHT_ID = 10
_MR_EXECUTION_ID = 50
_MR_PLANNED_TOTAL = Decimal("1.874999999999")
_MR_CONFIRM = "SEND 1.874999999999 FROM wallet FOR PLAN 14"
_MR_IDEMPOTENCY = "unit-test-plan-14-multirow"
_MR_SC2_ADDRESS = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv"
_MR_SC3_ADDRESS = "az1qalf65k4u0vgmxhj3qyp4l2q9uz92hj9jfa6pwp"
_SEND_FAILURE = object()


def _multirow_plan() -> dict[str, object]:
    return {
        "id": _MR_PLAN_ID,
        "status": plan_review.PLAN_STATUS_APPROVED,
        "planned_amount_total": _MR_PLANNED_TOTAL,
        "wallet_name": _SOURCE_WALLET,
    }


def _multirow_plan_rows() -> list[dict[str, object]]:
    return [
        {
            "id": 210,
            "payout_plan_id": _MR_PLAN_ID,
            "sc_node_id": "sc-2",
            "payout_address": _MR_SC2_ADDRESS,
            "payout_amount": Decimal("1.004928799583"),
            "row_status": plan_review.ROW_STATUS_APPROVED,
        },
        {
            "id": 211,
            "payout_plan_id": _MR_PLAN_ID,
            "sc_node_id": "sc-3",
            "payout_address": _MR_SC3_ADDRESS,
            "payout_amount": Decimal("0.870071200416"),
            "row_status": plan_review.ROW_STATUS_APPROVED,
        },
    ]


def _multirow_preflight() -> dict[str, object]:
    return {
        "id": _MR_PREFLIGHT_ID,
        "payout_plan_id": _MR_PLAN_ID,
        "source_wallet_name": _SOURCE_WALLET,
        "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
        "execution_allowed": True,
        "planned_amount_total": _MR_PLANNED_TOTAL,
    }


def _multirow_preflight_rows() -> list[dict[str, object]]:
    return [
        {
            "payout_plan_row_id": plan_row["id"],
            "sc_node_id": plan_row["sc_node_id"],
            "payout_address": plan_row["payout_address"],
            "payout_amount": plan_row["payout_amount"],
            "row_status": production_preflight.ROW_STATUS_CHECKED,
        }
        for plan_row in _multirow_plan_rows()
    ]


def _multirow_address_rows() -> list[dict[str, object]]:
    return [
        {
            "sc_node_id": "sc-2",
            "payout_address": _MR_SC2_ADDRESS,
            "status": "active",
            "is_default": True,
        },
        {
            "sc_node_id": "sc-3",
            "payout_address": _MR_SC3_ADDRESS,
            "status": "active",
            "is_default": True,
        },
    ]


class _FakeCursor:
    def __init__(self, db: "_FakeExecutionDb") -> None:
        self._db = db
        self._rows: list[dict[str, object]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
        self._rows = self._db.dispatch(sql, params)

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, db: "_FakeExecutionDb") -> None:
        self._db = db

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self, row_factory: object = None) -> _FakeCursor:
        return _FakeCursor(self._db)

    def commit(self) -> None:
        self._db.events.append(("commit",))

    def set_read_only(self, value: bool) -> None:
        pass


class _FakeExecutionDb:
    """In-memory stand-in for azpool_ledger; dispatches on exact builder SQL."""

    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.execution_state: dict[str, object] | None = None
        self.row_states: dict[int, dict[str, object]] = {}
        self._next_row_id = 101

    def dispatch(
        self, sql: str, params: dict[str, object] | None
    ) -> list[dict[str, object]]:
        if sql == executor.build_approved_payout_plan_for_execution_sql(_MR_PLAN_ID):
            return [_multirow_plan()]
        if sql == executor.build_approved_payout_plan_rows_for_execution_sql(
            _MR_PLAN_ID
        ):
            return _multirow_plan_rows()
        if sql == executor.build_passed_production_preflight_sql():
            return [_multirow_preflight()]
        if sql == executor.build_production_preflight_rows_for_execution_sql():
            return _multirow_preflight_rows()
        if sql == payout_addresses.build_active_default_payout_addresses_sql():
            return _multirow_address_rows()
        if sql == executor.build_execution_by_plan_idempotency_sql():
            return []
        if sql == executor.build_existing_active_production_execution_sql():
            return []
        if sql == executor.build_insert_production_execution_sql():
            assert params is not None
            self.execution_state = dict(params)
            self.execution_state["id"] = _MR_EXECUTION_ID
            self.events.append(("insert_execution", _MR_EXECUTION_ID))
            return [{"id": _MR_EXECUTION_ID}]
        if sql == executor.build_insert_production_execution_row_sql():
            assert params is not None
            row_id = self._next_row_id
            self._next_row_id += 1
            self.row_states[row_id] = dict(params)
            self.row_states[row_id]["id"] = row_id
            self.events.append(("insert_row", row_id, params["payout_plan_row_id"]))
            return [{"id": row_id}]
        if sql == executor.build_mark_production_execution_row_sent_sql():
            assert params is not None
            row_id = int(params["production_execution_row_id"])  # type: ignore[arg-type]
            state = self.row_states[row_id]
            if state["row_status"] != executor.ROW_STATUS_DRAFT:
                return []
            state["row_status"] = executor.ROW_STATUS_SENT
            state["txid"] = params["txid"]
            self.events.append(("row_sent", row_id, params["txid"]))
            return [{"id": row_id}]
        if sql == executor.build_mark_production_execution_row_refused_sql():
            assert params is not None
            row_id = int(params["production_execution_row_id"])  # type: ignore[arg-type]
            state = self.row_states[row_id]
            state["row_status"] = executor.ROW_STATUS_REFUSED
            state["refusal_reason"] = params["refusal_reason"]
            self.events.append(("row_refused", row_id))
            return [{"id": row_id}]
        if sql == executor.build_mark_production_execution_sent_sql():
            assert params is not None
            assert self.execution_state is not None
            self.execution_state["status"] = executor.EXECUTION_STATUS_SENT
            self.execution_state["txid"] = params["txid"]
            self.events.append(("execution_sent", params["txid"]))
            return [{"id": _MR_EXECUTION_ID}]
        if sql == executor.build_mark_production_execution_refused_sql():
            assert params is not None
            assert self.execution_state is not None
            self.execution_state["status"] = executor.EXECUTION_STATUS_REFUSED
            self.execution_state["refusal_reason"] = params["refusal_reason"]
            self.events.append(("execution_refused", params["refusal_reason"]))
            return [{"id": _MR_EXECUTION_ID}]
        if sql == executor.build_production_execution_details_sql(_MR_EXECUTION_ID):
            assert self.execution_state is not None
            return [dict(self.execution_state)]
        if sql == executor.build_production_execution_rows_sql(_MR_EXECUTION_ID):
            return [dict(state) for state in self.row_states.values()]
        raise AssertionError(f"unexpected SQL in fake DB: {sql[:120]!r}")


class _FakeWalletSubprocess:
    """Mocked subprocess.run: getbalances + scripted sendtoaddress results."""

    def __init__(self, send_results: list[object]) -> None:
        self.send_calls: list[list[str]] = []
        self._send_results = list(send_results)

    def __call__(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "getbalances" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"mine": {"trusted": "6.0", "immature": "0"}}),
                stderr="",
            )
        if "sendtoaddress" in argv:
            self.send_calls.append(list(argv))
            result = self._send_results.pop(0)
            if result is _SEND_FAILURE:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="wallet send failed (unit-test)",
                )
            return SimpleNamespace(returncode=0, stdout=f"{result}\n", stderr="")
        raise AssertionError(f"unexpected wallet RPC argv: {argv!r}")


def _execute_real_argv(*, allow_multiple_rows: bool) -> list[str]:
    argv = [
        "execute-real",
        "--payout-plan-id",
        str(_MR_PLAN_ID),
        "--production-preflight-id",
        str(_MR_PREFLIGHT_ID),
        "--source-wallet-name",
        _SOURCE_WALLET,
        "--azc-bin",
        "/usr/local/bin/azc-payout",
        "--idempotency-key",
        _MR_IDEMPOTENCY,
        "--confirm-phrase",
        _MR_CONFIRM,
    ]
    if allow_multiple_rows:
        argv.insert(1, "--allow-multiple-rows")
    return argv


def _run_execute_real(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_multiple_rows: bool,
    send_results: list[object],
) -> tuple[int, _FakeExecutionDb, _FakeWalletSubprocess]:
    db = _FakeExecutionDb()
    wallet = _FakeWalletSubprocess(send_results)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test/azpool_ledger")
    monkeypatch.setattr(
        executor_cli.psycopg, "connect", lambda dsn: _FakeConn(db)
    )
    monkeypatch.setattr(executor_cli.subprocess, "run", wallet)
    rc = executor_cli.main(_execute_real_argv(allow_multiple_rows=allow_multiple_rows))
    return rc, db, wallet


def test_execute_real_refuses_multirow_plan_before_any_send(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, db, wallet = _run_execute_real(
        monkeypatch,
        allow_multiple_rows=False,
        send_results=[],
    )
    capsys.readouterr()
    assert rc == 1
    assert wallet.send_calls == []
    assert db.execution_state is not None
    assert db.execution_state["status"] == executor.EXECUTION_STATUS_REFUSED
    assert "allow-multiple-rows" in str(db.execution_state["refusal_reason"])
    assert not [event for event in db.events if event[0] == "row_sent"]


def test_execute_real_multirow_sends_each_row_with_own_txid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, db, wallet = _run_execute_real(
        monkeypatch,
        allow_multiple_rows=True,
        send_results=["txid-A", "txid-B"],
    )
    capsys.readouterr()
    assert rc == 0
    assert len(wallet.send_calls) == 2
    assert wallet.send_calls[0][-2:] == [_MR_SC2_ADDRESS, "1.00492879"]
    assert wallet.send_calls[1][-2:] == [_MR_SC3_ADDRESS, "0.87007120"]
    # Each production_execution_row_id records its own txid.
    assert db.row_states[101]["row_status"] == executor.ROW_STATUS_SENT
    assert db.row_states[101]["txid"] == "txid-A"
    assert db.row_states[102]["row_status"] == executor.ROW_STATUS_SENT
    assert db.row_states[102]["txid"] == "txid-B"
    assert ("row_sent", 101, "txid-A") in db.events
    assert ("row_sent", 102, "txid-B") in db.events
    assert db.execution_state is not None
    assert db.execution_state["status"] == executor.EXECUTION_STATUS_SENT


def test_execute_real_sends_8dp_rounded_down_amounts_not_raw_planned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, _, wallet = _run_execute_real(
        monkeypatch,
        allow_multiple_rows=True,
        send_results=["txid-A", "txid-B"],
    )
    capsys.readouterr()
    assert rc == 0
    amounts = [argv[-1] for argv in wallet.send_calls]
    assert amounts == ["1.00492879", "0.87007120"]
    for argv in wallet.send_calls:
        assert "1.004928799583" not in argv
        assert "0.870071200416" not in argv


def test_execute_real_partial_failure_preserves_first_row_txid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No partial_sent status exists yet (TODO in the executor script):
    already-broadcast rows must remain recorded as sent with their own
    committed txid even though the overall execution is later refused."""
    rc, db, wallet = _run_execute_real(
        monkeypatch,
        allow_multiple_rows=True,
        send_results=["txid-A", _SEND_FAILURE],
    )
    capsys.readouterr()
    assert rc == 1
    assert len(wallet.send_calls) == 2

    # First row stays sent and keeps its own txid.
    assert db.row_states[101]["row_status"] == executor.ROW_STATUS_SENT
    assert db.row_states[101]["txid"] == "txid-A"

    # First row recording was committed before failure handling started.
    row_sent_idx = db.events.index(("row_sent", 101, "txid-A"))
    refused_idx = next(
        i for i, event in enumerate(db.events) if event[0] == "execution_refused"
    )
    assert row_sent_idx < refused_idx
    assert ("commit",) in db.events[row_sent_idx + 1 : refused_idx]

    # Second row is never stamped with the first row's txid.
    assert db.row_states[102]["row_status"] == executor.ROW_STATUS_REFUSED
    assert db.row_states[102]["txid"] is None
    assert ("row_sent", 102, "txid-A") not in db.events
    assert [event for event in db.events if event[0] == "row_sent"] == [
        ("row_sent", 101, "txid-A")
    ]

    # Execution header records the partial broadcast for the operator.
    assert db.execution_state is not None
    assert db.execution_state["status"] == executor.EXECUTION_STATUS_REFUSED
    assert "partial broadcast" in str(db.execution_state["refusal_reason"])
    assert "txid-A" in str(db.execution_state["refusal_reason"])


def test_execute_real_script_has_no_single_row_hard_guard() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "supports exactly one payout row" not in source
    assert "plan_rows[0]" not in source
