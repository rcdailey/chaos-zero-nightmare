"""Prydwen pages split into searchable sections, as Game8's are.

``prydwen.guide_section`` holds each page's sections as parsed from its HTML (replaced only by a
clean parse, like the other ``prydwen`` tables); ``public.guide_section`` adds each section's
embedding (``czn-data build``). ``prydwen.document.body`` becomes the text the sections are cut
from, without the site's menu, and its search column goes: search_guides searches sections.
"""

from alembic import op

from czn_data.migrate import request_rebuild

revision = "0021"
down_revision = "0020"


def upgrade() -> None:
    op.execute(
        """
        -- One section of a page, or one piece of a long one. heading names its parents
        -- ("A > B"). "offset" and length locate text in prydwen.document.body.
        CREATE TABLE prydwen.guide_section (
            url text NOT NULL REFERENCES prydwen.page ON DELETE CASCADE,
            position int NOT NULL,
            heading text NOT NULL,
            text text NOT NULL,
            "offset" int NOT NULL,
            length int NOT NULL,
            PRIMARY KEY (url, position)
        );

        CREATE TABLE guide_section (
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
        CREATE INDEX guide_section_search_idx ON guide_section USING gin (search);
        CREATE INDEX guide_section_heading_trgm ON guide_section USING gin (heading gin_trgm_ops);

        ALTER TABLE prydwen.document DROP COLUMN search;
        CREATE INDEX document_title_trgm ON prydwen.document USING gin (title gin_trgm_ops);
        """
    )
    request_rebuild()
