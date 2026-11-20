"""The ``e7bot`` schema: wiki.e7bot.top's game data, as received.

``document`` holds the last good response of every API path, replaced all at once by a run that
fetched and validated every path. ``save`` holds leaderboard Save Data, which never changes once
created, so each is fetched once. ``run`` records every attempt for ``data_status``.
"""

from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA e7bot;

        CREATE TABLE e7bot.document (
            path text PRIMARY KEY,
            body jsonb NOT NULL,
            fetched_at timestamptz NOT NULL
        );

        CREATE TABLE e7bot.save (
            save_id text PRIMARY KEY,
            body jsonb NOT NULL,
            fetched_at timestamptz NOT NULL
        );

        CREATE TABLE e7bot.run (
            started_at timestamptz PRIMARY KEY,
            finished_at timestamptz NOT NULL,
            ok boolean NOT NULL,
            error text
        );
        """
    )
