from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if (PROJECT_ROOT / ".env").exists():
    load_dotenv(PROJECT_ROOT / ".env", override=True)

DEFAULT_DB_PATH = str(PROJECT_ROOT / "payouts.db")
DEFAULT_AUDIT_LOG_PATH = str(PROJECT_ROOT / "logs" / "payout_audit.jsonl")
POSTGRES_LEDGER_READ_MODE_SQLITE = "sqlite"
POSTGRES_LEDGER_READ_MODE_SHADOW_CANDIDATE = "postgres_shadow_candidate"
POSTGRES_LEDGER_READ_MODE_AUTHORITATIVE = "postgres_authoritative"
VALID_POSTGRES_LEDGER_READ_MODES = frozenset(
    {
        POSTGRES_LEDGER_READ_MODE_SQLITE,
        POSTGRES_LEDGER_READ_MODE_SHADOW_CANDIDATE,
        POSTGRES_LEDGER_READ_MODE_AUTHORITATIVE,
    }
)


def _resolve_path(path_value: str, default_path: str) -> str:
    value = (path_value or "").strip() or default_path
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _parse_env_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_postgres_ledger_read_mode(value: str | None) -> str:
    mode = (value or POSTGRES_LEDGER_READ_MODE_SQLITE).strip() or POSTGRES_LEDGER_READ_MODE_SQLITE
    if mode not in VALID_POSTGRES_LEDGER_READ_MODES:
        valid = ", ".join(sorted(VALID_POSTGRES_LEDGER_READ_MODES))
        raise ValueError(f"Invalid POSTGRES_LEDGER_READ_MODE {mode!r}; expected one of: {valid}")
    return mode


def _parse_endpoint_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(endpoint for endpoint in (item.strip() for item in value.split(",")) if endpoint)


@dataclass(frozen=True)
class Settings:
    payout_interval_minutes: int = 10
    payout_decimals: int = 8
    fixed_reward_btc: str = ""
    pool_reward_url: str = ""
    pool_api_base_url: str = ""
    pool_api_key: str = ""
    reward_mode: str = "blocks"
    block_reward_btc: str = "1.87500000"
    enable_block_event_rewards: bool = False
    maturity_window_minutes: int = 200
    block_reward_batch_url: str = ""
    block_reward_batch_timeout_seconds: int = 10
    defer_on_zero_matured_reward: bool = True
    translator_blocks_found_url: str = ""
    translator_blocks_found_timeout_seconds: int = 10
    translator_blocks_found_limit: int = 100
    translator_blocks_found_candidate_window_seconds: int = 30
    translator_blocks_found_candidate_limit: int = 5
    translator_metrics_url: str = "http://127.0.0.1:9092/metrics"
    translator_channels_url: str = ""
    translator_downstreams_url: str = ""
    translator_bearer_token: str = ""
    enable_startup_reconciliation_hook: bool = False
    enable_block_event_replay_hook: bool = False
    enable_reward_refetch_hook: bool = False
    enable_settlement_replay_hook: bool = False
    payout_audit_log_path: str = DEFAULT_AUDIT_LOG_PATH
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 60
    db_path: str = DEFAULT_DB_PATH
    dry_run: bool = True
    postgres_ledger_shadow_write_enabled: bool = False
    postgres_settlement_engine_enabled: bool = False
    postgres_sender_enabled: bool = False
    postgres_primary_session_enabled: bool = False
    postgres_primary_session_fallback_to_sqlite: bool = True
    sqlite_retirement_mode_enabled: bool = False
    sqlite_runtime_writes_enabled: bool = True
    postgres_ledger_reads_enabled: bool = False
    postgres_ledger_read_fallback_to_sqlite: bool = True
    postgres_ledger_read_require_shadow_match: bool = True
    postgres_ledger_read_allowed_endpoints: tuple[str, ...] = ()
    postgres_ledger_read_mode: str = POSTGRES_LEDGER_READ_MODE_SQLITE

    @property
    def effective_postgres_read_mode(self) -> str:
        if not self.postgres_ledger_reads_enabled:
            return POSTGRES_LEDGER_READ_MODE_SQLITE
        return self.postgres_ledger_read_mode

    @property
    def postgres_reads_enabled(self) -> bool:
        return self.postgres_ledger_reads_enabled

    @property
    def postgres_read_allowed_endpoints(self) -> frozenset[str]:
        return frozenset(self.postgres_ledger_read_allowed_endpoints)


def load_settings() -> Settings:
    resolved_db_path = _resolve_path(os.getenv("DB_PATH", DEFAULT_DB_PATH), DEFAULT_DB_PATH)
    resolved_audit_log_path = _resolve_path(
        os.getenv("PAYOUT_AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH),
        DEFAULT_AUDIT_LOG_PATH,
    )

    return Settings(
        payout_interval_minutes=int(os.getenv("PAYOUT_INTERVAL_MINUTES", "10")),
        payout_decimals=int(os.getenv("PAYOUT_DECIMALS", "8")),
        fixed_reward_btc=os.getenv("FIXED_REWARD_BTC", ""),
        pool_reward_url=os.getenv("POOL_REWARD_URL", ""),
        pool_api_base_url=os.getenv("POOL_API_BASE_URL", ""),
        pool_api_key=os.getenv("POOL_API_KEY", ""),
        reward_mode=os.getenv("REWARD_MODE", "blocks"),
        block_reward_btc=os.getenv("BLOCK_REWARD_BTC", "1.87500000"),
        enable_block_event_rewards=_parse_env_bool(
            os.getenv("ENABLE_BLOCK_EVENT_REWARDS"),
            default=False,
        ),
        maturity_window_minutes=int(os.getenv("MATURITY_WINDOW_MINUTES", "200")),
        block_reward_batch_url=os.getenv("BLOCK_REWARD_BATCH_URL", ""),
        block_reward_batch_timeout_seconds=int(
            os.getenv("BLOCK_REWARD_BATCH_TIMEOUT_SECONDS", "10")
        ),
        defer_on_zero_matured_reward=_parse_env_bool(
            os.getenv("DEFER_ON_ZERO_MATURED_REWARD"),
            default=True,
        ),
        translator_blocks_found_url=os.getenv("TRANSLATOR_BLOCKS_FOUND_URL", ""),
        translator_blocks_found_timeout_seconds=int(
            os.getenv("TRANSLATOR_BLOCKS_FOUND_TIMEOUT_SECONDS", "10")
        ),
        translator_blocks_found_limit=int(os.getenv("TRANSLATOR_BLOCKS_FOUND_LIMIT", "100")),
        translator_blocks_found_candidate_window_seconds=int(
            os.getenv("TRANSLATOR_BLOCKS_FOUND_CANDIDATE_WINDOW_SECONDS", "30")
        ),
        translator_blocks_found_candidate_limit=int(
            os.getenv("TRANSLATOR_BLOCKS_FOUND_CANDIDATE_LIMIT", "5")
        ),
        translator_metrics_url=os.getenv("TRANSLATOR_METRICS_URL", "http://127.0.0.1:9092/metrics"),
        translator_channels_url=os.getenv("TRANSLATOR_CHANNELS_URL", ""),
        translator_downstreams_url=os.getenv("TRANSLATOR_DOWNSTREAMS_URL", ""),
        translator_bearer_token=os.getenv("TRANSLATOR_BEARER_TOKEN", ""),
        enable_startup_reconciliation_hook=_parse_env_bool(
            os.getenv("ENABLE_STARTUP_RECONCILIATION_HOOK"),
            default=False,
        ),
        enable_block_event_replay_hook=_parse_env_bool(
            os.getenv("ENABLE_BLOCK_EVENT_REPLAY_HOOK"),
            default=False,
        ),
        enable_reward_refetch_hook=_parse_env_bool(
            os.getenv("ENABLE_REWARD_REFETCH_HOOK"),
            default=False,
        ),
        enable_settlement_replay_hook=_parse_env_bool(
            os.getenv("ENABLE_SETTLEMENT_REPLAY_HOOK"),
            default=False,
        ),
        payout_audit_log_path=resolved_audit_log_path,
        scheduler_enabled=_parse_env_bool(os.getenv("SCHEDULER_ENABLED"), default=False),
        scheduler_interval_seconds=int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60")),
        db_path=resolved_db_path,
        dry_run=_parse_env_bool(os.getenv("DRY_RUN"), default=True),
        postgres_ledger_shadow_write_enabled=_parse_env_bool(
            os.getenv("POSTGRES_LEDGER_SHADOW_WRITE_ENABLED"),
            default=False,
        ),
        postgres_settlement_engine_enabled=_parse_env_bool(
            os.getenv("POSTGRES_SETTLEMENT_ENGINE_ENABLED"),
            default=False,
        ),
        postgres_sender_enabled=_parse_env_bool(
            os.getenv("POSTGRES_SENDER_ENABLED"),
            default=False,
        ),
        postgres_primary_session_enabled=_parse_env_bool(
            os.getenv("POSTGRES_PRIMARY_SESSION_ENABLED"),
            default=False,
        ),
        postgres_primary_session_fallback_to_sqlite=_parse_env_bool(
            os.getenv("POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE"),
            default=True,
        ),
        sqlite_retirement_mode_enabled=_parse_env_bool(
            os.getenv("SQLITE_RETIREMENT_MODE_ENABLED"),
            default=False,
        ),
        sqlite_runtime_writes_enabled=_parse_env_bool(
            os.getenv("SQLITE_RUNTIME_WRITES_ENABLED"),
            default=True,
        ),
        postgres_ledger_reads_enabled=_parse_env_bool(
            os.getenv("POSTGRES_LEDGER_READS_ENABLED"),
            default=False,
        ),
        postgres_ledger_read_fallback_to_sqlite=_parse_env_bool(
            os.getenv("POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE"),
            default=True,
        ),
        postgres_ledger_read_require_shadow_match=_parse_env_bool(
            os.getenv("POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH"),
            default=True,
        ),
        postgres_ledger_read_allowed_endpoints=_parse_endpoint_list(
            os.getenv("POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS", "")
        ),
        postgres_ledger_read_mode=_parse_postgres_ledger_read_mode(
            os.getenv("POSTGRES_LEDGER_READ_MODE", POSTGRES_LEDGER_READ_MODE_SQLITE)
        ),
    )
