from __future__ import annotations

import asyncio
import sys
from pathlib import Path


LEDGER_ROOT = Path(__file__).resolve().parents[1]
if str(LEDGER_ROOT) not in sys.path:
    sys.path.insert(0, str(LEDGER_ROOT))

from app.postgres_db import make_postgres_engine, make_postgres_session_factory
from app.postgres_repositories import PostgresLedgerRepository
from app.translator_sv1_capture_proxy import (
    TranslatorSv1CaptureProxy,
    configure_logging,
    load_config_from_env,
)


def main() -> None:
    config = load_config_from_env()
    configure_logging(config.log_level)
    repository = None
    if not config.dry_run:
        engine = make_postgres_engine(config.postgres_database_url)
        repository = PostgresLedgerRepository(make_postgres_session_factory(engine))
    asyncio.run(TranslatorSv1CaptureProxy(config=config, repository=repository).run_forever())


if __name__ == "__main__":
    main()
