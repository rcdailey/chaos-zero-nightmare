"""Deck codes and farming zones on Prydwen's guide decks.

A deck code is the string players paste into the game's deck import to load the deck. The
``deck`` view in ``public`` lists the table's columns as they were when it was created, so it is
recreated to expose the new ones.
"""

from alembic import op

from czn_data.migrate import request_rebuild

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE prydwen.deck
            ADD COLUMN code text,
            -- The Chaos the deck is built to farm ("Celestial Bulwark (Season-5)").
            ADD COLUMN farming_zone text;
        DROP VIEW deck;
        CREATE VIEW deck AS
        SELECT g.*, p.data_fetched_at FROM prydwen.deck g JOIN prydwen.page p USING (url);
        """
    )
    request_rebuild()
