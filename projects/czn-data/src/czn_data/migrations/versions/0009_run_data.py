"""Chaos run data in ``public``: events, Divine Epiphanies, and leaderboard decks.

``czn-data build`` fills these from the ``e7bot`` schema only; Prydwen has none of it.
"""

from alembic import op

from czn_data.migrate import request_build, request_rebuild

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.execute(
        """
        -- A Chaos event (question-mark node), with every stage it can move through. chaos lists
        -- the Chaos it belongs to; any_chaos marks common events that can appear in every Chaos.
        CREATE TABLE encounter (
            id text PRIMARY KEY,
            name text NOT NULL,
            rarity text,
            pack text,
            chaos text[] NOT NULL,
            any_chaos boolean NOT NULL,
            -- Union of its options' reward classes (see encounter_option).
            reward_classes text[] NOT NULL,
            url text NOT NULL,
            data_fetched_at timestamptz
        );

        -- One answer of an event stage. Stage 1 is where the event starts; an answer's
        -- leads_to names the stage it opens. Some events start at one of several stages at
        -- random. rewards is [{"outcome", "class", "type", "text"}], where outcome is success
        -- or failure (answers decided by a dice roll have both) and class is copy, removal,
        -- epiphany, divine_epiphany, equipment, card, or other.
        CREATE TABLE encounter_option (
            encounter_id text NOT NULL REFERENCES encounter ON DELETE CASCADE,
            stage int NOT NULL,
            position int NOT NULL,
            text text NOT NULL,
            requirements text[] NOT NULL,
            rewards jsonb NOT NULL,
            reward_classes text[] NOT NULL,
            leads_to int,
            PRIMARY KEY (encounter_id, stage, position)
        );

        CREATE VIEW chaos AS
        SELECT DISTINCT unnest(chaos) AS name FROM encounter;

        -- One Divine Epiphany effect. The game repeats an effect per god and rarity; they are
        -- one row here, listing every god that offers it.
        CREATE TABLE divine_epiphany (
            id text PRIMARY KEY,
            gods text[] NOT NULL,
            rarities text[] NOT NULL,
            text text NOT NULL,
            -- Which cards it can apply to: card types and the cost range (0 to 99: any).
            card_types text[] NOT NULL,
            cost_min int,
            cost_max int,
            url text NOT NULL,
            data_fetched_at timestamptz
        );

        -- Leaderboard of a seasonal mode: rank by score within a season. Player identities are
        -- not stored.
        CREATE TABLE leaderboard_entry (
            season text NOT NULL,
            rank int NOT NULL,
            score bigint NOT NULL,
            PRIMARY KEY (season, rank)
        );

        -- One character's Save Data on a leaderboard team. cards is
        -- [{"base", "variant", "divine_epiphany"}], one item per card in the deck.
        CREATE TABLE leaderboard_deck (
            season text NOT NULL,
            rank int NOT NULL,
            team int NOT NULL,
            character_slug text NOT NULL,
            cards jsonb NOT NULL,
            equipment text[] NOT NULL,
            url text NOT NULL,
            data_fetched_at timestamptz,
            FOREIGN KEY (season, rank) REFERENCES leaderboard_entry ON DELETE CASCADE
        );
        CREATE INDEX leaderboard_deck_character_idx ON leaderboard_deck (character_slug);

        CREATE INDEX encounter_name_trgm ON encounter USING gin (name gin_trgm_ops);
        CREATE INDEX divine_epiphany_text_trgm ON divine_epiphany USING gin (text gin_trgm_ops);
        """
    )
    request_build()
    # Guide page text now separates adjacent spans, so full-text search finds their words.
    request_rebuild()
