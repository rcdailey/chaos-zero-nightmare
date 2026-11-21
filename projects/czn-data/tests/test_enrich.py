"""Contract: enrich derives gender and appearance from stored portraits only, once per image,
and reports and retries every portrait it could not tag. The tagger is replaced by a function; the database is
real Postgres.
"""

from datetime import UTC, datetime

import pytest

from czn_data.enrich import EnrichResult, tag_appearances, tag_genders
from czn_data.tagger import TaggerUnavailable

PORTRAIT = "https://cdn.prydwen.gg/images/chaos/characters/mei-lin_card.webp"


@pytest.fixture
def stored(conn):
    conn.execute(
        "INSERT INTO prydwen.portrait (image_url, image, fetched_at) VALUES (%s, %s, %s)",
        (PORTRAIT, b"art", datetime.now(UTC)),
    )
    return conn


def genders(conn) -> list[tuple]:
    return conn.execute("SELECT image_url, gender FROM prydwen.portrait_gender").fetchall()


def test_stored_portraits_get_a_gender_once(stored):
    tagged = []

    def gender(image: bytes) -> str:
        tagged.append(image)
        return "female"

    result = EnrichResult()
    tag_genders(stored, gender, result)
    tag_genders(stored, gender, result)

    assert result.ok
    assert tagged == [b"art"]
    assert genders(stored) == [(PORTRAIT, "female")]


def test_stored_portraits_get_an_appearance_once_and_failures_retry(stored):
    def broken(image: bytes) -> list[str]:
        raise ValueError("not an image")

    failed = EnrichResult()
    tag_appearances(stored, broken, failed)
    assert failed.failures == {"appearance": 1}

    tagged = []

    def appearance(image: bytes) -> list[str]:
        tagged.append(image)
        return ["pink hair", "twintails"]

    result = EnrichResult()
    tag_appearances(stored, appearance, result)
    tag_appearances(stored, appearance, result)

    assert result.ok
    assert tagged == [b"art"]
    assert stored.execute("SELECT image_url, tags FROM prydwen.portrait_appearance").fetchall() == [
        (PORTRAIT, ["pink hair", "twintails"])
    ]


def test_portrait_that_fails_is_reported_and_retried_next_run(stored):
    def broken(image: bytes) -> str:
        raise ValueError("not an image")

    failed = EnrichResult()
    tag_genders(stored, broken, failed)
    assert failed.failures == {"gender": 1}

    retried = EnrichResult()
    tag_genders(stored, lambda image: "male", retried)
    assert retried.ok
    assert genders(stored) == [(PORTRAIT, "male")]


def test_unavailable_tagger_is_reported_once(stored):
    stored.execute(
        "INSERT INTO prydwen.portrait (image_url, image, fetched_at) VALUES ('other', 'x', now())"
    )

    def unavailable(image: bytes) -> str:
        raise TaggerUnavailable("model download failed")

    result = EnrichResult()
    tag_genders(stored, unavailable, result)
    assert result.failures == {"gender": 1}
