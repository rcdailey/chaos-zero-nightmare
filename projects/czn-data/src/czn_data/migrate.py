"""Database migrations: bring the schema to the version this code expects.

Both the daily CronJob and the MCP server's init container run ``czn-data migrate`` before they
start work, so whichever pod starts first with a new image upgrades the database. An advisory
lock makes the second one wait, then find nothing to do.

Migrations live in ``migrations/versions`` as plain SQL run through Alembic, one file per
revision, forward only. In the ``prydwen`` schema every table except ``page`` (and the stored
portraits) is derived from the HTML stored in ``page``; a migration that changes those tables
calls ``request_rebuild()``. The merged tables in ``public`` are derived from both source
schemas by ``czn_data.build``; a migration that changes them calls ``request_build()``. After
the last migration, the requested steps run in the same transaction (reparse first, then
build), so the schema change and the rebuilt rows commit together. Nothing is fetched.
"""

import logging

from alembic import command, context
from alembic.config import Config
from sqlalchemy import create_engine, make_url, pool, text

log = logging.getLogger(__name__)

# Arbitrary constant shared by every czn-data process; pg_advisory_xact_lock takes a bigint.
MIGRATION_LOCK = 7_418_023_355


def migrate(database_url: str) -> None:
    from .build import build
    from .scrape import reparse

    url = make_url(database_url).set(drivername="postgresql+psycopg")
    engine = create_engine(url, poolclass=pool.NullPool)
    config = Config()
    config.set_main_option("script_location", "czn_data:migrations")
    log.info("migrating database")
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            driver = connection.connection.driver_connection
            if config.attributes.get("rebuild"):
                log.info("rebuilding derived tables from stored HTML")
                result = reparse(driver, allow_shrink=True)
                log.info("rebuild finished", extra={"pages": dict(result.pages)})
            if config.attributes.get("rebuild") or config.attributes.get("build"):
                build(driver)
    finally:
        engine.dispose()


def request_rebuild() -> None:
    """Call from a migration's ``upgrade()`` when it changes tables derived from page HTML."""
    context.config.attributes["rebuild"] = True


def request_build() -> None:
    """Call from a migration's ``upgrade()`` when it changes the merged tables in ``public``."""
    context.config.attributes["build"] = True
