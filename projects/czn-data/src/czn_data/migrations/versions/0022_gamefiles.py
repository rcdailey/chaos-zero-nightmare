"""The ``gamefiles`` schema: tables from the game's own data files, and the in-game Help built
from them.

``gamefiles.file`` holds each downloaded table's rows; ``gamefiles.run`` records every attempt
for ``data_status``. ``czn-data build`` fills ``game_help`` from them (see
``czn_data.gamefiles``).
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0022"
down_revision = "0021"


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA gamefiles;

        CREATE TABLE gamefiles.file (
            -- The file's name in the game's manifest, such as text/en/text.db.
            name text PRIMARY KEY,
            -- The CDN build folder and the file's manifest record it was downloaded with; the
            -- file is downloaded again when either changes.
            build text NOT NULL,
            record bytea NOT NULL,
            -- Every row as an object of column -> text value.
            rows jsonb NOT NULL,
            fetched_at timestamptz NOT NULL
        );

        CREATE TABLE gamefiles.run (
            started_at timestamptz PRIMARY KEY,
            finished_at timestamptz NOT NULL,
            build text NOT NULL,
            ok boolean NOT NULL,
            error text
        );

        -- One topic of the game's Help menus: the menu showing it ("Help", "Chaos Help", or
        -- "Tutorial"), its title, and every page's text.
        CREATE TABLE game_help (
            id text PRIMARY KEY,
            menu text NOT NULL,
            title text NOT NULL,
            text text NOT NULL,
            embedding real[],
            data_fetched_at timestamptz NOT NULL,
            search tsvector GENERATED ALWAYS AS (to_tsvector('english',
                title || ' ' || text)) STORED
        );
        CREATE INDEX game_help_search_idx ON game_help USING gin (search);
        CREATE INDEX game_help_title_trgm ON game_help USING gin (title gin_trgm_ops);
        """
    )
    request_build()
