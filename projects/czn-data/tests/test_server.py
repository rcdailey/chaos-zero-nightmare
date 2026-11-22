"""Contract: MCP tools answer from the stored data with the fields their descriptions promise, as
plain text for Claude; REST routes answer programs in JSON.

Tools are called in-process through the MCP server, against real Postgres holding one scraped
Prydwen character page; tests that need game data add e7bot documents. Every call rebuilds the
merged tables first, as each scrape does. Assertions read the text the way Claude does: by the
lines and ``key: value`` fields that hold each fact.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient
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

    def call(tool: str, **arguments) -> str:
        build(conn)
        result = anyio.run(server.call_tool, tool, arguments)
        assert result.structured_content is None
        (content,) = result.content
        return content.text

    return call


def lines_with(text: str, *parts: str) -> list[str]:
    """The lines of text that contain every part."""
    return [line.strip() for line in text.splitlines() if all(p in line for p in parts)]


def top_field(text: str, key: str) -> str | None:
    """The value of a top-level field: on its ``key:`` line, or on the indented lines below."""
    lines = text.splitlines()
    for position, line in enumerate(lines):
        if line == f"{key}:":
            below = []
            for nested in lines[position + 1 :]:
                if not nested.startswith(" "):
                    break
                below.append(nested.strip())
            return "\n".join(below)
        if line.startswith(f"{key}: "):
            return line.removeprefix(f"{key}: ")
    return None


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
    assert {"name: Mei Lin", "gender: female"} <= set(summary.splitlines())
    (sections,) = lines_with(summary, "sections: ")
    for name in ("cards", "potentials", "teams", "decks", "equipment", "Pros", "Stats"):
        assert name in sections.removeprefix("sections: ").split(", ")

    teams = call("get_character", name="mei lin", section="teams")
    assert lines_with(teams, "Spire Fire") == [
        "- Spire Fire; tier: T1; members: Mei Lin, Veronica, Tiphera"
    ]
    assert "gender" not in teams  # a section reply repeats no profile

    stats = call("get_character", name="mei lin", section="stats")
    assert lines_with(stats, "Stats") == ["- Stats; text: CRIT Rate > CRIT DMG"]


def test_guide_decks_carry_deck_code_and_farming_zone(call, conn):
    entry = {"deck_code": "v6l8cOji", "farming_zone": "Celestial Bulwark (Season-5)"}
    push = json.dumps(json.dumps({"decks": [entry]}, separators=(",", ":")))
    html = CHARACTER_HTML.replace(
        "</body>", f"<script>self.__next_f.push([1,{push}])</script></body>"
    )
    parsed = parse_page(CHARACTER_URL, html)
    store.save_page(conn, CHARACTER_URL, "character", html, datetime.now(UTC), parsed, None)

    for reply in (
        call("get_character", name="mei lin", section="decks"),
        call("plan_deck", character="mei lin"),
    ):
        (deck,) = lines_with(reply, "Standard Dragon Spire")
        assert "code: v6l8cOji" in deck
        assert "farming_zone: Celestial Bulwark (Season-5)" in deck


def test_deck_notes_leave_out_the_disclaimer_every_deck_repeats(call, conn):
    disclaimer = (
        " Note: This is not an exhaustive list of teams this deck can be used in."
        " Teams Database Mei Lin Burn"
    )
    html = CHARACTER_HTML.replace("Buff Spire, then Ravage.", "Buff Spire." + disclaimer)
    parsed = parse_page(CHARACTER_URL, html)
    store.save_page(conn, CHARACTER_URL, "character", html, datetime.now(UTC), parsed, None)

    reply = call("get_character", name="mei lin", section="decks")

    assert "notes: Buff Spire." in reply
    assert "exhaustive" not in reply and "Teams Database" not in reply


def test_list_banners_shows_live_then_upcoming_and_hides_ended(call, conn):
    now = datetime.now(UTC)

    def save(url, name, start, end):
        html = banner_html(name, start.isoformat(), end.isoformat())
        store.save_page(conn, url, "banners", html, now, parse_page(BANNERS_URL, html), None)

    day = timedelta(days=1)
    save(BANNERS_URL, "Dahlia", now + day, now + 9 * day)
    save(f"{BANNERS_URL}?live", "Blazebell", now - day, now + 2 * day)
    save(f"{BANNERS_URL}?ended", "Tiphera", now - 9 * day, now - day)

    banners = lines_with(call("list_banners"), "kind: character")

    assert [line.split("; status: ")[1].split(";")[0] for line in banners] == ["live", "upcoming"]
    assert [line.split(";")[0] for line in banners] == ["- Blazebell", "- Dahlia"]


def test_search_guides_finds_pages_missing_some_query_words(call):
    reply = call("search_guides", query="Mei Lin Policy Office")
    assert [line.split("; data_fetched_at")[0] for line in lines_with(reply, "url: ")] == [
        f"- Mei Lin; url: {CHARACTER_URL}"
    ]


def guide_urls(reply: str) -> list[str]:
    return [line.split("url: ")[1].split(";")[0] for line in lines_with(reply, "url: ")]


def add_guide(conn, url: str, title: str, sections: dict[str, str]) -> None:
    """Store a scraped Prydwen guide page: the site menu, then title and sections."""
    parts = "".join(f"<h2>{heading}</h2><p>{text}</p>" for heading, text in sections.items())
    html = f"<main><nav><p>Home Guides Teams</p></nav><h1>{title}</h1>{parts}</main>"
    store.save_page(conn, url, "document", html, datetime.now(UTC), parse_page(url, html), None)


def test_search_guides_ranks_pages_with_more_query_words_first(call, conn):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/guides/team-building"
    add_guide(conn, url, "Team Building", {"Basics": "Team building. " * 50})

    pages = call("search_guides", query="Spire Fire team")

    assert guide_urls(pages) == [CHARACTER_URL, url]


def test_get_page_pages_long_text_by_character_offset(call, conn):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/guides/long"
    body = "a" * 15_000 + "b" * 5_000
    store.save_page(conn, url, "guide", body, datetime.now(UTC), ParsedPage("Long", body), None)

    first = call("get_page", url=url)
    rest = call("get_page", url=url, offset=15_000)

    assert (top_field(first, "length"), top_field(first, "body")) == ("20000", "a" * 15_000)
    assert "offset=15000" in top_field(first, "note")
    assert top_field(rest, "body") == "b" * 5_000
    assert top_field(rest, "note") is None


def test_search_guides_ranks_pages_with_every_word_by_closeness_not_repetition(call, conn):
    guides = "https://www.prydwen.gg/chaos-zero-nightmare/guides"
    pages = {
        f"{guides}/close": "Zorblax quuxite guide.",
        f"{guides}/repeated": "Zorblax intro. " + "Filler text. " * 100 + "Quuxite. " * 30,
    }
    for url, text in pages.items():
        add_guide(conn, url, "Guide", {"Notes": text})

    top = call("search_guides", query="zorblax quuxite", limit=1)

    assert guide_urls(top) == [f"{guides}/close"]


def test_search_guides_returns_the_matching_section_without_the_site_menu(call, conn):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/guides/partners"
    add_guide(
        conn,
        url,
        "Partners Guide",
        {"Overview": "Partners join a team.", "Best Partner for Zorblax": "Use Quuxite E4."},
    )

    reply = call("search_guides", query="best partner for zorblax", limit=1)

    (section,) = lines_with(reply, "offset: ")
    assert section.startswith("- Best Partner for Zorblax; text: Use Quuxite E4.;")
    (offset,) = [int(f.split(": ")[1]) for f in section.split("; ") if f.startswith("offset: ")]
    page = call("get_page", url=url, offset=offset, length=len("Use Quuxite E4."))
    assert top_field(page, "body") == "Use Quuxite E4."
    assert "Home Guides" not in call("get_page", url=url)


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

    restrictions = call("search_posts", query="season restrictions")
    (post,) = lines_with(restrictions, "- 11/11(Wed) Update Notice;")
    assert "/view/1" in post and "board: Update Notice" in restrictions
    assert "      - ■ Season Restrictions; text: Cards: Acid Gas" in restrictions.splitlines()

    typo = call("search_posts", query="Teatme Evnt")
    assert lines_with(typo, "- ")[0].startswith("- Teatime Event;")

    active = call("search_posts", active_on="2026-11-20")
    (post,) = lines_with(active, "- 11/11(Wed) Update Notice;")
    assert "every item: starts_on: 2026-11-11; ends_on: 2026-12-02" in active
    assert section_headings(active) == [
        "■ New Combatant [Blazebell] Rescue Rate-Up",
        (
            "■ New Combatant [Blazebell] Rescue Rate-Up > ◆ Rate-Up Period: 11/11/2026 after"
            " the maintenance – 12/02/2026 before the maintenance"
        ),
    ]


def section_headings(reply: str) -> list[str]:
    """The heading of each section in a search_posts reply, under its post."""
    return [
        line.removeprefix("      - ").split(";")[0]
        for line in reply.splitlines()
        if line.startswith("      - ")
    ]


def test_search_posts_pages_through_every_match(call, conn):
    sections = "".join(f"<p>◆ Event {n} Period: 11/01/2026 – 11/30/2026</p>" for n in range(3))
    add_post(conn, "1", "Events", sections)

    first = call("search_posts", active_on="2026-11-20", limit=2)
    rest = call("search_posts", active_on="2026-11-20", limit=2, offset=2)

    assert top_field(first, "total") == "3"
    assert "offset=2" in top_field(first, "note")
    assert top_field(rest, "note") is None
    assert sorted(section_headings(first) + section_headings(rest)) == [
        f"◆ Event {n} Period: 11/01/2026 – 11/30/2026" for n in range(3)
    ]


def test_search_posts_needs_query_or_valid_date(call):
    with pytest.raises(Exception, match="Give query"):
        call("search_posts")
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        call("search_posts", active_on="next week")


def test_list_characters_filters_by_gender(call, conn):
    assert call("list_characters", gender="female") == "No results."
    tag_female(conn)
    assert call("list_characters", gender="Female") == (
        "- Mei Lin; rarity: 5; element: Passion; class: Striker; gender: female"
    )
    assert call("list_characters", gender="male") == "No results."


def test_characters_route_gives_programs_names_and_card_art_as_json(
    conn, database_url, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", database_url)
    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    store.save_page(
        conn, CHARACTER_URL, "character", CHARACTER_HTML, datetime.now(UTC), parsed, None
    )
    build(conn)
    app = build_server(None, None).streamable_http_app()

    response = TestClient(app).get("/characters")

    assert response.json() == {"characters": [{"name": "Mei Lin", "image_url": MEI_LIN_PORTRAIT}]}


def test_synergies_combine_synergy_lists_and_teams(call):
    reply = call("synergies", character="mei lin")
    assert reply.splitlines() == [
        "every item: for: Mei Lin; teams: Spire Fire (T1)",
        "- Veronica; sources: own synergy list, team; reasons: Doubles as a draw unit.",
        "- Tiphera; sources: team",
    ]


def matches(item: str) -> list[tuple[str, float]]:
    """(card name, match) for each match line under one match_card_text item."""
    return [
        (line.removeprefix("- ").split(";")[0], float(line.split("match: ")[1]))
        for line in lines_with(item, "match: ")
    ]


def test_match_card_text_finds_the_variant_of_each_screen_text(call):
    texts = ["For 1 turn each Passion Card used 1 extra Hit", "no card says this zzz"]
    reply = call("match_card_text", texts=texts)
    spire, nothing = reply.split(f"- {texts[1]}\n")
    assert spire.startswith(f"- {texts[0]}\n")
    (best,) = lines_with(spire, "- Rising Dragon Spire V;")
    assert "variant: V" in best and "alias: Ascended" in best
    assert matches(spire)[0][0] == "Rising Dragon Spire V"
    assert all(score < matches(spire)[0][1] for _, score in matches(nothing))


def test_lookup_finds_a_version_by_the_name_the_game_shows(call):
    reply = call("lookup", name="Rising Dragon Spire (Ascended)", kind="card")
    assert lines_with(reply, "- ")[0].startswith("- Rising Dragon Spire V (Ascended);")


GAME8_URL = "https://game8.co/games/Chaos-Zero-Nightmare/archives/578215"
GAME8_HTML = """
<html><body><h1 class="p-archiveHeader__title">Distortion Adaptation Explained</h1>
<div class="archive-style-wrapper">
  <p>Distortion Adaptation adjusts characters to a Chaos's recommended level.</p>
  <div><h2 class="a-header--2">Distortion Adaptation Effects</h2></div>
  <div><h3 class="a-header--3">Keep The Save Data From Runs</h3></div>
  <p>You keep the Save Data you obtain while it is on.</p>
  <div><h2 class="a-header--2">How to Get</h2></div>
  <p>Available after Chapter 1 Episode 10.</p>
</div></body></html>
"""


def add_game8(conn, html: str = GAME8_HTML, url: str = GAME8_URL):
    conn.execute(
        "INSERT INTO game8.page (url, html, lastmod, fetched_at)"
        " VALUES (%s, %s, '2026-08-05T06:52:35Z', now())",
        (url, html),
    )


def game8_sections(call, query: str) -> list[dict]:
    """Each section search_game_systems found: its fields, with its page's fields."""
    reply = call("search_game_systems", query=query, limit=20)
    pages_shared, page, sections_shared, sections = {}, {}, {}, []
    for line in reply.splitlines():
        if line.startswith("  every item: "):
            pages_shared = fields(line.removeprefix("  every item: "))
        elif line.startswith("  - "):
            title, _, rest = line.removeprefix("  - ").partition("; ")
            page, sections_shared = pages_shared | {"page_title": title} | fields(rest), {}
        elif line.startswith("      every item: "):
            sections_shared = fields(line.removeprefix("      every item: "))
        elif line.startswith("      - "):
            # The heading comes first and unlabeled, or on the every item line when shared.
            item = line.removeprefix("      - ")
            heading, _, rest = item.partition("; ")
            named = {"heading": heading} | fields(rest) if ": " not in heading else fields(item)
            sections.append(page | sections_shared | named)
        elif line.startswith("          ") and sections:  # lines of a long section text
            text = sections[-1].get("text")
            sections[-1]["text"] = f"{text}\n{line.strip()}" if text else line.strip()
        elif line.startswith("      "):
            page["page_summary"] = line.strip()  # a long page summary, below its page
    return sections


def fields(line: str) -> dict:
    """The ``key: value`` fields of one line, joined by ``; ``."""
    return dict(part.split(": ", 1) for part in line.split("; ") if ": " in part)


def test_game8_sections_carry_parent_headings_and_table_rows_without_navigation(call, conn):
    add_game8(
        conn,
        """
        <html><body><h1 class="p-archiveHeader__title">Full-Scale Offensive Guide</h1>
        <div class="archive-style-wrapper">
          <p>A three-team boss mode.</p>
          <div class="a-outline">List of Contents Bosses Best Teams</div>
          <div><h2 class="a-header--2">Bosses</h2></div>
          <div><h3 class="a-header--3">Aavarck</h3></div>
          <table><tr><th>Effect</th><th>Value</th></tr>
            <tr><td>DoT Amount</td><td>+50%</td></tr></table>
          <div><h3 class="a-header--3">Woman Within the Fog</h3></div>
          <p>TBD</p>
          <div><h2 class="a-header--2">Best Teams</h2></div>
          <div><h3 class="a-header--3">Aavarck Best Teams</h3></div>
          <div><h4 class="a-header--4">Arabella Fracture Team</h4></div>
          <p>Arabella, Mika, Khalipe.</p>
          <div><h2 class="a-header--2">Chaos Zero Nightmare Related Guides</h2></div>
          <div><h3 class="a-header--3">Current Events</h3></div>
          <p>Link list</p>
        </div></body></html>
        """,
    )

    sections = game8_sections(call, "full-scale offensive aavarck")

    assert sorted((s["heading"], s["text"]) for s in sections) == [
        ("Best Teams > Aavarck Best Teams > Arabella Fracture Team", "Arabella, Mika, Khalipe."),
        ("Bosses > Aavarck", "Effect | Value / DoT Amount | +50%"),
    ]
    assert {s["page_summary"] for s in sections} == {"A three-team boss mode."}
    body = top_field(call("get_page", url=GAME8_URL), "body")
    assert "List of Contents" not in body and "Link list" not in body and "TBD" not in body


def test_long_game8_section_is_served_as_overlapping_pieces_located_in_the_page(call, conn):
    lines = "".join(f"<p>Line {n} {'x' * 90}</p>" for n in range(60))
    add_game8(
        conn,
        '<html><body><div class="archive-style-wrapper">'
        f'<h2 class="a-header--2">Long Rules</h2>{lines}</div></body></html>',
    )

    pieces = game8_sections(call, "long rules")
    for piece in pieces:
        piece["offset"], piece["length"] = int(piece["offset"]), int(piece["length"])
    pieces.sort(key=lambda s: s["offset"])

    assert len(pieces) > 2
    assert all(s["heading"] == "Long Rules" and s["length"] <= 2000 for s in pieces)
    assert pieces[1]["offset"] < pieces[0]["offset"] + pieces[0]["length"]  # pieces overlap
    for piece in pieces:
        page = call("get_page", url=GAME8_URL, offset=piece["offset"], length=piece["length"])
        assert top_field(page, "body") == piece["text"]


def test_search_game_systems_returns_matching_sections_with_their_page(call, conn):
    add_game8(conn)

    best = game8_sections(call, "does distortion adaptation keep save data")[0]

    assert best["heading"] == "Distortion Adaptation Effects > Keep The Save Data From Runs"
    assert best["text"] == "You keep the Save Data you obtain while it is on."
    assert best["page_title"] == "Distortion Adaptation Explained"
    assert best["page_summary"].startswith("Distortion Adaptation adjusts")
    assert (best["url"], best["updated_at"]) == (GAME8_URL, "2026-08-05 06:52 UTC")


def test_get_page_reads_the_text_around_a_game8_section(call, conn):
    add_game8(conn)
    section = game8_sections(call, "Chapter 1 Episode 10")[0]
    offset, length = int(section["offset"]), int(section["length"])

    page = call("get_page", url=GAME8_URL, offset=offset, length=length)

    assert top_field(page, "body") == "Available after Chapter 1 Episode 10."
    before = call("get_page", url=GAME8_URL, offset=offset - 11, length=11)
    assert top_field(before, "body") == "How to Get"


def test_find_teams_by_member(call):
    reply = call("find_teams", character="veronika")
    assert reply.splitlines()[0] == "prydwen_teams:"
    (team,) = lines_with(reply, "- ")
    assert team.startswith("- Spire Fire;") and "listed_for: Mei Lin;" in team


EFFECTS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/guides/effects-dictionary"
PARTNERS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/partners"


@pytest.fixture
def effects(conn):
    rows = [
        {"name": "Exhaust", "type": "Tag", "description": "When used, move to Exhaust pile."},
        {"name": "Equilux", "type": "Unique effect", "description": "Polarizes the card."},
        # Spelled like "Polarization" but about something else.
        {"name": "Ionization", "type": "Debuff", "description": "+8% Damage taken."},
    ]
    page = ParsedPage("Effects", "", {"effect": rows})
    store.save_page(conn, EFFECTS_URL, "effects", "", datetime.now(UTC), page, None)


def test_lookup_accepts_keyword_as_effect_and_rejects_unknown_kinds(call, effects):
    (found,) = lines_with(call("lookup", name="exhaust", kind="keyword"), "- ")
    assert found.startswith("- Exhaust; kind: effect;")
    with pytest.raises(Exception, match="kind"):
        call("lookup", name="exhaust", kind="spell")


def test_lookup_finds_terms_defined_inside_other_effects(call, effects):
    found = lines_with(call("lookup", name="Polarization", kind="effect"), "- ")
    assert found[0].startswith("- Equilux;")


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
    assert (top_field(found, "ego_name"), top_field(found, "ego_cost")) == (
        "Frail Dominion of Kyrken",
        "3",
    )


def test_list_cards_filters_by_color(call):
    listing = call("list_cards", character="meilin", color="white")
    (white,) = lines_with(listing, "- ")
    assert top_field(listing, "total") == "1"
    assert white.startswith("- Strike;") and "color: White" in white


def test_recommended_by_lists_every_place_an_item_is_recommended(call, conn):
    fragments = ParsedPage(
        "Memory Fragments",
        "Executioner's Tool",
        {"fragment_set": [{"name": "Executioner's Tool", "bonuses": []}]},
    )
    store.save_page(conn, FRAGMENTS_URL, "memory_fragments", "", datetime.now(UTC), fragments, None)

    (row,) = lines_with(call("recommended_by", name="executioners tool"), "- ")
    assert row.startswith("- Executioner's Tool; character: Mei Lin;")
    places = row.split("places: ")[1].split(";")[0].split(", ")
    assert sorted(places) == ["deck: Standard Dragon Spire", "fragment ranking: Default #1 100.00%"]


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

    reply = call("get_card", name="Rising Dragon Spire")

    assert f"prydwen_url: {CHARACTER_URL}" in reply
    (base,) = lines_with(reply, "- Rising Dragon Spire;")
    (best,) = lines_with(reply, "- Rising Dragon Spire V;")
    assert "cost: 3" in base and "text: Game text / line two" in base
    assert "variant" not in base  # empty fields are left out
    assert "text: 9 Hits" in best and f"rating: {rating}" in best
    for internal in ("source", "game_id", "character_slug", "image_url"):
        assert f"{internal}:" not in reply


def test_character_e7bot_lacks_is_served_from_prydwen(call):
    character = call("get_character", name="mei lin")
    assert top_field(character, "url") == CHARACTER_URL
    assert "source:" not in character and "slug:" not in character


def test_data_status_reports_both_sources(call, conn, game_data):
    conn.execute(
        "INSERT INTO e7bot.run (started_at, finished_at, ok, error)"
        " VALUES (now(), now(), false, 'czn/cards/list: HTTP 500')"
    )
    status = call("data_status")
    assert top_field(status, "prydwen").startswith("pages: 1\n")
    e7bot = top_field(status, "e7bot").splitlines()
    assert "error: czn/cards/list: HTTP 500" in e7bot
    assert any(line.startswith("data_fetched_at: ") for line in e7bot)
    assert top_field(status, "stove") == "posts: 0"


def test_plan_deck_ranks_epiphanies_and_caps_removals(call, game_data):
    plan = call("plan_deck", character="mei lin")

    assert (top_field(plan, "season"), top_field(plan, "leaderboard_decks")) == ("s1", "1")
    cards = top_field(plan, "cards").splitlines()
    spire = cards.index(next(line for line in cards if line.startswith("- Rising Dragon Spire;")))
    assert cards[spire + 1 : spire + 3] == [
        "versions:",
        "- V; rating: 5; leaderboard_share: 1.0",
    ]
    removals = lines_with(top_field(plan, "removals"), "- ")
    assert [line.split(";")[0] for line in removals] == ["- Strike"]
    assert lines_with(top_field(plan, "equipment"), "- ")[0] == "- eq_1; leaderboard_share: 1.0"


def test_game_data_urls_open_the_item_where_e7bot_has_a_page_for_it(call, conn, game_data):
    partner = {"rarity": 5, "job": "striker", "partner_card": {}, "partner_passives": []}
    add_e7bot(
        conn,
        "czn/characters/partner/list/detail",
        # The site's partner page opens only partners in its characters list.
        [partner | {"id": "1027", "name": "Listed"}, partner | {"id": "20023", "name": "Unlisted"}],
    )
    site = "https://wiki.e7bot.top/#/czn"

    def url(tool: str, name: str) -> str | None:
        return top_field(call(tool, name=name), "url")

    assert url("get_character", "mei lin") == f"{site}/combatant/1027"
    assert url("get_encounter", "mirror hall") == f"{site}/encounters/e1a"
    assert url("get_partner", "Listed") == f"{site}/partner/1027"
    assert url("get_partner", "Unlisted") == f"{site}/partner"


def test_list_divine_epiphanies_pages_through_every_match(call, conn):
    sparks = [
        {"id": f"s{n:02}", "description": f"Effect {n:02}", "god_type": "vitor", "rarity": "R"}
        for n in range(35)
    ]
    add_e7bot(conn, "czn/r-sparks/list", sparks)

    first = call("list_divine_epiphanies")
    rest = call("list_divine_epiphanies", offset=30)
    past = call("list_divine_epiphanies", offset=500)

    assert (top_field(first, "total"), "offset=30" in top_field(first, "note")) == ("35", True)
    assert top_field(rest, "note") is None
    texts = lines_with(first, "- Effect") + lines_with(rest, "- Effect")
    assert sorted(texts) == [f"- Effect {n:02}" for n in range(35)]
    # Fields every row shares, such as the source, are stated once.
    assert len(lines_with(first, "url: ")) == 1
    assert (top_field(past, "total"), top_field(past, "results")) == ("35", "none")


def test_get_character_unknown_section_lists_the_real_ones(call):
    reply = call("get_character", name="mei lin", section="ratings")
    assert top_field(reply, "sections") is None
    assert "No section 'ratings'" in top_field(reply, "note")
    assert "Pros" in top_field(reply, "note")


def test_plan_deck_for_unknown_character_says_no_match(call):
    assert call("plan_deck", character="zzzz") == "No match."


def test_list_encounters_filters_by_chaos_and_names_copy_answers(call, game_data):
    listing = call("list_encounters", chaos="The Blu Pot", reward="copy")
    assert top_field(listing, "total") == "1"
    assert lines_with(listing, "- ")[0].startswith("- Mirror Hall;")
    assert lines_with(listing, "Look into the mirror") == [
        "- Look into the mirror; stage: 2; gives: copy"
    ]

    found = call("get_encounter", name="mirror hall")
    stages = top_field(found, "stages")
    first, second = stages.split("- stage: ")[1:]
    assert first.startswith("1\n") and lines_with(first, "- ") == ["- Step inside; leads_to: 2"]
    assert second.startswith("2\n") and "Copy 1 card" in second


def test_list_equipment_by_chaos_matches_listed_and_shortened_zone_names(call, conn, game_data):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/equipment"
    item = {"rarity": "Rare", "slot": "Weapon", "text": "Hit.", "stats": {}}
    places = {"Pot Blade": "Blue Pot", "Mist Blade": "City of Mist, The Blue Pot", "Shop": "Shop"}
    rows = [item | {"name": name, "source": place} for name, place in places.items()]
    parsed = ParsedPage("Equipment", "Equipment", {"equipment": rows})
    store.save_page(conn, url, "equipment", "", datetime.now(UTC), parsed, None)

    listing = call("list_equipment", chaos="blue pot")

    assert [line.split(";")[0] for line in lines_with(listing, "- ")] == [
        "- Mist Blade",
        "- Pot Blade",
    ]


def test_list_equipment_leaves_out_tutorial_copies(call, conn):
    def relic(relic_id: str, text: str) -> dict:
        return {
            "id": relic_id,
            "name": "Crimson Sword",
            "rarity": "RARITY_LEGENDARY",
            "relic_category": "EQUIPMENT",
            "relic_type": "EQUIP_WEAPON",
            "skills": [{"description": text}],
        }

    add_e7bot(
        conn,
        "czn/relics/list",
        [relic("eq_pub_008", "Hit hard."), relic("eq_tut_002", "Hit hard sometimes.")],
    )

    listing = call("list_equipment", name="Crimson Sword")

    (sword,) = lines_with(listing, "Crimson Sword")
    assert "text: Hit hard." in sword


def test_unknown_chaos_is_an_error_naming_the_known_ones(call, game_data):
    with pytest.raises(Exception, match=r"Known: The Blue Pot$"):
        call("list_encounters", chaos="Nowhere At All")
