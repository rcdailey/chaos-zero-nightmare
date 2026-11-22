"""Chaos events as graphs of steps (``czn_data.build._encounters``): an event's versions stay
separate, and the follow-up steps its choices open, with their own choices, become rows."""

from alembic import op

from czn_data.migrate import request_build

revision = "0026"
down_revision = "0025"


def upgrade() -> None:
    op.execute(
        """
        DROP TABLE encounter_option;

        -- One version of a Chaos event. Versions of one event share version_group and name;
        -- the game picks one. steps lists every step the version can reach, its first step
        -- first, then in breadth-first order of the links between them.
        DELETE FROM encounter;
        ALTER TABLE encounter
            ADD COLUMN version_group text NOT NULL,
            ADD COLUMN steps text[] NOT NULL;
        CREATE INDEX encounter_version_group_idx ON encounter (version_group);

        -- One screen of an event, keyed by the game's id. shown_choices is how many choices
        -- the screen offers; auto_choice names a choice the game takes without asking;
        -- appears_when holds the game's condition expressions. extra keeps every field of the
        -- game's step the build does not interpret, verbatim.
        CREATE TABLE encounter_step (
            id text PRIMARY KEY,
            name text NOT NULL,
            shown_choices int,
            auto_choice text,
            appears_when text[] NOT NULL,
            extra jsonb NOT NULL
        );

        -- One choice on a step. results is [{"outcome", "id", "text", "battle", "rewards",
        -- "next_step", "extra"}]: outcome is success or failure (a dice choice has both),
        -- rewards is [{"id", "class", "type", "text"}] (class as in encounter.reward_classes),
        -- next_step the encounter_step it opens. requirements is [{"condition", "tooltip"}].
        CREATE TABLE encounter_choice (
            step_id text NOT NULL REFERENCES encounter_step ON DELETE CASCADE,
            position int NOT NULL,
            id text NOT NULL,
            text text NOT NULL,
            kind text,
            random_group text,
            dice_skill text,
            dice_target int,
            battle text,
            requirements jsonb NOT NULL,
            appears_when text[] NOT NULL,
            results jsonb NOT NULL,
            reward_classes text[] NOT NULL,
            extra jsonb NOT NULL,
            PRIMARY KEY (step_id, position)
        );
        """
    )
    request_build()
