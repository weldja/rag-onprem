"""Fixtures for tests against a real Postgres + pgvector (scripts/test-db.sh).

DATABASE_URL must name a database ending in `_test`; it is dropped and
recreated, then migrated to head, once per session. Each test then starts
from empty tables.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

from app.core.config import get_settings


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = make_url(get_settings().database_url)
    if not (url.database or "").endswith("_test"):
        pytest.exit(f"Refusing to run DB tests against '{url.database}': name must end in _test")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    command.upgrade(Config("alembic.ini"), "head")
    eng = create_engine(url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Engine:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE sessions, ingest_jobs, document_groups, chunks, documents, sources, "
                "user_groups, users RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("DELETE FROM groups WHERE name <> 'All staff'"))
    return engine
