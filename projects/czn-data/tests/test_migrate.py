"""Contract: migrations keep stored pages, rebuild derived rows, and are safe to run from two
pods at once."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url

from czn_data.migrate import migrate

FIXTURE = Path(__file__).parent / "fixtures" / "character.html"
CHARACTER_URL = "https://www.prydwen.gg/chaos-zero-nightmare/characters/mei-lin"


def test_database_from_before_migrations_keeps_its_pages_and_gets_rebuilt_rows(database_url):
    # The cluster database was created by the old schema.sql, which the baseline reproduces:
    # its tables exist, but there is no migration history and no rows from the new parsers.
    config = Config()
    config.set_main_option("script_location", "czn_data:migrations")
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    engine.dispose()
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute(
            "INSERT INTO page (url, kind, html, fetched_at) VALUES (%s, 'character', %s, %s)",
            (CHARACTER_URL, FIXTURE.read_text(), datetime.now(UTC)),
        )

    migrate(database_url)

    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM prydwen.page").fetchone() == (1,)
        names = {row[0] for row in conn.execute("SELECT name FROM prydwen.card")}
        assert "Rising Dragon Spire" in names
        (title,) = conn.execute("SELECT title FROM prydwen.document").fetchone()
        assert title == "Mei Lin"


def test_concurrent_migrations_both_succeed(database_url):
    errors = []

    def run():
        try:
            migrate(database_url)
        except Exception as error:  # noqa: BLE001 - reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
