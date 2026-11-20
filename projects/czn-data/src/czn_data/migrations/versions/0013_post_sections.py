"""Official Stove posts split into searchable sections, in ``public``.

``czn-data build`` fills it from ``stove.article`` (see ``czn_data.posts``).
"""

from alembic import op

from czn_data.migrate import request_build

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.execute(
        """
        -- One section of a post. heading names its parent section ("■ A > ◆ B"); it is null for
        -- text before the first marker. starts_on and ends_on hold the section's period when
        -- its text gives one in a form that parses.
        CREATE TABLE post_section (
            article_id text NOT NULL,
            board text NOT NULL,
            post_title text NOT NULL,
            posted_at timestamptz NOT NULL,
            position int NOT NULL,
            heading text,
            text text NOT NULL,
            starts_on date,
            ends_on date,
            url text NOT NULL,
            data_fetched_at timestamptz NOT NULL,
            search tsvector GENERATED ALWAYS AS (to_tsvector('english',
                post_title || ' ' || coalesce(heading, '') || ' ' || text)) STORED,
            PRIMARY KEY (article_id, position)
        );
        CREATE INDEX post_section_search_idx ON post_section USING gin (search);
        CREATE INDEX post_section_title_trgm ON post_section USING gin (post_title gin_trgm_ops);
        CREATE INDEX post_section_heading_trgm ON post_section USING gin (heading gin_trgm_ops);
        """
    )
    request_build()
