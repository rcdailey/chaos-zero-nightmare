import os
import re

import psycopg
import pytest

from czn_data import embed, rerank
from czn_data.migrate import migrate


@pytest.fixture(autouse=True)
def same_meaning_for_every_text(monkeypatch):
    """Replaces the search models: every text gets one vector, and the reranker scores a
    passage by how many of the query's words it holds, so search ranks by words."""
    monkeypatch.setattr(embed, "passages", lambda texts: [[1.0] for _ in texts])
    monkeypatch.setattr(embed, "query", lambda text: [1.0])

    def shared_words(query: str, passages: list[str]) -> list[float]:
        words = set(re.findall(r"\w+", query.lower()))
        return [float(len(words & set(re.findall(r"\w+", p.lower())))) for p in passages]

    monkeypatch.setattr(rerank, "scores", shared_words)


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; run `mise run test` to start Postgres")
    return url


# Whether the test database holds every migration. Migrating takes most of a test's time, so
# it runs once; later tests only empty the tables.
_migrated = False
# Every table but alembic's record of applied migrations, read from the catalog at each test,
# so tables a new migration adds are emptied too. No migration inserts rows of its own.
_TABLES_SQL = """
SELECT format('%I.%I', schemaname, tablename) FROM pg_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND NOT (schemaname = 'public' AND tablename = 'alembic_version')
"""


def _drop_every_table(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "DROP SCHEMA IF EXISTS prydwen, e7bot, stove, game8, gamefiles CASCADE;"
            " DROP SCHEMA public CASCADE; CREATE SCHEMA public"
        )


@pytest.fixture
def empty_database_url():
    """A Postgres database from TEST_DATABASE_URL with no tables, for migration tests."""
    global _migrated
    url = _test_database_url()
    _drop_every_table(url)
    _migrated = False
    return url


@pytest.fixture
def database_url():
    """A fully migrated Postgres database from TEST_DATABASE_URL with every table empty."""
    global _migrated
    url = _test_database_url()
    if not _migrated:
        _drop_every_table(url)
        migrate(url)
        _migrated = True
    with psycopg.connect(url, autocommit=True) as connection:
        tables = [row[0] for row in connection.execute(_TABLES_SQL)]
        connection.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
    return url


@pytest.fixture
def conn(database_url):
    """A connection to a fully migrated, empty database."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection
