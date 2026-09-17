from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from payouts.collector.app.delta import SnapshotCounters, compute_delta
from payouts.collector.app import pool_client, sc_node_credit_ledger as ledger
from payouts.collector.app.work_evidence import verify_block_work, difficulty, compact_target


def test_cumulative_counter_delta_preserves_all_source_digits():
    at = datetime(2026, 9, 16, 17, 53, tzinfo=timezone.utc)
    old = SnapshotCounters(Decimal(9239), Decimal('2151089.449144117'), 9239, at)
    new = SnapshotCounters(Decimal(9240), Decimal('75178753098847460000000'), 9240, at+timedelta(minutes=1))
    result = compute_delta(pool_instance_id='sv2p01', client_id=3,
                           channel_type='extended', channel_id=1520, previous=old, current=new)
    assert result.work_delta == Decimal('75178753098847457848910.550855883')
    assert result.accepted_delta == 1


def test_http_counter_does_not_round_through_binary_float():
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"extended_channels":[{"channel_id":1520,"user_identity":"circle-01","shares_accepted":9240,"share_work_sum":75178753098847460000000.000000001}]}'
    with patch.object(pool_client.requests, 'get', return_value=response):
        payload = pool_client.fetch_client_channels('http://example.invalid', 3)
    assert payload['extended_channels'][0]['share_work_sum'] == Decimal('75178753098847460000000.000000001')


@pytest.mark.parametrize('work', ['75178753098847457848910.550855883','1e20','NaN','Infinity','-1'])
def test_invalid_work_refuses_credit_before_database_write(work):
    assert 'work accounting anomaly' in ledger.evaluate_allocation_refusal(
        reward_event_count=1, reward_amount_total=Decimal('1.875'),
        mapped_work_total=Decimal(work), coverage_gap=False)


def test_ordinary_work_allocation_remains_allowed():
    assert ledger.evaluate_allocation_refusal(reward_event_count=1,
        reward_amount_total=Decimal('1.875'), mapped_work_total=Decimal('36255474.467355737'),
        coverage_gap=False) is None


def test_incident_block_is_valid_but_cannot_justify_assigned_work():
    header = (Path(__file__).parent/'fixtures'/'block-943889.hex').read_text().strip()
    proof = verify_block_work(header_hex=header,
        expected_hash='000000000000004862ae1561442df1fdd7d329d482547957e9f603da3e8112c2',
        assigned_target_be_hex='00000000000000000000000000101497aad8457bfd1f1d5a19bb7e5fb40a548c')
    assert not proof.meets_assigned_target
    assert Decimal('75178753098847459737600') < proof.assigned_difficulty < Decimal('75178753098847459737601')
    assert Decimal('11428310.0216198') < proof.network_difficulty < Decimal('11428310.0216199')
    # A block with an ordinary, easier assigned target satisfies both rules.
    ordinary = verify_block_work(header_hex=header, expected_hash=proof.block_hash,
        assigned_target_be_hex=f'{0xffff << 200:064x}')
    assert ordinary.meets_assigned_target
    assert ordinary.assigned_difficulty == 256


def test_header_tampering_is_rejected():
    header = (Path(__file__).parent/'fixtures'/'block-943889.hex').read_text().strip()
    with pytest.raises(ValueError, match='header hash'):
        verify_block_work(header_hex='00'+header[2:], expected_hash='00'*32,
                          assigned_target_be_hex='ff'*32)


def test_target_byte_order_changes_difficulty_and_is_not_silently_reversed():
    target = bytes.fromhex('00000000000000000000000000101497aad8457bfd1f1d5a19bb7e5fb40a548c')
    assert difficulty(int.from_bytes(target,'big')) > Decimal('1e22')
    assert difficulty(int.from_bytes(target,'little')) < 1
    assert compact_target(0x1a0177d0) == 0x177d0 << (8*23)


def test_accepted_shares_with_lost_counter_work_block_allocation():
    start=datetime(2026,9,16,tzinfo=timezone.utc)
    coverage=ledger.resolve_operator_coverage(coverage_start=start,
        coverage_end=start+timedelta(days=1),pool_coverage_start=start,
        pool_coverage_end=start+timedelta(days=1),reward_coverage_start=start,
        reward_coverage_end=start+timedelta(days=1))
    result=ledger.build_credit_run_preview(wallet_name='wallet',coverage=coverage,
        reward_rows=[{'amount':Decimal('1.875')}],
        sc_node_rows=[{'sc_node_id':'circle-01','work_delta_total':Decimal('100'),
                       'invalid_work_rows':348}],unmapped_row=None)
    assert not result.allocation_allowed
    assert not result.sc_node_credits
    assert 'precision-lost' in result.refusal_reason


def test_zero_delta_source_is_detected_in_mapped_and_unmapped_queries():
    for query in (ledger.build_sc_node_work_share_sql(),ledger.build_unmapped_work_sql()):
        assert 'invalid_work_rows' in query
        assert 'accepted_delta > 0' in query
        assert 'work_delta = 0' in query
