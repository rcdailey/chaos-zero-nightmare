"""One ``passage`` table that every search tool ranks, by BM25 and by meaning.

Each search tool (search_posts, search_game_systems, search_guides) searched its own tables with
its own SQL, scoring words with ``ts_rank``, which has no notion of how rare a word is. Every
searchable piece of text (post sections, Game8 sections, Prydwen sections, Help topics, and the
game's tutorial and popup text) is now one ``passage`` row with its embedding, ranked by
pg_textsearch's BM25 index. The section tables keep the fields each tool shows and lose their
search and embedding columns. pg_textsearch must be in ``shared_preload_libraries``.
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0023"
down_revision = "0022"


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS pg_textsearch;

        -- source: post, game8, guide, help, or game_text. key and position name the row the
        -- passage comes from: (article_id, position) of post_section, (url, position) of
        -- game8_section and guide_section, the Help topic's id with position 0, or the game
        -- text group's id. published_at: when the source last changed, for sources whose age
        -- matters (posts, Game8 pages, the game's own text).
        CREATE TABLE passage (
            source text NOT NULL,
            key text NOT NULL,
            position int NOT NULL,
            title text NOT NULL,
            heading text,
            text text NOT NULL,
            published_at timestamptz,
            embedding real[] NOT NULL,
            document text GENERATED ALWAYS AS (
                title || E'\\n' || coalesce(heading, '') || E'\\n' || text) STORED,
            PRIMARY KEY (source, key, position)
        );
        CREATE INDEX passage_bm25 ON passage USING bm25 (document)
            WITH (text_config = 'english');
        CREATE INDEX passage_title_trgm ON passage USING gin (title gin_trgm_ops);
        CREATE INDEX passage_heading_trgm ON passage USING gin (heading gin_trgm_ops);

        ALTER TABLE post_section DROP COLUMN search, DROP COLUMN embedding;
        ALTER TABLE game8_section DROP COLUMN search, DROP COLUMN embedding;
        ALTER TABLE guide_section DROP COLUMN search, DROP COLUMN embedding;
        ALTER TABLE game_help DROP COLUMN search, DROP COLUMN embedding;
        DROP INDEX IF EXISTS post_section_title_trgm, post_section_heading_trgm,
            game8_section_heading_trgm, guide_section_heading_trgm, game_help_title_trgm;
        """
    )
    request_build()
