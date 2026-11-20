"""The ``stove`` schema: official posts from the game's Stove community boards, as received.

``article`` holds each post's newest good version; ``run`` records every attempt for
``data_status``. The searchable sections are derived from ``article`` (``czn_data.stove``).
"""

from alembic import op

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA stove;

        CREATE TABLE stove.article (
            article_id text PRIMARY KEY,
            board int NOT NULL,
            title text NOT NULL,
            -- The post body as Stove's editor HTML.
            content text NOT NULL,
            created_at timestamptz NOT NULL,
            -- Stove's edit time; an article is fetched again only when it changes.
            updated_at timestamptz NOT NULL,
            fetched_at timestamptz NOT NULL
        );

        CREATE TABLE stove.run (
            started_at timestamptz PRIMARY KEY,
            finished_at timestamptz NOT NULL,
            ok boolean NOT NULL,
            error text
        );
        """
    )
