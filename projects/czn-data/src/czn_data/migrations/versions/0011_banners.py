"""Prydwen's banner schedule: current and upcoming character and partner banners."""

from alembic import op

from czn_data.migrate import request_rebuild

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute(
        """
        -- One gacha banner. Times are the Global server's. featured lists the rate-up
        -- characters; secondary the 4-star characters with a smaller rate-up.
        CREATE TABLE prydwen.banner (
            url text NOT NULL REFERENCES prydwen.page ON DELETE CASCADE,
            name text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('character', 'partner')),
            rarity int,
            rerun boolean NOT NULL,
            -- Season and phase, such as "Season 5 Phase 2"; reruns give only the season.
            phase text,
            starts_at timestamptz NOT NULL,
            ends_at timestamptz NOT NULL,
            featured text[] NOT NULL,
            secondary text[] NOT NULL,
            description text
        );
        CREATE VIEW banner AS
        SELECT g.*, p.data_fetched_at FROM prydwen.banner g JOIN prydwen.page p USING (url);
        """
    )
    request_rebuild()
