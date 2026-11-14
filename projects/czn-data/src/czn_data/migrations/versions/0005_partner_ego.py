"""Partner EGO skills and base stats, read from the partners page's flight payload. Partner rows
are rebuilt from the stored HTML, which also drops the "New!" badge from partner names.
"""

from alembic import op

from czn_data.migrate import request_rebuild

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE partner
            ADD COLUMN ego_name text,
            -- EP cost of the EGO skill.
            ADD COLUMN ego_cost int,
            ADD COLUMN ego_text text,
            -- Base stats as Prydwen lists them (hp_base, atk_base, def_base).
            ADD COLUMN hp int,
            ADD COLUMN attack int,
            ADD COLUMN defense int;
        """
    )
    request_rebuild()
