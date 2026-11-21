import os

import psycopg
import pytest

from czn_data import embed
from czn_data.migrate import migrate


@pytest.fixture(autouse=True)
def same_meaning_for_every_text(monkeypatch):
    """Replaces the embedding model: every text gets one vector, so search ranks by words."""
    monkeypatch.setattr(embed, "passages", lambda texts: [[1.0] for _ in texts])
    monkeypatch.setattr(embed, "query", lambda text: [1.0])


@pytest.fixture
def database_url():
    """An empty Postgres database from TEST_DATABASE_URL (the test drops every table)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; run `mise run test` to start Postgres")
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "DROP SCHEMA IF EXISTS prydwen, e7bot, stove CASCADE;"
            " DROP SCHEMA public CASCADE; CREATE SCHEMA public"
        )
    return url


@pytest.fixture
def conn(database_url):
    """A connection to a fully migrated, empty database."""
    migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection
