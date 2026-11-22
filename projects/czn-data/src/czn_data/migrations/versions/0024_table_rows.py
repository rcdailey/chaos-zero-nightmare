"""Table cells written as paragraphs stay on their row (``czn_data.text``): parse the stored
pages again so every table-derived text has one line per row."""

from czn_data.migrate import request_rebuild

revision = "0024"
down_revision = "0023"


def upgrade() -> None:
    request_rebuild()
