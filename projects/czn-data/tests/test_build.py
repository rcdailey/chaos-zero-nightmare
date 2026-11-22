"""Contract: the build reports Prydwen ratings it cannot attach, an unmappable source value
fails the build without touching the merged tables, and a build reprocesses only the posts whose
content or splitting code changed and writes only the rows that differ.

Real Postgres holding one scraped Prydwen character page plus e7bot documents.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from czn_data import digest, store
from czn_data.build import BuildError, build
from czn_data.parse import ParsedPage, page_kind, parse_page

CHARACTER_URL = "https://www.prydwen.gg/chaos-zero-nightmare/characters/mei-lin"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()


@pytest.fixture
def prydwen(conn):
    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    kind = page_kind(CHARACTER_URL)
    store.save_page(conn, CHARACTER_URL, kind, CHARACTER_HTML, datetime.now(UTC), parsed, None)


def roster(ego: str) -> list[dict]:
    return [
        {
            "id": "1027",
            "name": "Mei Lin",
            "char_type": "CHAR_COMBATANT",
            "ego": ego,
            "job": "striker",
            "rarity": 5,
            "gender_type": "GENDER_FEMALE",
        }
    ]


def put(conn, path: str, body) -> None:
    conn.execute(
        "INSERT INTO e7bot.document (path, body, fetched_at) VALUES (%s, %s, now())"
        " ON CONFLICT (path) DO UPDATE SET body = excluded.body",
        (path, Jsonb(body)),
    )


def test_ratings_for_cards_e7bot_lacks_are_counted_and_still_served(conn, prydwen):
    put(conn, "czn/characters/list", roster("love"))
    put(conn, "czn/cards/list", [])  # e7bot knows Mei Lin but none of her cards

    result = build(conn)

    (rated,) = conn.execute(
        "SELECT count(*) FROM prydwen.card WHERE rating IS NOT NULL OR epiphany_review IS NOT NULL"
    ).fetchone()
    assert rated > 0
    assert result.unmatched["card"] == rated
    sources = conn.execute("SELECT DISTINCT source FROM card").fetchall()
    assert sources == [("prydwen",)]


def e7_card(card_id: str, text: str, versions=()) -> dict:
    return {
        "id": card_id,
        "name": "Chain Fire",
        "cost": 1,
        "card_category": "CARD_ATK",
        "rarity": "RARITY_RARE",
        "desc_display": text,
        "r_spark_cards": list(versions),
        "base_combatant": {"id": "1027"},
        "sort": "SORT_START",
        "talent": [],
        "tooltips": [],
    }


def test_ratings_of_a_card_prydwen_renames_land_on_the_same_versions(conn):
    """Prydwen calls the game's Chain Fire "Rapid Fire"; each rating must reach the game
    version with that Epiphany numeral, never a neighbour."""
    texts = {"": "50% Damage x 2", "I": "75% Damage x 2", "II": "30% Damage x 3"}
    ratings = {"I": 4, "II": 5}
    rows = [
        {
            "character_slug": "mei-lin",
            "category": "starting",
            "base_name": "Rapid Fire",
            "variant": variant,
            "name": f"Rapid Fire {variant}".strip(),
            "cost": "1",
            "type": "Attack",
            "tags": [],
            "text": text,
            "rating": ratings.get(variant),
        }
        for variant, text in texts.items()
    ]
    page = ParsedPage("Mei Lin", "", {"card": rows})
    store.save_page(conn, CHARACTER_URL, "character", "", datetime.now(UTC), page, None)
    put(conn, "czn/characters/list", roster("love"))
    versions = [e7_card(f"c_1027_srt1_rsp{n}", texts[v]) for n, v in ((1, "I"), (2, "II"))]
    put(conn, "czn/cards/list", [e7_card("c_1027_srt1", texts[""], versions)])

    build(conn)

    merged = conn.execute(
        "SELECT name, rating, prydwen_name, source FROM card ORDER BY name"
    ).fetchall()
    assert merged == [
        ("Chain Fire", None, "Rapid Fire", "e7bot"),
        ("Chain Fire I", 4, "Rapid Fire", "e7bot"),
        ("Chain Fire II", 5, "Rapid Fire", "e7bot"),
    ]


def test_a_card_prydwen_lists_twice_keeps_the_review_from_either_listing(conn):
    """Prydwen lists a card a character also creates twice (as her own card and as the
    created copy); the review on the second listing must reach the game's card."""
    review = "Most cards use very common divines"
    rows = [
        {
            "character_slug": "mei-lin",
            "category": category,
            "base_name": "Chain Fire",
            "variant": "",
            "name": "Chain Fire",
            "cost": "1",
            "type": "Attack",
            "tags": [],
            "text": text,
            "epiphany_review": epiphany_review,
        }
        for category, text, epiphany_review in (
            ("starting", "50% Damage x 2", None),
            ("generated", "40% Damage x 2", review),
        )
    ]
    page = ParsedPage("Mei Lin", "", {"card": rows})
    store.save_page(conn, CHARACTER_URL, "character", "", datetime.now(UTC), page, None)
    put(conn, "czn/characters/list", roster("love"))
    put(conn, "czn/cards/list", [e7_card("c_1027_srt1", "50% Damage x 2")])

    result = build(conn)

    merged = conn.execute("SELECT name, epiphany_review, source FROM card").fetchall()
    assert merged == [("Chain Fire", review, "e7bot")]
    assert result.unmatched["card"] == 0


def test_unknown_game_value_fails_the_build_and_keeps_merged_data(conn, prydwen):
    put(conn, "czn/characters/list", roster("love"))
    build(conn)
    before = conn.execute("SELECT slug, element, source FROM character").fetchall()

    put(conn, "czn/characters/list", roster("wonder"))
    with pytest.raises(BuildError, match="wonder"):
        build(conn)

    assert conn.execute("SELECT slug, element, source FROM character").fetchall() == before
    assert before == [("mei-lin", "Passion", "e7bot")]


def post(conn, article_id: int, content: str) -> None:
    conn.execute(
        "INSERT INTO stove.article"
        " (article_id, board, title, content, created_at, updated_at, fetched_at)"
        " VALUES (%s, 1, 'Notice', %s, now(), now(), now())"
        " ON CONFLICT (article_id) DO UPDATE SET content = excluded.content",
        (article_id, content),
    )


def test_unchanged_sources_rebuild_without_writing_or_splitting(conn, prydwen):
    put(conn, "czn/characters/list", roster("love"))
    post(conn, 1, "<p>Maintenance on Monday.</p>")
    build(conn)

    result = build(conn)

    assert not any(result.changed.values())
    assert not any(result.split.values())


def test_only_the_changed_post_is_split_again(conn):
    post(conn, 1, "<p>Maintenance on Monday.</p>")
    post(conn, 2, "<p>New event.</p>")
    build(conn)
    post(conn, 2, "<p>New event, extended.</p>")

    result = build(conn)

    assert result.split["build.post_sections"] == 1
    texts = {row[0] for row in conn.execute("SELECT text FROM post_section").fetchall()}
    assert texts == {"Maintenance on Monday.", "New event, extended."}


def test_after_a_splitter_change_every_post_is_split_again(conn, monkeypatch):
    post(conn, 1, "<p>Maintenance on Monday.</p>")
    post(conn, 2, "<p>New event.</p>")
    build(conn)
    monkeypatch.setattr(digest, "code", lambda *modules: "edited splitter")

    result = build(conn)

    assert result.split["build.post_sections"] == 2


def test_a_version_the_game_names_itself_keeps_that_name(conn):
    """A card can turn into versions with their own names (Flicker Blade splits into
    Shard:Light); players read that name on screen, so it must be the version's name."""
    put(conn, "czn/characters/list", roster("love"))
    renamed = e7_card("c_1027_srt1_mut1", "80% Damage") | {"name": "Shard:Light"}
    unnamed = e7_card("c_1027_srt1_mut2", "90% Damage")
    put(conn, "czn/cards/list", [e7_card("c_1027_srt1", "50% Damage x 2"), renamed, unnamed])

    build(conn)

    merged = conn.execute("SELECT name, base_name, variant FROM card ORDER BY name").fetchall()
    assert merged == [
        ("Chain Fire", "Chain Fire", ""),
        ("Chain Fire (mutation 2)", "Chain Fire", "mutation 2"),
        ("Shard:Light", "Chain Fire", "mutation 1"),
    ]
