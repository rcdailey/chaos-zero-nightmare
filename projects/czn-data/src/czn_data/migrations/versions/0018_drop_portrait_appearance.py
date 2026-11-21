"""Drop character appearance tags: the server now names characters on screenshots by comparing
them with the card art (``czn_data.identity``), which replaced matching on tagged traits.
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0018"
down_revision = "0017"


def upgrade() -> None:
    op.execute(
        """
        DROP TABLE prydwen.portrait_appearance;
        ALTER TABLE character DROP COLUMN appearance;
        """
    )
    request_build()
