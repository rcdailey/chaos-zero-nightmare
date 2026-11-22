"""Equipment icons from e7bot and their prints (``czn_data.gear``), so the server can name the
gear a screenshot shows only as icons; screenshot prints are keyed by a code fingerprint. Builds
again to fill ``equipment.icon_name``."""

from alembic import op

from czn_data.migrate import request_build

revision = "0028"
down_revision = "0027"


def upgrade() -> None:
    op.execute(
        """
        -- Image files e7bot serves for the game's assets, by their path under e7bot's asset
        -- root: equipment icons (item/<icon_name>.webp) and the rarity backgrounds slots draw
        -- behind them (rarity/...). Fetched once; e7bot names them after the game's own files.
        CREATE TABLE e7bot.asset (
            path text PRIMARY KEY,
            image bytea NOT NULL,
            fetched_at timestamptz NOT NULL
        );

        -- One print per rarity background for each icon. digest fingerprints the code that made
        -- them (czn_data.digest), so prints of different code are never compared.
        CREATE TABLE e7bot.gear_print (
            icon_name text NOT NULL,
            digest text NOT NULL,
            prints real[] NOT NULL,
            tagged_at timestamptz NOT NULL,
            PRIMARY KEY (icon_name, digest)
        );

        -- Character prints are keyed the same way instead of by a hand-kept model name; enrich
        -- prints every portrait again under the new key and drops the old prints.
        ALTER TABLE prydwen.portrait_identity RENAME COLUMN model TO digest;

        -- The game's icon file for the item; items that share an icon look the same on screen.
        ALTER TABLE equipment ADD COLUMN icon_name text;
        """
    )
    request_build()
