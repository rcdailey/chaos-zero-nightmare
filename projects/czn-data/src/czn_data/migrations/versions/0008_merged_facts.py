"""Merged game facts in ``public``: the tables the MCP server reads.

``czn-data build`` fills them from the ``e7bot`` and ``prydwen`` schemas. Every row says where
its facts came from (``source``), links there (``url``), and when that data was fetched. Prydwen's
opinions (ratings, reviews, guide prose) sit on the same rows, linked by ``prydwen_url``.
Prydwen-only guide tables are exposed as views.
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0008"
down_revision = "0007"

PROVENANCE = """
    source text NOT NULL CHECK (source IN ('e7bot', 'prydwen')),
    url text NOT NULL,
    data_fetched_at timestamptz
"""

GUIDE_VIEWS = (
    "manifest_ego",
    "team",
    "deck",
    "character_equipment",
    "seasonal_power",
    "fragment_rank",
    "partner_rank",
    "synergy",
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE character (
            slug text PRIMARY KEY,
            game_id text,
            name text NOT NULL,
            rarity int NOT NULL,
            element text NOT NULL,
            class text NOT NULL,
            gender text,
            image_url text,
            updated_text text,
            -- Prydwen's written review as [{{"title", "text"}}].
            sections jsonb NOT NULL,
            prydwen_url text,
            {PROVENANCE}
        );

        -- One row per card version: the base card has variant ''; Epiphany versions are 'I'-'V';
        -- other game versions keep the game's code ('pot', 'lbk', 'mut1').
        CREATE TABLE card (
            character_slug text,
            game_id text,
            category text NOT NULL,
            base_name text NOT NULL,
            variant text NOT NULL,
            name text NOT NULL,
            alias text,
            cost text NOT NULL,
            type text NOT NULL,
            color text,
            element text,
            tags text[] NOT NULL,
            text text NOT NULL,
            rules text,
            seasonal boolean,
            rating int,
            epiphany_review text,
            target_neutral text,
            neutral_review text,
            divine_review text,
            image_url text,
            -- Prydwen's name for the base card, which its decks use, when it differs.
            prydwen_name text,
            prydwen_url text,
            {PROVENANCE}
        );

        CREATE TABLE potential (
            character_slug text NOT NULL,
            position text NOT NULL,
            name text NOT NULL,
            text text NOT NULL,
            {PROVENANCE}
        );

        CREATE TABLE partner (
            game_id text,
            name text NOT NULL,
            rarity int NOT NULL,
            class text NOT NULL,
            passive_name text,
            passive_text text NOT NULL,
            ego_name text,
            ego_cost int,
            ego_text text,
            hp int,
            attack int,
            defense int,
            image_url text,
            -- Prydwen's spelling, which its guides use; matched when it differs from the game's.
            prydwen_name text,
            prydwen_url text,
            {PROVENANCE}
        );

        CREATE TABLE equipment (
            game_id text,
            name text NOT NULL,
            rarity text NOT NULL,
            slot text NOT NULL,
            text text NOT NULL,
            stats jsonb NOT NULL,
            -- Where it drops, as Prydwen lists it.
            obtained_from text,
            image_url text,
            prydwen_name text,
            prydwen_url text,
            {PROVENANCE}
        );

        CREATE TABLE fragment_set (
            game_id text,
            name text NOT NULL,
            bonuses jsonb NOT NULL,
            image_url text,
            prydwen_name text,
            prydwen_url text,
            {PROVENANCE}
        );

        CREATE TABLE effect (
            name text NOT NULL,
            type text,
            description text NOT NULL,
            {PROVENANCE}
        );

        CREATE INDEX character_name_trgm ON character USING gin (name gin_trgm_ops);
        CREATE INDEX card_name_trgm ON card USING gin (name gin_trgm_ops);
        CREATE INDEX card_text_trgm ON card USING gin (text gin_trgm_ops);
        CREATE INDEX card_character_idx ON card (character_slug);
        CREATE INDEX equipment_name_trgm ON equipment USING gin (name gin_trgm_ops);
        CREATE INDEX fragment_set_name_trgm ON fragment_set USING gin (name gin_trgm_ops);
        CREATE INDEX partner_name_trgm ON partner USING gin (name gin_trgm_ops);
        CREATE INDEX effect_name_trgm ON effect USING gin (name gin_trgm_ops);
        """
    )
    for view in GUIDE_VIEWS:
        op.execute(
            f"""
            CREATE VIEW {view} AS
            SELECT g.*, p.data_fetched_at FROM prydwen.{view} g JOIN prydwen.page p USING (url)
            """
        )
    request_build()
