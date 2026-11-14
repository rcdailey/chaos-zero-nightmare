"""Baseline: the schema before migrations existed.

Every statement is idempotent, so a database created by the old ``schema.sql`` adopts this
revision without changes.
"""

from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;

        -- Latest HTML of every Prydwen page, plus the outcome of parsing it.
        CREATE TABLE IF NOT EXISTS page (
            url text PRIMARY KEY,
            kind text NOT NULL,
            html text NOT NULL,
            fetched_at timestamptz NOT NULL,
            -- Fetch time of the HTML that produced the page's current rows. Differs from
            -- fetched_at while the newest HTML fails to parse and the previous rows are kept.
            data_fetched_at timestamptz,
            parse_error text
        );

        CREATE TABLE IF NOT EXISTS document (
            url text PRIMARY KEY REFERENCES page ON DELETE CASCADE,
            title text NOT NULL,
            body text NOT NULL,
            search tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('english', title), 'A') || to_tsvector('english', body)
            ) STORED
        );
        CREATE INDEX IF NOT EXISTS document_search_idx ON document USING gin (search);

        CREATE TABLE IF NOT EXISTS character (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            slug text PRIMARY KEY,
            name text NOT NULL,
            rarity int NOT NULL,
            element text NOT NULL,
            class text NOT NULL,
            updated_text text,
            image_url text,
            -- Prydwen's written review as [{"title", "text"}].
            sections jsonb NOT NULL
        );

        -- One row per card version. A base card has variant ''; Epiphany versions are 'I'-'V'.
        CREATE TABLE IF NOT EXISTS card (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text,
            category text NOT NULL,
            base_name text NOT NULL,
            variant text NOT NULL,
            name text NOT NULL,
            cost text NOT NULL,
            type text NOT NULL,
            color text,
            element text,
            tags text[] NOT NULL,
            text text NOT NULL,
            rules text,
            seasonal boolean,
            -- Prydwen's Epiphany rating in stars (1-5) and its Epiphany review.
            rating int,
            epiphany_review text,
            image_url text
        );

        CREATE TABLE IF NOT EXISTS potential (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            position text NOT NULL,
            name text NOT NULL,
            text text NOT NULL
        );

        CREATE TABLE IF NOT EXISTS manifest_ego (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            character_slug text NOT NULL,
            level int NOT NULL,
            name text NOT NULL,
            text text NOT NULL,
            performance text,
            review text
        );

        CREATE TABLE IF NOT EXISTS equipment (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            name text NOT NULL,
            rarity text NOT NULL,
            slot text NOT NULL,
            text text NOT NULL,
            stats jsonb NOT NULL,
            source text,
            image_url text
        );

        CREATE TABLE IF NOT EXISTS fragment_set (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            name text NOT NULL,
            bonuses jsonb NOT NULL,
            image_url text
        );

        CREATE TABLE IF NOT EXISTS partner (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            name text NOT NULL,
            rarity int NOT NULL,
            class text NOT NULL,
            passive_name text,
            passive_text text NOT NULL,
            image_url text
        );

        CREATE TABLE IF NOT EXISTS effect (
            url text NOT NULL REFERENCES page ON DELETE CASCADE,
            name text NOT NULL,
            type text,
            description text NOT NULL
        );

        CREATE INDEX IF NOT EXISTS character_name_trgm ON character USING gin (name gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS card_name_trgm ON card USING gin (name gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS card_character_idx ON card (character_slug);
        CREATE INDEX IF NOT EXISTS equipment_name_trgm ON equipment USING gin (name gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS fragment_set_name_trgm
            ON fragment_set USING gin (name gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS partner_name_trgm ON partner USING gin (name gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS effect_name_trgm ON effect USING gin (name gin_trgm_ops);
        """
    )
