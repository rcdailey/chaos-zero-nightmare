"""The ``game8`` schema: Game8's guide pages on the game's systems and modes, as received.

``page`` holds each selected page's newest good HTML; ``run`` records every attempt for
``data_status``. The searchable sections are built from ``page`` (``czn_data.game8``).
"""

from alembic import op

revision = "0019"
down_revision = "0018"


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA game8;

        CREATE TABLE game8.page (
            url text PRIMARY KEY,
            html text NOT NULL,
            -- Game8's sitemap date for the page (its last update); a page is fetched again
            -- when it changes. Null when the sitemap gave none.
            lastmod timestamptz,
            fetched_at timestamptz NOT NULL
        );

        CREATE TABLE game8.run (
            started_at timestamptz PRIMARY KEY,
            finished_at timestamptz NOT NULL,
            ok boolean NOT NULL,
            error text
        );
        """
    )
