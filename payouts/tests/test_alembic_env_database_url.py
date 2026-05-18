from pathlib import Path


LEDGER_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_env_prefers_postgres_ledger_database_url() -> None:
    source = (LEDGER_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "POSTGRES_LEDGER_DATABASE_URL" in source
    assert "create_engine(" in source
    assert "config.set_main_option" not in source


def test_alembic_ini_uses_placeholder_url() -> None:
    source = (LEDGER_ROOT / "alembic.ini").read_text(encoding="utf-8")

    assert (
        "sqlalchemy.url = "
        "postgresql+psycopg://ledger_user:ledger_password@localhost:5432/ledger_database"
        in source
    )
    assert "azledger_dev_password" not in source
