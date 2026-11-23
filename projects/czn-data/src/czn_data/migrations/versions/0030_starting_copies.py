"""Records how many copies of each starting card a character's starting deck holds, so Faint
Memory costs can tell a Duplicate of a starting card from its starting copies. Builds again to
fill it."""

from alembic import op

from czn_data.migrate import request_build

revision = "0030"
down_revision = "0029"


def upgrade() -> None:
    op.execute(
        """
        -- Copies of this card in the character's starting deck; set on starting cards' base
        -- rows only.
        ALTER TABLE card ADD COLUMN starting_copies integer;
        """
    )
    request_build()
