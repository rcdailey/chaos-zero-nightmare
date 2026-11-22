"""What each step last processed, so a run skips items whose input and code did not change
(``czn_data.digest``)."""

from alembic import op

revision = "0027"
down_revision = "0026"


def upgrade() -> None:
    op.execute(
        """
        -- One row per item a step processed cleanly. digest fingerprints the item's input and
        -- the step's code; value holds the step's output when the step reuses it (a pickle,
        -- read only by the code version that wrote it, since a code change changes digest).
        CREATE TABLE processed (
            step text NOT NULL,
            key text NOT NULL,
            digest text NOT NULL,
            value bytea,
            PRIMARY KEY (step, key)
        );
        """
    )
