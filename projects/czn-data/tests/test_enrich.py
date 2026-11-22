"""Contract: enrich derives gender and identity prints from stored portraits, and gear prints
from stored equipment icons, once per image and per print code (old code's prints are dropped),
and reports and retries every image it could not process. The models are replaced by functions; the database is real Postgres.
"""

from datetime import UTC, datetime

import pytest

from czn_data.e7bot import RARITY_BACKGROUNDS
from czn_data.enrich import EnrichResult, compute_gear_prints, compute_identities, tag_genders
from czn_data.hub import ModelUnavailable

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


def test_stored_portraits_get_an_identity_print_once_per_code_and_failures_retry(stored):
    def broken(image: bytes) -> list[float]:
        raise ValueError("not an image")

    failed = EnrichResult()
    compute_identities(stored, "code-a", broken, failed)
    assert failed.failures == {"identity": 1}

    computed = []

    def identity_print(image: bytes) -> list[float]:
        computed.append(image)
        return [0.5, 0.25]

    result = EnrichResult()
    compute_identities(stored, "code-a", identity_print, result)
    compute_identities(stored, "code-a", identity_print, result)
    assert computed == [b"art"]

    compute_identities(stored, "code-b", identity_print, result)

    assert result.ok
    assert computed == [b"art", b"art"]
    assert stored.execute(
        "SELECT image_url, digest, print FROM prydwen.portrait_identity"
    ).fetchall() == [(PORTRAIT, "code-b", [0.5, 0.25])]


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


def store_asset(conn, path: str, image: bytes) -> None:
    conn.execute(
        "INSERT INTO e7bot.asset (path, image, fetched_at) VALUES (%s, %s, now())", (path, image)
    )


def test_gear_icons_get_prints_on_every_background_once_per_code(conn):
    store_asset(conn, "item/relics/relic_1.webp", b"icon")

    def gear_prints(icon: bytes, backgrounds: list[bytes]) -> list[list[float]]:
        computed.append((icon, backgrounds))
        return [[float(i)] for i in range(len(backgrounds))]

    computed = []
    missing_backgrounds = EnrichResult()
    compute_gear_prints(conn, "code-a", gear_prints, missing_backgrounds)
    assert missing_backgrounds.failures == {"gear": 1} and computed == []

    for number, path in enumerate(RARITY_BACKGROUNDS):
        store_asset(conn, path, f"bg{number}".encode())
    result = EnrichResult()
    compute_gear_prints(conn, "code-a", gear_prints, result)
    compute_gear_prints(conn, "code-a", gear_prints, result)
    backgrounds = [f"bg{n}".encode() for n in range(len(RARITY_BACKGROUNDS))]
    assert computed == [(b"icon", backgrounds)]

    compute_gear_prints(conn, "code-b", gear_prints, result)

    assert result.ok
    assert computed == [(b"icon", backgrounds)] * 2
    assert conn.execute("SELECT icon_name, digest, prints FROM e7bot.gear_print").fetchall() == [
        ("relics/relic_1", "code-b", [[0.0], [1.0], [2.0], [3.0]])
    ]


def test_unavailable_tagger_is_reported_once(stored):
    stored.execute(
        "INSERT INTO prydwen.portrait (image_url, image, fetched_at) VALUES ('other', 'x', now())"
    )

    def unavailable(image: bytes) -> str:
        raise ModelUnavailable("model download failed")

    result = EnrichResult()
    tag_genders(stored, unavailable, result)
    assert result.failures == {"gender": 1}
