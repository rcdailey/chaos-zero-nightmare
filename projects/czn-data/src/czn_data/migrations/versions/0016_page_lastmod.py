"""Each stored Prydwen page's sitemap date, so a scrape downloads only pages that changed.

Existing rows get no date, so the next scrape downloads every page once.
"""

from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.execute(
        """
        -- The sitemap's last-modified date for the stored html (NULL when the sitemap gives
        -- none), and the sitemap that listed the page (NULL for pages the scrape adds itself).
        ALTER TABLE prydwen.page ADD COLUMN lastmod timestamptz, ADD COLUMN sitemap text;
        """
    )
