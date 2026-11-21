"""Character identity prints of the card art (``czn_data.identity``), so the server can tell which
characters a game screenshot shows. Prints are filled in by the next enrich.
"""

from alembic import op

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.execute(
        """
        -- The identity model's feature vector of each character image. Keyed by image URL like
        -- prydwen.portrait_gender, so new art gets a new print, and by model, so prints of
        -- different models are never compared.
        CREATE TABLE prydwen.portrait_identity (
            image_url text NOT NULL,
            model text NOT NULL,
            print real[] NOT NULL,
            tagged_at timestamptz NOT NULL,
            PRIMARY KEY (image_url, model)
        );
        """
    )
