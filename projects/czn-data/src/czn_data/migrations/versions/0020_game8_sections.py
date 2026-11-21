"""Game8 pages split into searchable sections, in ``public``.

``czn-data build`` fills both tables from ``game8.page`` (see ``czn_data.game8``).
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0020"
down_revision = "0019"


def upgrade() -> None:
    op.execute(
        """
        -- One Game8 page: its text as get_page serves it, and Game8's update date.
        CREATE TABLE game8_page (
            url text PRIMARY KEY,
            title text NOT NULL,
            -- The page's opening text, before its first heading.
            summary text NOT NULL,
            body text NOT NULL,
            updated_at timestamptz,
            data_fetched_at timestamptz NOT NULL
        );

        -- One section of a page, or one piece of a long one. heading names its parents
        -- ("A > B > C"). "offset" and length locate text in game8_page.body.
        CREATE TABLE game8_section (
            url text NOT NULL,
            position int NOT NULL,
            heading text NOT NULL,
            text text NOT NULL,
            "offset" int NOT NULL,
            length int NOT NULL,
            embedding real[],
            search tsvector GENERATED ALWAYS AS (to_tsvector('english',
                heading || ' ' || text)) STORED,
            PRIMARY KEY (url, position)
        );
        CREATE INDEX game8_section_search_idx ON game8_section USING gin (search);
        CREATE INDEX game8_section_heading_trgm ON game8_section USING gin (heading gin_trgm_ops);
        CREATE INDEX game8_page_title_trgm ON game8_page USING gin (title gin_trgm_ops);
        """
    )
    request_build()
