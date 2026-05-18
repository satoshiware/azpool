"""
Phase 7 - SC-2 staged rollout and validation integration tests.

Tests verify that the runtime configuration and initialization paths are correct
for the staged deployment on SC-2. These are pure logic tests that don't require
a real database connection or FastAPI app.

Deployment Stages (from README):
  Step 8: Primary Session Cutover → POSTGRES_PRIMARY_SESSION_ENABLED=true
  Step 9: SQLite Retirement Mode → SQLITE_RETIREMENT_MODE_ENABLED=true
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Configuration Dataclass (mirrors app/config.py settings)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeploymentConfig:
    """Runtime deployment configuration for Phase 7."""

    # Step 8: Primary session cutover
    postgres_primary_session_enabled: bool = False
    postgres_primary_session_fallback_to_sqlite: bool = True

    # Step 9: SQLite retirement mode
    sqlite_retirement_mode_enabled: bool = False
    sqlite_runtime_writes_enabled: bool = True
    postgres_settlement_engine_enabled: bool = False
    postgres_sender_enabled: bool = False
    postgres_ledger_read_fallback_to_sqlite: bool = True

    # Candidate reads
    postgres_ledger_reads_enabled: bool = False
    postgres_ledger_read_fallback_to_sqlite: bool = True

    def validate_step_8(self) -> tuple[bool, str | None]:
        """Validate Step 8 configuration (primary session cutover)."""
        if not self.postgres_primary_session_enabled:
            return False, "POSTGRES_PRIMARY_SESSION_ENABLED must be true for Step 8"
        if not self.postgres_primary_session_fallback_to_sqlite:
            # Fallback can be false for strict mode
            pass
        return True, None

    def validate_step_9(self) -> tuple[bool, str | None]:
        """Validate Step 9 configuration (SQLite retirement mode)."""
        if not self.sqlite_retirement_mode_enabled:
            return False, "SQLITE_RETIREMENT_MODE_ENABLED must be true for Step 9"
        if self.sqlite_runtime_writes_enabled:
            return False, "SQLITE_RUNTIME_WRITES_ENABLED must be false for Step 9"
        if not self.postgres_primary_session_enabled:
            return False, "POSTGRES_PRIMARY_SESSION_ENABLED must be true for Step 9"
        if self.postgres_primary_session_fallback_to_sqlite:
            return False, "POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE must be false for Step 9"
        if not self.postgres_settlement_engine_enabled:
            return False, "POSTGRES_SETTLEMENT_ENGINE_ENABLED must be true for Step 9"
        if not self.postgres_sender_enabled:
            return False, "POSTGRES_SENDER_ENABLED must be true for Step 9"
        if self.postgres_ledger_read_fallback_to_sqlite:
            return False, "POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE must be false for Step 9"
        return True, None


# ---------------------------------------------------------------------------
# Runtime Initialization Scenarios
# ---------------------------------------------------------------------------

class Phase7RuntimeInitializer:
    """Simulates runtime initialization paths for Phase 7 deployment."""

    def __init__(self, config: DeploymentConfig):
        self.config = config
        self.initialized_components: dict[str, bool] = {}

    def can_open_postgres_session(self) -> bool:
        """Determine if Postgres session should be opened."""
        return self.config.postgres_primary_session_enabled

    def can_open_sqlite_session(self) -> bool:
        """Determine if SQLite session should be opened."""
        if self.config.sqlite_retirement_mode_enabled:
            return False
        return True

    def should_fail_closed_on_postgres_error(self) -> bool:
        """Determine if runtime should fail closed on Postgres errors."""
        if self.config.sqlite_retirement_mode_enabled:
            return True
        if self.config.postgres_primary_session_enabled:
            if not self.config.postgres_primary_session_fallback_to_sqlite:
                return True
        return False

    def initialize_session_layer(self) -> tuple[bool, str | None]:
        """Initialize the session layer based on config."""
        if self.config.sqlite_retirement_mode_enabled:
            self.initialized_components["postgres_session"] = True
            self.initialized_components["sqlite_session"] = False
            return True, None

        if self.config.postgres_primary_session_enabled:
            self.initialized_components["postgres_session"] = True
            self.initialized_components["sqlite_session"] = self.config.postgres_primary_session_fallback_to_sqlite
            return True, None

        self.initialized_components["postgres_session"] = False
        self.initialized_components["sqlite_session"] = True
        return True, None

    def initialize_settlement_engine(self) -> tuple[bool, str | None]:
        """Initialize settlement engine."""
        if self.config.sqlite_retirement_mode_enabled:
            if not self.config.postgres_settlement_engine_enabled:
                return False, "Step 9: POSTGRES_SETTLEMENT_ENGINE_ENABLED required"
            self.initialized_components["settlement_engine"] = "postgres"
            return True, None

        if self.config.postgres_primary_session_enabled:
            self.initialized_components["settlement_engine"] = "postgres_primary"
            return True, None

        self.initialized_components["settlement_engine"] = "sqlite"
        return True, None

    def initialize_sender(self) -> tuple[bool, str | None]:
        """Initialize payout sender."""
        if self.config.sqlite_retirement_mode_enabled:
            if not self.config.postgres_sender_enabled:
                return False, "Step 9: POSTGRES_SENDER_ENABLED required"
            self.initialized_components["sender"] = "postgres"
            return True, None

        if self.config.postgres_primary_session_enabled:
            self.initialized_components["sender"] = "postgres_primary"
            return True, None

        self.initialized_components["sender"] = "sqlite"
        return True, None

    def initialize_all(self) -> tuple[bool, list[str]]:
        """Initialize all components and return success + any errors."""
        errors: list[str] = []

        ok, err = self.initialize_session_layer()
        if err:
            errors.append(err)

        ok, err = self.initialize_settlement_engine()
        if not ok:
            errors.append(err or "Settlement engine init failed")

        ok, err = self.initialize_sender()
        if not ok:
            errors.append(err or "Sender init failed")

        return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Tests: Step 8 - Primary Session Cutover
# ---------------------------------------------------------------------------

def test_step8_config_valid() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=True,
    )
    ok, err = config.validate_step_8()
    assert ok is True
    assert err is None


def test_step8_strict_mode_config_valid() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
    )
    ok, err = config.validate_step_8()
    assert ok is True
    assert err is None


def test_step8_without_postgres_primary_fails() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=False,
    )
    ok, err = config.validate_step_8()
    assert ok is False
    assert err is not None


def test_step8_runtime_init_opens_postgres_session() -> None:
    config = DeploymentConfig(postgres_primary_session_enabled=True)
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert errors == []
    assert init.initialized_components["postgres_session"] is True


def test_step8_runtime_init_allows_sqlite_fallback() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=True,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["sqlite_session"] is True


def test_step8_strict_mode_no_sqlite_fallback() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["sqlite_session"] is False


def test_step8_fail_closed_enforcement_with_fallback_disabled() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
    )
    init = Phase7RuntimeInitializer(config)
    assert init.should_fail_closed_on_postgres_error() is True


def test_step8_fail_closed_not_enforced_with_fallback_enabled() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=True,
    )
    init = Phase7RuntimeInitializer(config)
    assert init.should_fail_closed_on_postgres_error() is False


# ---------------------------------------------------------------------------
# Tests: Step 9 - SQLite Retirement Mode
# ---------------------------------------------------------------------------

def test_step9_config_valid() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        sqlite_runtime_writes_enabled=False,
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
        postgres_settlement_engine_enabled=True,
        postgres_sender_enabled=True,
        postgres_ledger_read_fallback_to_sqlite=False,
    )
    ok, err = config.validate_step_9()
    assert ok is True
    assert err is None


def test_step9_missing_postgres_settlement_fails() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        sqlite_runtime_writes_enabled=False,
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
        postgres_settlement_engine_enabled=False,
    )
    ok, err = config.validate_step_9()
    assert ok is False
    assert "POSTGRES_SETTLEMENT_ENGINE_ENABLED" in err


def test_step9_missing_postgres_sender_fails() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        sqlite_runtime_writes_enabled=False,
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
        postgres_settlement_engine_enabled=True,
        postgres_sender_enabled=False,
    )
    ok, err = config.validate_step_9()
    assert ok is False
    assert "POSTGRES_SENDER_ENABLED" in err


def test_step9_with_sqlite_writes_fails() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        sqlite_runtime_writes_enabled=True,
    )
    ok, err = config.validate_step_9()
    assert ok is False
    assert "SQLITE_RUNTIME_WRITES_ENABLED must be false" in err


def test_step9_runtime_init_no_sqlite_session() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        sqlite_runtime_writes_enabled=False,
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
        postgres_settlement_engine_enabled=True,
        postgres_sender_enabled=True,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["sqlite_session"] is False


def test_step9_runtime_init_postgres_components() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        postgres_settlement_engine_enabled=True,
        postgres_sender_enabled=True,
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["settlement_engine"] == "postgres"
    assert init.initialized_components["sender"] == "postgres"


def test_step9_fail_closed_always_enforced() -> None:
    config = DeploymentConfig(sqlite_retirement_mode_enabled=True)
    init = Phase7RuntimeInitializer(config)
    assert init.should_fail_closed_on_postgres_error() is True


# ---------------------------------------------------------------------------
# Tests: Gradual Rollout Scenario
# ---------------------------------------------------------------------------

def test_gradual_rollout_phase1_sqlite_only() -> None:
    """Initial state: SQLite only."""
    config = DeploymentConfig()
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["postgres_session"] is False
    assert init.initialized_components["sqlite_session"] is True


def test_gradual_rollout_phase2_candidate_reads() -> None:
    """Phase 2: Candidate reads enabled."""
    config = DeploymentConfig(
        postgres_ledger_reads_enabled=True,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    # Settlement and sender still use SQLite
    assert init.initialized_components["settlement_engine"] == "sqlite"
    assert init.initialized_components["sender"] == "sqlite"


def test_gradual_rollout_phase3_primary_session_with_fallback() -> None:
    """Phase 3: Primary session with SQLite fallback."""
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=True,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["postgres_session"] is True
    assert init.initialized_components["sqlite_session"] is True


def test_gradual_rollout_phase4_primary_strict_mode() -> None:
    """Phase 4: Primary session strict mode (no fallback)."""
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.should_fail_closed_on_postgres_error() is True


def test_gradual_rollout_phase5_retirement_mode() -> None:
    """Phase 5: SQLite retirement mode (Postgres-only)."""
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        sqlite_runtime_writes_enabled=False,
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
        postgres_settlement_engine_enabled=True,
        postgres_sender_enabled=True,
        postgres_ledger_read_fallback_to_sqlite=False,
    )
    init = Phase7RuntimeInitializer(config)
    ok, errors = init.initialize_all()
    assert ok is True
    assert init.initialized_components["sqlite_session"] is False
    assert init.initialized_components["settlement_engine"] == "postgres"
    assert init.should_fail_closed_on_postgres_error() is True


# ---------------------------------------------------------------------------
# Tests: Cycle Continuity Validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SettlementCycleValidation:
    """Validates a settlement cycle under a given configuration."""

    cycle_number: int
    config: DeploymentConfig
    postgres_available: bool = True
    postgres_has_parity: bool = True
    audit_log_generated: bool = True

    def validate_cycle_success(self) -> tuple[bool, list[str]]:
        """Validate that a settlement cycle would succeed."""
        errors: list[str] = []

        # Only fail closed if Postgres is unavailable AND primary is enabled AND fallback is disabled
        if (
            self.config.postgres_primary_session_enabled
            and not self.config.postgres_primary_session_fallback_to_sqlite
            and not self.postgres_available
        ):
            errors.append(f"Cycle {self.cycle_number}: Postgres unavailable but primary enabled (strict mode)")

        if (
            self.config.postgres_primary_session_enabled
            and self.postgres_available
            and not self.postgres_has_parity
        ):
            errors.append(f"Cycle {self.cycle_number}: Postgres/SQLite parity mismatch")

        if not self.audit_log_generated:
            errors.append(f"Cycle {self.cycle_number}: Audit log not generated")

        return len(errors) == 0, errors


def test_cycle_continuity_step8_with_postgres_available() -> None:
    config = DeploymentConfig(postgres_primary_session_enabled=True)
    cycle = SettlementCycleValidation(
        cycle_number=100,
        config=config,
        postgres_available=True,
        postgres_has_parity=True,
        audit_log_generated=True,
    )
    ok, errors = cycle.validate_cycle_success()
    assert ok is True


def test_cycle_continuity_step8_postgres_unavailable_with_fallback() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=True,
    )
    cycle = SettlementCycleValidation(
        cycle_number=100,
        config=config,
        postgres_available=False,
    )
    ok, errors = cycle.validate_cycle_success()
    # Cycle should succeed (falls back to SQLite)
    assert ok is True


def test_cycle_continuity_step8_strict_mode_postgres_unavailable_fails() -> None:
    config = DeploymentConfig(
        postgres_primary_session_enabled=True,
        postgres_primary_session_fallback_to_sqlite=False,
    )
    cycle = SettlementCycleValidation(
        cycle_number=100,
        config=config,
        postgres_available=False,
    )
    ok, errors = cycle.validate_cycle_success()
    assert ok is False
    assert "Postgres unavailable but primary enabled" in errors[0]


def test_cycle_continuity_parity_mismatch_detected() -> None:
    config = DeploymentConfig(postgres_primary_session_enabled=True)
    cycle = SettlementCycleValidation(
        cycle_number=100,
        config=config,
        postgres_available=True,
        postgres_has_parity=False,
    )
    ok, errors = cycle.validate_cycle_success()
    assert ok is False
    assert "parity mismatch" in errors[0]


def test_multiple_cycle_success_step9() -> None:
    config = DeploymentConfig(
        sqlite_retirement_mode_enabled=True,
        postgres_primary_session_enabled=True,
        postgres_settlement_engine_enabled=True,
        postgres_sender_enabled=True,
    )
    for cycle_num in range(100, 110):
        cycle = SettlementCycleValidation(
            cycle_number=cycle_num,
            config=config,
            postgres_available=True,
            postgres_has_parity=True,
            audit_log_generated=True,
        )
        ok, errors = cycle.validate_cycle_success()
        assert ok is True, f"Cycle {cycle_num} failed: {errors}"
