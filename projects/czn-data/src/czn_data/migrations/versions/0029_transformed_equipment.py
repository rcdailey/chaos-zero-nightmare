"""Marks equipment that is a transformed form of another item (Harmonization, Mutation, and the
like), which shares its base item's icon. Builds again to fill it."""

from alembic import op

from czn_data.migrate import request_build

revision = "0029"
down_revision = "0028"


def upgrade() -> None:
    op.execute(
        """
        -- True for a form a Chaos event turns worn gear into; such forms share the icon of the
        -- item they come from, so a screenshot cannot tell them apart from it.
        ALTER TABLE equipment ADD COLUMN transformed boolean;
        """
    )
    request_build()
