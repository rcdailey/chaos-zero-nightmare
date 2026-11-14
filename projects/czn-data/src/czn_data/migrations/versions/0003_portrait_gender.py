"""Character gender from portrait art, and card art as the character image.

``character.image_url`` changes from the splash art to the card art, so every character row is
rebuilt from the stored HTML. Genders are filled in by the next scrape.
"""

from alembic import op

from czn_data.migrate import request_rebuild

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute(
        """
        -- Gender the portrait tagger assigned to each character image (czn_data.gender).
        -- Keyed by image URL rather than character, so it survives rebuilds of the character
        -- table, and new art for a character is tagged again.
        CREATE TABLE portrait_gender (
            image_url text PRIMARY KEY,
            gender text NOT NULL CHECK (gender IN ('female', 'male')),
            tagged_at timestamptz NOT NULL
        );
        """
    )
    request_rebuild()
