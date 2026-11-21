"""Character appearance from portrait art (``czn_data.tagger``), so Claude can tell which
character a portrait on a game screen shows. Appearances are filled in by the next enrich.
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    op.execute(
        """
        -- Visible traits the portrait tagger read from each character image, most certain
        -- first. Keyed by image URL like prydwen.portrait_gender, so new art is tagged again.
        CREATE TABLE prydwen.portrait_appearance (
            image_url text PRIMARY KEY,
            tags text[] NOT NULL,
            tagged_at timestamptz NOT NULL
        );
        ALTER TABLE character ADD COLUMN appearance text[];
        """
    )
    request_build()
