"""Contract: MCP tools answer from the stored data in the shapes their descriptions promise.

Tools are called in-process through the MCP server, against real Postgres holding one scraped
character page.
"""

from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

from czn_data import store
from czn_data.parse import ParsedPage, page_kind, parse_page
from czn_data.server import build_server

CHARACTER_URL = "https://www.prydwen.gg/chaos-zero-nightmare/characters/mei-lin"
FRAGMENTS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/memory-fragments"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()


@pytest.fixture
def call(conn, database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    store.save_page(
        conn,
        CHARACTER_URL,
        page_kind(CHARACTER_URL),
        CHARACTER_HTML,
        datetime.now(UTC),
        parsed,
        None,
    )
    server = build_server(None, None)

    def call(tool: str, **arguments):
        result = anyio.run(server.call_tool, tool, arguments)
        return result.structured_content["result"]

    return call


def test_get_character_lists_sections_and_returns_one_at_a_time(call):
    summary = call("get_character", name="mei lin")
    assert summary["name"] == "Mei Lin"
    assert {"cards", "teams", "decks", "equipment", "Pros", "Stats"} <= set(summary["sections"])
    assert "potentials" in summary["sections"]

    teams = call("get_character", name="mei lin", section="teams")["teams"]
    assert teams == [
        {"name": "Spire Fire", "tier": "T1", "members": ["Mei Lin", "Veronica", "Tiphera"]}
    ]

    stats = call("get_character", name="mei lin", section="stats")["sections"]
    assert stats == [{"title": "Stats", "text": "CRIT Rate > CRIT DMG"}]


def test_match_card_text_finds_the_variant_from_screen_text(call):
    best = call("match_card_text", text="For 1 turn each Passion Card used 1 extra Hit")[0]
    assert (best["name"], best["variant"], best["alias"]) == (
        "Rising Dragon Spire V",
        "V",
        "Ascended",
    )


def test_find_teams_by_member(call):
    (team,) = call("find_teams", character="veronika")
    assert (team["name"], team["listed_for"]) == ("Spire Fire", ["Mei Lin"])


def test_recommended_by_lists_every_place_an_item_is_recommended(call, conn):
    fragments = ParsedPage(
        "Memory Fragments",
        "Executioner's Tool",
        {"fragment_set": [{"name": "Executioner's Tool", "bonuses": []}]},
    )
    store.save_page(conn, FRAGMENTS_URL, "memory_fragments", "", datetime.now(UTC), fragments, None)

    (row,) = call("recommended_by", name="executioners tool")
    assert row["character"] == "Mei Lin"
    assert sorted(row["places"]) == [
        "deck: Standard Dragon Spire",
        "fragment ranking: Default #1 100.00%",
    ]
