"""Split character guides into tables and Epiphany reviews per variant.

Teams, decks, equipment and seasonal tabs, fragment and partner rankings, and synergies become
rows of their own instead of prose in ``character.sections``. Cards get the review text for
their own variant, the game's variant name where Prydwen gives it, and the base card gets its
Neutral and Divine advice. All rows are rebuilt from the stored HTML.
"""

from alembic import op

from czn_data.migrate import request_rebuild

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE card
            -- The game's name for an Epiphany variant ("Amped"), when Prydwen gives it.
            ADD COLUMN alias text,
            -- Base cards only: the exact Neutral pick when Prydwen names one ("2 Morale"),
            -- and Prydwen's advice on Neutral and Divine Epiphanies.
            ADD COLUMN target_neutral text,
            ADD COLUMN neutral_review text,
            ADD COLUMN divine_review text;
        CREATE INDEX card_text_trgm ON card USING gin (text gin_trgm_ops);

        CREATE TABLE team (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            name text NOT NULL,
            tier text,
            members text[] NOT NULL
        );

        CREATE TABLE deck (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            name text,
            partner text,
            equipment text[] NOT NULL,
            fragments text[] NOT NULL,
            -- [{"name", "count"}]; names include the Epiphany variant ("Strike II").
            cards jsonb NOT NULL,
            notes text
        );

        -- Recommended equipment per tab of the Build tab's Equipment section. Tabs are seasons
        -- or themes ("Swamp", "Season 1: Lab Zero"); slots maps a slot heading to item names,
        -- best first.
        CREATE TABLE character_equipment (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            tab text NOT NULL,
            best boolean NOT NULL,
            slots jsonb NOT NULL,
            notes text
        );

        CREATE TABLE seasonal_power (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            season text NOT NULL,
            best boolean NOT NULL,
            text text NOT NULL
        );

        -- Prydwen's ranking of memory fragment set combinations, per build ("Default").
        CREATE TABLE fragment_rank (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            build text,
            position int NOT NULL,
            sets text[] NOT NULL,
            performance text,
            notes text
        );

        -- Prydwen's partner ranking; the same partner can appear at several dupe levels.
        CREATE TABLE partner_rank (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            position int NOT NULL,
            partner text NOT NULL,
            dupes text,
            performance text,
            notes text
        );

        CREATE TABLE synergy (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            name text NOT NULL,
            points text[] NOT NULL
        );
        """
    )
    request_rebuild()
