from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if (PROJECT_ROOT / ".env").exists():
    load_dotenv(PROJECT_ROOT / ".env", override=True)

DEFAULT_POSTGRES_LEDGER_DATABASE_URL = (
    "postgresql+psycopg://azledger:azledger_dev_password@localhost:5432/azcoin_ledger_dev"
)


def resolve_postgres_database_url(database_url: str | None = None) -> str:
    value = (database_url or os.getenv("POSTGRES_LEDGER_DATABASE_URL", "")).strip()
    return value or DEFAULT_POSTGRES_LEDGER_DATABASE_URL


def make_postgres_engine(
    database_url: str | None = None,
    *,
    echo: bool = False,
    schema: str | None = None,
) -> Engine:
    engine = create_engine(
        resolve_postgres_database_url(database_url),
        echo=echo,
        future=True,
        pool_pre_ping=True,
    )
    if schema:
        return engine.execution_options(schema_translate_map={None: schema})
    return engine


def make_postgres_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
