"""Card art stored by the scrape, so post-processing (``czn-data enrich``) reads only the
database. Images are fetched by the next scrape.
"""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute(
        """
        -- Each character image (character.image_url), fetched once. About 30 KB each.
        CREATE TABLE portrait (
            image_url text PRIMARY KEY,
            image bytea NOT NULL,
            fetched_at timestamptz NOT NULL
        );
        """
    )
