"""Contract: MCP tools answer from the stored data in the shapes their descriptions promise.

Tools are called in-process through the MCP server, against real Postgres holding one scraped
Prydwen character page; tests that need game data add e7bot documents. Every call rebuilds the
merged tables first, as each scrape does.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from psycopg.types.json import Jsonb
from test_parse import banner_html

from czn_data import store
from czn_data.build import build
from czn_data.parse import ParsedPage, page_kind, parse_page
from czn_data.server import build_server

CHARACTER_URL = "https://www.prydwen.gg/chaos-zero-nightmare/characters/mei-lin"
FRAGMENTS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/memory-fragments"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()
BANNERS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/banners"


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
        build(conn)
        result = anyio.run(server.call_tool, tool, arguments)
        if result.structured_content is None:  # a plain dict result travels as JSON text
            return json.loads(result.content[0].text)
        return result.structured_content["result"]

    return call


MEI_LIN_PORTRAIT = "https://cdn.prydwen.gg/images/chaos/characters/mei-lin_card.webp"


def tag_female(conn):
    conn.execute(
        "INSERT INTO prydwen.portrait_gender (image_url, gender, tagged_at)"
        " VALUES (%s, 'female', now())",
        (MEI_LIN_PORTRAIT,),
    )


def add_e7bot(conn, path: str, body):
    conn.execute(
        "INSERT INTO e7bot.document (path, body, fetched_at) VALUES (%s, %s, now())",
        (path, Jsonb(body)),
    )


def test_get_character_lists_sections_and_returns_one_at_a_time(call, conn):
    tag_female(conn)
    summary = call("get_character", name="mei lin")
    assert (summary["name"], summary["gender"]) == ("Mei Lin", "female")
    assert {"cards", "teams", "decks", "equipment", "Pros", "Stats"} <= set(summary["sections"])
    assert "potentials" in summary["sections"]

    teams = call("get_character", name="mei lin", section="teams")["teams"]
    assert teams == [
        {"name": "Spire Fire", "tier": "T1", "members": ["Mei Lin", "Veronica", "Tiphera"]}
    ]

    stats = call("get_character", name="mei lin", section="stats")["sections"]
    assert stats == [{"title": "Stats", "text": "CRIT Rate > CRIT DMG"}]


def test_guide_decks_carry_deck_code_and_farming_zone(call, conn):
    entry = {"deck_code": "v6l8cOji", "farming_zone": "Celestial Bulwark (Season-5)"}
    push = json.dumps(json.dumps({"decks": [entry]}, separators=(",", ":")))
    html = CHARACTER_HTML.replace(
        "</body>", f"<script>self.__next_f.push([1,{push}])</script></body>"
    )
    parsed = parse_page(CHARACTER_URL, html)
    store.save_page(conn, CHARACTER_URL, "character", html, datetime.now(UTC), parsed, None)

    (deck,) = call("get_character", name="mei lin", section="decks")["decks"]
    (planned,) = call("plan_deck", character="mei lin")["prydwen_decks"]

    expected = {"name": "Standard Dragon Spire"} | {
        "code": "v6l8cOji",
        "farming_zone": "Celestial Bulwark (Season-5)",
    }
    assert deck == deck | expected
    assert planned == planned | expected


def test_list_banners_shows_live_then_upcoming_and_hides_ended(call, conn):
    now = datetime.now(UTC)

    def save(url, name, start, end):
        html = banner_html(name, start.isoformat(), end.isoformat())
        store.save_page(conn, url, "banners", html, now, parse_page(BANNERS_URL, html), None)

    day = timedelta(days=1)
    save(BANNERS_URL, "Dahlia", now + day, now + 9 * day)
    save(f"{BANNERS_URL}?live", "Blazebell", now - day, now + 2 * day)
    save(f"{BANNERS_URL}?ended", "Tiphera", now - 9 * day, now - day)

    banners = call("list_banners")

    assert [(b["status"], b["name"]) for b in banners if b["kind"] == "character"] == [
        ("live", "Blazebell"),
        ("upcoming", "Dahlia"),
    ]


def test_search_guides_finds_pages_missing_some_query_words(call):
    (page,) = call("search_guides", query="Mei Lin Policy Office")
    assert page["url"] == CHARACTER_URL


def test_search_guides_ranks_pages_with_more_query_words_first(call, conn):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/guides/team-building"
    body = "Team building. " * 50
    page = ParsedPage("Team Building", body)
    store.save_page(conn, url, "guide", body, datetime.now(UTC), page, None)

    pages = call("search_guides", query="Spire Fire team")

    assert [p["url"] for p in pages] == [CHARACTER_URL, url]


def test_search_guides_ranks_pages_with_every_word_by_closeness_not_repetition(call, conn):
    guides = "https://www.prydwen.gg/chaos-zero-nightmare/guides"
    pages = {
        f"{guides}/close": "Zorblax quuxite guide.",
        f"{guides}/repeated": "Zorblax intro. " + "Filler text. " * 200 + "Quuxite. " * 30,
    }
    for url, body in pages.items():
        parsed = ParsedPage(url, body)
        store.save_page(conn, url, "guide", body, datetime.now(UTC), parsed, None)

    (top,) = call("search_guides", query="zorblax quuxite", limit=1)

    assert top["url"] == f"{guides}/close"


def add_post(conn, article_id: str, title: str, content: str):
    conn.execute(
        "INSERT INTO stove.article VALUES (%s, 142421, %s, %s, '2026-11-11', now(), now())",
        (article_id, title, content),
    )


def test_search_posts_by_words_typo_title_and_active_date(call, conn):
    add_post(
        conn,
        "1",
        "11/11(Wed) Update Notice",
        "<p>■ New Combatant [Blazebell] Rescue Rate-Up</p>"
        "<p>◆ Rate-Up Period: 11/11/2026 after the maintenance – 12/02/2026 before the"
        " maintenance</p><p>■ Season Restrictions</p><p>Cards: Acid Gas</p>",
    )
    add_post(conn, "2", "Teatime Event", "<p>◆ Event Period: 11/01/2026 – 11/05/2026</p>")

    (restriction,) = [
        r
        for r in call("search_posts", query="season restrictions")["results"]
        if "Acid" in r.get("text", "")
    ]
    assert restriction | {"url": None} == restriction | {
        "post_title": "11/11(Wed) Update Notice",
        "board": "Update Notice",
        "heading": "■ Season Restrictions",
        "url": None,
    }
    assert restriction["url"].endswith("/view/1")

    typo = call("search_posts", query="Teatme Evnt")["results"]
    assert typo[0]["post_title"] == "Teatime Event"

    active = call("search_posts", active_on="2026-11-20")
    assert [(r["heading"], r["starts_on"], r["ends_on"]) for r in active["results"]] == [
        ("■ New Combatant [Blazebell] Rescue Rate-Up", "2026-11-11", "2026-12-02"),
        (
            (
                "■ New Combatant [Blazebell] Rescue Rate-Up > ◆ Rate-Up Period: 11/11/2026 after"
                " the maintenance – 12/02/2026 before the maintenance"
            ),
            "2026-11-11",
            "2026-12-02",
        ),
    ]


def test_search_posts_finds_sections_in_players_words(call, conn):
    """Runs the real embedding model (downloaded into FASTEMBED_CACHE_PATH on first use)."""
    add_post(
        conn,
        "1",
        "10/21(Wed) Update Notice",
        "<p>■ Celestial Bulwark Opens</p>"
        "<p>◆ Season Restrictions</p><p>Cards: Acid Gas. Equipment: Lead-Colored Holy Water.</p>"
        "<p>◆ Supply Store</p><p>Item | Limit</p><p>Aether | 5</p>"
        "<p>■ Bug Fixes</p><p>Fixed Celestial Bulwark boss icons.</p>"
        "<p>■ Spectral Cube Improvements</p><p>New products for Spectral Cubes.</p>",
    )

    def top(query: str) -> str:
        return call("search_posts", query=query)["results"][0]["heading"]

    assert top("what is banned in Celestial Bulwark?").endswith("Season Restrictions")
    assert top("what's in the Celestial Bulwark shop?").endswith("Supply Store")


def test_search_posts_pages_through_every_match(call, conn):
    sections = "".join(f"<p>◆ Event {n} Period: 11/01/2026 – 11/30/2026</p>" for n in range(3))
    add_post(conn, "1", "Events", sections)

    first = call("search_posts", active_on="2026-11-20", limit=2)
    rest = call("search_posts", active_on="2026-11-20", limit=2, offset=2)

    assert (first["total"], len(first["results"])) == (3, 2)
    assert "raise offset" in first["note"]
    assert (len(rest["results"]), "note" in rest) == (1, False)
    headings = {r["heading"] for r in first["results"] + rest["results"]}
    assert len(headings) == 3


def test_search_posts_needs_query_or_valid_date(call):
    with pytest.raises(Exception, match="Give query"):
        call("search_posts")
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        call("search_posts", active_on="next week")


def test_list_characters_filters_by_gender_and_describes_appearance(call, conn):
    assert call("list_characters", gender="female") == []
    tag_female(conn)
    conn.execute(
        "INSERT INTO prydwen.portrait_appearance (image_url, tags, tagged_at)"
        " VALUES (%s, %s, now())",
        (MEI_LIN_PORTRAIT, ["pink hair", "twintails"]),
    )
    assert call("list_characters", gender="Female") == [
        {
            "name": "Mei Lin",
            "rarity": 5,
            "element": "Passion",
            "class": "Striker",
            "gender": "female",
            "appearance": "pink hair, twintails",
            "image_url": MEI_LIN_PORTRAIT,
        }
    ]
    assert call("get_character", name="mei lin")["appearance"] == ["pink hair", "twintails"]
    assert call("list_characters", gender="male") == []


def test_synergies_combine_synergy_lists_and_teams(call):
    veronica, tiphera = call("synergies", character="mei lin")
    assert veronica == {
        "for": "Mei Lin",
        "name": "Veronica",
        "sources": ["own synergy list", "team"],
        "teams": ["Spire Fire (T1)"],
        "reasons": ["Doubles as a draw unit."],
    }
    assert (tiphera["name"], tiphera["sources"]) == ("Tiphera", ["team"])


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


EFFECTS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/guides/effects-dictionary"
PARTNERS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/partners"


@pytest.fixture
def effects(conn):
    rows = [
        {"name": "Exhaust", "type": "Tag", "description": "When used, move to Exhaust pile."},
        {"name": "Equilux", "type": "Unique effect", "description": "Polarizes the card."},
    ]
    page = ParsedPage("Effects", "", {"effect": rows})
    store.save_page(conn, EFFECTS_URL, "effects", "", datetime.now(UTC), page, None)


def test_lookup_accepts_keyword_as_effect_and_rejects_unknown_kinds(call, effects):
    (found,) = call("lookup", name="exhaust", kind="keyword")
    assert (found["kind"], found["name"]) == ("effect", "Exhaust")
    with pytest.raises(Exception, match="kind"):
        call("lookup", name="exhaust", kind="spell")


def test_lookup_finds_terms_defined_inside_other_effects(call, effects):
    (found,) = call("lookup", name="Polarization", kind="effect")
    assert found["name"] == "Equilux"


def test_get_partner_returns_ego_skill(call, conn):
    sig = {
        "name": "Sig",
        "rarity": 5,
        "class": "Striker",
        "passive_text": "Crit up.",
        "ego_name": "Frail Dominion of Kyrken",
        "ego_cost": 3,
        "ego_text": "2 Dominion Shard.",
    }
    page = ParsedPage("Partners", "", {"partner": [sig]})
    store.save_page(conn, PARTNERS_URL, "partners", "", datetime.now(UTC), page, None)
    found = call("get_partner", name="sig")
    assert (found["ego_name"], found["ego_cost"]) == ("Frail Dominion of Kyrken", 3)


def test_list_cards_filters_by_color(call):
    listing = call("list_cards", character="meilin", color="white")
    (white,) = listing["results"]
    assert (listing["total"], white["name"], white["color"]) == (1, "Strike", "White")


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


def card(card_id: str, name: str, cost: int, text: str, versions=(), sort="SORT_UNIQUE") -> dict:
    return {
        "id": card_id,
        "name": name,
        "cost": cost,
        "card_category": "CARD_ATK",
        "rarity": "RARITY_RARE",
        "desc_display": text,
        "r_spark_cards": list(versions),
        "base_combatant": {"id": "1027"},
        "sort": sort,
        "talent": [],
        "tooltips": [],
    }


@pytest.fixture
def game_data(conn):
    """e7bot's Mei Lin: one starting and one unique card with an Epiphany V, one Chaos event,
    and one leaderboard team whose Save Data took Rising Dragon Spire V."""
    add_e7bot(
        conn,
        "czn/characters/list",
        [
            {
                "id": "1027",
                "name": "Mei Lin",
                "names": {"en": "Mei Lin"},
                "char_type": "CHAR_COMBATANT",
                "ego": "love",
                "job": "striker",
                "rarity": 5,
                "gender_type": "GENDER_FEMALE",
            }
        ],
    )
    spire_v = card("c_1027_uni1_rsp5", "Rising Dragon Spire", 0, "<color_orange>9</> Hits")
    add_e7bot(
        conn,
        "czn/cards/list",
        [
            card("c_1027_srt1", "Strike", 1, "100% Damage", sort="SORT_START"),
            # Her Ego skill: no deck holds it, yet it is never a removal.
            card("c_1027_eps", "Dragon Ascent", 4, "500% Damage", sort="SORT_START"),
            card("c_1027_uni1", "Rising Dragon Spire", 3, "Game text<br>line two", [spire_v]),
            spire_v,
        ],
    )
    add_e7bot(
        conn,
        "czn/encounters/packs/list",
        [{"id": "p", "name": "The Blue Pot", "pack_type": "PACK_PLANET"}],
    )
    add_e7bot(
        conn,
        "czn/encounters/list",
        [
            {
                "id": stage,
                "name": "Mirror Hall",
                "link_encounter_set_id": "e1",
                "link_encounter_pack_id": "p",
            }
            for stage in ("e1a", "e1b")
        ],
    )
    add_e7bot(
        conn,
        "czn/encounters/e1a",
        {
            "options": [
                {
                    "id": "o0",
                    "simple_flavor_text": "Step inside",
                    "success_link_encounter_option_eff": {"next_encounter_id": "e1b"},
                }
            ]
        },
    )
    copy = {"reward_type": "CARD_COPY_HAVE_SELECT", "desc_option": "Copy <cc>1</> card"}
    add_e7bot(
        conn,
        "czn/encounters/e1b",
        {
            "options": [
                {
                    "id": "o1",
                    "simple_flavor_text": "Look into the mirror",
                    "select_link_nb_conditions": [],
                    "success_link_encounter_option_eff": {"reward0_reward": copy},
                },
                {"id": "o2", "simple_flavor_text": "Walk away"},
            ]
        },
    )
    add_e7bot(
        conn,
        "czn/rankings/list",
        [{"season_id": "s1", "score": 9, "t1_c1_id": 1027, "t1_c1_save_id": "77"}],
    )
    saved = {"res_id": "c_1027_uni1", "r_spark_category": "CATEGORY_UNIQUE"}
    conn.execute(
        "INSERT INTO e7bot.save (save_id, body, fetched_at) VALUES ('77', %s, now())",
        (
            Jsonb(
                {
                    "id": 77,
                    "card_datas": [saved | {"r_spark_res_id": "rsp5_c_1027_uni1"}],
                    "equip_datas": [{"res_id": "eq_1"}],
                }
            ),
        ),
    )


def test_game_facts_come_from_e7bot_with_prydwen_ratings_attached(call, conn, game_data):
    (rating,) = conn.execute(
        "SELECT rating FROM prydwen.card WHERE name = 'Rising Dragon Spire V'"
    ).fetchone()

    # Empty fields are left out, so the base card has no variant.
    versions = {c.get("variant", ""): c for c in call("get_card", name="Rising Dragon Spire")}

    assert versions[""]["cost"] == "3"
    assert versions[""]["text"] == "Game text\nline two"
    assert versions["V"]["text"] == "9 Hits"
    assert versions["V"]["rating"] == rating
    assert "source" not in versions["V"]
    assert versions["V"]["prydwen_url"] == CHARACTER_URL


def test_character_e7bot_lacks_is_served_from_prydwen(call):
    character = call("get_character", name="mei lin")
    assert character["url"] == CHARACTER_URL
    assert "source" not in character


def test_data_status_reports_both_sources(call, conn, game_data):
    conn.execute(
        "INSERT INTO e7bot.run (started_at, finished_at, ok, error)"
        " VALUES (now(), now(), false, 'czn/cards/list: HTTP 500')"
    )
    status = call("data_status")
    assert status["prydwen"]["pages"] == 1
    assert status["e7bot"]["error"] == "czn/cards/list: HTTP 500"
    assert "data_fetched_at" in status["e7bot"]
    assert status["stove"] == {"posts": 0}


def test_plan_deck_ranks_epiphanies_and_caps_removals(call, game_data):
    plan = call("plan_deck", character="mei lin")

    assert (plan["season"], plan["leaderboard_decks"]) == ("s1", 1)
    spire = next(c for c in plan["cards"] if c["card"] == "Rising Dragon Spire")
    assert spire["versions"][0]["variant"] == "V"
    assert spire["versions"][0]["leaderboard_share"] == 1
    assert [r["card"] for r in plan["removals"]] == ["Strike"]
    assert len(plan["removals"]) <= 5
    assert plan["equipment"][0] == {"item": "eq_1", "leaderboard_share": 1}


def test_plan_deck_for_unknown_character_is_null(call):
    assert call("plan_deck", character="zzzz") is None


def test_list_encounters_filters_by_chaos_and_names_copy_answers(call, game_data):
    listing = call("list_encounters", chaos="The Blu Pot", reward="copy")
    (event,) = listing["results"]
    assert (listing["total"], event["name"]) == (1, "Mirror Hall")
    assert event["answers"] == [{"stage": 2, "answer": "Look into the mirror", "gives": ["copy"]}]

    found = call("get_encounter", name="mirror hall")
    first, second = found["stages"]
    assert [a["text"] for a in first["answers"]] == ["Step inside"]
    assert first["answers"][0]["leads_to"] == second["stage"] == 2
    assert second["answers"][0]["rewards"][0]["text"] == "Copy 1 card"


def test_list_equipment_by_chaos_matches_listed_and_shortened_zone_names(call, conn, game_data):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/equipment"
    item = {"rarity": "Rare", "slot": "Weapon", "text": "Hit.", "stats": {}}
    places = {"Pot Blade": "Blue Pot", "Mist Blade": "City of Mist, The Blue Pot", "Shop": "Shop"}
    rows = [item | {"name": name, "source": place} for name, place in places.items()]
    parsed = ParsedPage("Equipment", "Equipment", {"equipment": rows})
    store.save_page(conn, url, "equipment", "", datetime.now(UTC), parsed, None)

    listing = call("list_equipment", chaos="blue pot")

    assert [row["name"] for row in listing["results"]] == ["Mist Blade", "Pot Blade"]


def test_unknown_chaos_is_an_error_naming_the_known_ones(call, game_data):
    with pytest.raises(Exception, match=r"Known: The Blue Pot$"):
        call("list_encounters", chaos="Nowhere At All")
