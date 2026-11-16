import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from refactra.api import create_app
from refactra.config import Settings
from refactra.db import make_engine, sessions

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def migration_config(connection) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def database():
    url = os.getenv("REFACTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set REFACTRA_TEST_DATABASE_URL to run real PostgreSQL integration tests.")
    admin = make_engine(url)
    # Never truncate application tables. Each test owns a unique, disposable schema.
    schema = f"refactra_test_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": f"-csearch_path={schema}",
            "connect_timeout": 5,
        },
    )
    try:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def session_factory(database):
    return sessions(database)


@pytest.fixture
def client(database):
    with TestClient(create_app(Settings("unused", enable_mock=True), database)) as client:
        yield client
