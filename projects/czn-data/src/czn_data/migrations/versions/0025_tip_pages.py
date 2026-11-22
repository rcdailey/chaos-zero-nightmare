"""The game's tip pages and keyword tooltips become search passages
(``czn_data.gamefiles.game_texts``): build again so search_game_systems finds them."""

from czn_data.migrate import request_build

revision = "0025"
down_revision = "0024"


def upgrade() -> None:
    request_build()
