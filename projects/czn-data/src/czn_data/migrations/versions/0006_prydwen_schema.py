"""Move Prydwen's data into the ``prydwen`` schema, rows intact.

These tables hold Prydwen's pages and the rows parsed from them, in Prydwen's own shape. The
MCP server reads merged tables in ``public`` instead, which ``czn-data build`` derives from this
schema and the ``e7bot`` schema.
"""

from alembic import op

revision = "0006"
down_revision = "0005"

TABLES = (
    "page",
    "document",
    "character",
    "card",
    "potential",
    "manifest_ego",
    "equipment",
    "fragment_set",
    "partner",
    "effect",
    "team",
    "deck",
    "character_equipment",
    "seasonal_power",
    "fragment_rank",
    "partner_rank",
    "synergy",
    "portrait",
    "portrait_gender",
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA prydwen")
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} SET SCHEMA prydwen")
