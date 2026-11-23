"""Gives each search passage a short id that search lists and reads passages by, so a passage
can be named without its key (a page URL for guides)."""

from alembic import op

revision = "0031"
down_revision = "0030"


def upgrade() -> None:
    op.execute(
        """
        -- ref: a short id derived from the passage's key, stable across builds while the
        -- passage exists; search shows it on its one-line matches and reads passages by it.
        ALTER TABLE passage ADD COLUMN ref text GENERATED ALWAYS AS (
            left(md5(source || ':' || key || ':' || position), 8)) STORED;
        CREATE UNIQUE INDEX passage_ref ON passage (ref);
        """
    )
