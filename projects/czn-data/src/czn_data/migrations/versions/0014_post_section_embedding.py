"""A meaning vector per post section (``czn_data.embed``), for search by meaning.

Unit-length ``real[]`` compared by dot product in SQL; fine for a few thousand sections. Past
roughly 100,000, move to pgvector and an index.
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    op.execute("ALTER TABLE post_section ADD COLUMN embedding real[]")
    request_build()
