"""Contract: MCP tools answer from the stored data with the fields their descriptions promise, as
plain text for Claude; REST routes answer programs in JSON.

Tools are called in-process through the MCP server, against real Postgres holding one scraped
Prydwen character page; tests that need game data add e7bot documents. Every call rebuilds the
merged tables first, as each scrape does. Assertions read the text the way Claude does: by the
lines and ``key: value`` fields that hold each fact.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient
from test_parse import banner_html

from czn_data import store
from czn_data.build import BuildError, build
from czn_data.parse import ParsedPage, page_kind, parse_page
from czn_data.server import build_server

CHARACTER_URL = "https://www.prydwen.gg/chaos-zero-nightmare/characters/mei-lin"
FRAGMENTS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/memory-fragments"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()
BANNERS_URL = "https://www.prydwen.gg/chaos-zero-nightmare/banners"
COPY_REWARD = {"id": "r1", "reward_type": "CARD_COPY_HAVE_SELECT", "desc_option": "Copy 1 card"}
# One version of an event as e7bot sends it: its first choice opens a follow-up step nested in
# the result, whose choices end the event or loop back to the first step.
MIRROR_HALL = {
    "id": "e1a",
    "option_count": 1,
    "options": [
        {
            "id": "o0",
            "simple_flavor_text": "Step inside",
            "mystery_flag": "Z",
            "success_link_encounter_option_eff": {
                "overview_eff_desc": "Event Encounter: [The Mirror]",
                "next_encounter_id": "e1a-1",
                "next_encounter": {
                    "id": "e1a-1",
                    "name": "Mirror Hall",
                    "options": [
                        {
                            "id": "o1",
                            "simple_flavor_text": "Look into the mirror",
                            "dice_value": 6,
                            "link_dice_technique_id": "foresight",
                            "success_link_encounter_option_eff": {"reward0_reward": COPY_REWARD},
                            "fail_link_encounter_option_eff": {
                                "overview_eff_desc": "End the event"
                            },
                        },
                        {
                            "id": "o2",
                            "simple_flavor_text": "Turn back",
                            "appear_link_encounter_trigger_id": ["trg_tried"],
                            "success_link_encounter_option_eff": {
                                "next_encounter_id": "e1a",
                                "is_circular_reference": True,
                                "next_encounter": {"id": "e1a", "options": None},
                            },
                        },
                    ],
                },
            },
        }
    ],
}
# The other version: its screen shows one fixed choice and one of two random ones, and the
# game's option_count leaves out the battle choice, which the screen always adds.
MIRROR_HALL_B = {
    "id": "e1b",
    "option_count": 2,
    "options": [
        {"id": "o3", "simple_flavor_text": "Leave quietly"},
        {"id": "o4", "simple_flavor_text": "Pray", "select_type": "RANDOM"},
        {
            "id": "o5",
            "simple_flavor_text": "Sing",
            "select_type": "RANDOM",
            "success_link_encounter_option_eff": {
                "reward0_reward": {
                    "id": "r2",
                    "reward_type": "CARD_ID",
                    "amount_max": 1,
                    "desc_option": "Obtain Cards[Hymn] <cc>1</>Cards and <cc>1</> time(s)"
                    " to 1 random card(s)",
                }
            },
        },
        {"id": "o6", "option_type": "ATTACK", "link_battle_id": "b1"},
    ],
}


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


def test_character_guide_gives_the_profile_by_default_and_several_parts_in_one_call(call, conn):
    tag_female(conn)
    profile = call("character_guide", name="mei lin")
    assert "gender: female" in lines_with(profile, "gender: ")
    (parts,) = lines_with(profile, "parts: ")
    for part in ("plan", "teams", "synergies", "cards", "potentials", "equipment", "Pros", "Stats"):
        assert part in parts.removeprefix("parts: ").split(", ")

    reply = call("character_guide", name="mei lin", parts=["teams", "stats", "nonsense"])
    (team,) = lines_with(reply, "- Spire Fire")
    assert team.startswith("- Spire Fire; guide_tier: T1; members: Mei Lin, Veronica, Tiphera")
    assert lines_with(reply, "- Stats") == ["- Stats; text: CRIT Rate > CRIT DMG"]
    assert "gender" not in reply  # only the parts asked for
    assert "No part 'nonsense'. Parts: profile, plan" in top_field(reply, "note")


def test_guide_decks_carry_deck_code_and_farming_zone(call, conn):
    entry = {"deck_code": "v6l8cOji", "farming_zone": "Celestial Bulwark (Season-5)"}
    push = json.dumps(json.dumps({"decks": [entry]}, separators=(",", ":")))
    html = CHARACTER_HTML.replace(
        "</body>", f"<script>self.__next_f.push([1,{push}])</script></body>"
    )
    parsed = parse_page(CHARACTER_URL, html)
    store.save_page(conn, CHARACTER_URL, "character", html, datetime.now(UTC), parsed, None)

    reply = call("character_guide", name="mei lin", parts=["plan"])
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

    reply = call("character_guide", name="mei lin", parts=["plan"])

    assert "notes: Buff Spire." in reply
    assert "exhaustive" not in reply and "Teams Database" not in reply


def test_listing_banners_shows_live_then_upcoming_and_hides_ended(call, conn):
    now = datetime.now(UTC)

    def save(url, name, start, end):
        html = banner_html(name, start.isoformat(), end.isoformat())
        store.save_page(conn, url, "banners", html, now, parse_page(BANNERS_URL, html), None)

    day = timedelta(days=1)
    save(BANNERS_URL, "Dahlia", now + day, now + 9 * day)
    save(f"{BANNERS_URL}?live", "Blazebell", now - day, now + 2 * day)
    save(f"{BANNERS_URL}?ended", "Tiphera", now - 9 * day, now - day)

    banners = lines_with(call("list", kind="banners"), "kind: character")

    assert [line.split("; status: ")[1].split(";")[0] for line in banners] == ["live", "upcoming"]
    assert [line.split(";")[0] for line in banners] == ["- Blazebell", "- Dahlia"]


def test_search_finds_guide_pages_missing_some_query_words(call):
    reply = call("search", query="Mei Lin Policy Office", kinds=["guides"])
    assert lines_with(reply, "kind: guide")[0].startswith("- Mei Lin; kind: guide")


def add_guide(conn, url: str, title: str, sections: dict[str, str]) -> None:
    """Store a scraped Prydwen guide page: the site menu, then title and sections."""
    parts = "".join(f"<h2>{heading}</h2><p>{text}</p>" for heading, text in sections.items())
    html = f"<main><nav><p>Home Guides Teams</p></nav><h1>{title}</h1>{parts}</main>"
    store.save_page(conn, url, "document", html, datetime.now(UTC), parse_page(url, html), None)


def test_search_returns_the_matching_guide_section_without_the_site_menu(call, conn):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/guides/partners"
    add_guide(
        conn,
        url,
        "Partners Guide",
        {"Overview": "Partners join a team.", "Best Partner for Zorblax": "Use Quuxite E4."},
    )

    reply = call("search", query="best partner for zorblax", kinds=["guides"])

    shown = reply.split("\nmore:")[0]
    assert lines_with(shown, "Best Partner") == [
        "- Best Partner for Zorblax; text: Use Quuxite E4."
    ]
    assert "Home Guides" not in reply


def test_search_lists_later_matches_in_one_line_and_reads_them_by_ref(call, conn):
    add_guide(
        conn,
        "https://www.prydwen.gg/chaos-zero-nightmare/guides/partners",
        "Partners Guide",
        {"Best Partner for Zorblax": "Use Quuxite E4.", "Zorblax Partner Tips": "Pair E2."},
    )

    reply = call("search", query="zorblax partner", kinds=["guides"])
    refs = more_refs(reply)
    read = call("search", read=[*refs, "nonesuch"])

    # Each kind's best match comes in full; its later matches come as one line, without text.
    assert len(full_sections(reply)) == 1 and refs
    assert all("text:" not in line for line in reply.split("\nmore:")[1].splitlines())
    shown = full_sections(reply)[0]["text"]
    (other,) = {"Use Quuxite E4.", "Pair E2."} - {shown}
    assert other in [s["text"] for s in full_sections(read)]
    assert len(full_sections(read)) == len(refs)
    assert "No passage nonesuch" in top_field(read, "note")


def add_post(conn, article_id: str, title: str, content: str):
    conn.execute(
        "INSERT INTO stove.article VALUES (%s, 142421, %s, %s, '2026-11-11', now(), now())",
        (article_id, title, content),
    )


def test_search_finds_announcements_by_words_typo_title_and_active_date(call, conn):
    add_post(
        conn,
        "1",
        "11/11(Wed) Update Notice",
        "<p>■ New Combatant [Blazebell] Rescue Rate-Up</p>"
        "<p>◆ Rate-Up Period: 11/11/2026 after the maintenance – 12/02/2026 before the"
        " maintenance</p><p>■ Season Restrictions</p><p>Cards: Acid Gas</p>",
    )
    add_post(conn, "2", "Teatime Event", "<p>◆ Event Period: 11/01/2026 – 11/05/2026</p>")

    restrictions = call("search", query="season restrictions", kinds=["announcements"])
    assert len(lines_with(restrictions, "- 11/11(Wed) Update Notice;")) == 1
    assert "kind: announcement" in restrictions and "board: Update Notice" in restrictions
    assert "      - ■ Season Restrictions; text: Cards: Acid Gas" in restrictions.splitlines()

    typo = call("search", query="Teatme Evnt", kinds=["announcements"])
    assert lines_with(typo, "- ")[0].startswith("- Teatime Event")

    active = call("search", active_on="2026-11-20")
    assert "every item: starts_on: 2026-11-11; ends_on: 2026-12-02" in active
    assert active_matches(active) == [
        "11/11(Wed) Update Notice > ■ New Combatant [Blazebell] Rescue Rate-Up",
        (
            "11/11(Wed) Update Notice > ■ New Combatant [Blazebell] Rescue Rate-Up > ◆ Rate-Up"
            " Period: 11/11/2026 after the maintenance – 12/02/2026 before the maintenance"
        ),
    ]


def active_matches(reply: str) -> list[str]:
    """The title > heading of each one-line match in a search reply listing active sections."""
    return [line.split("; match: ")[1].split(";")[0] for line in lines_with(reply, "; match: ")]


def section_headings(reply: str) -> list[str]:
    """The heading of each section in a search reply, under its page."""
    return [
        line.removeprefix("      - ").split(";")[0]
        for line in reply.splitlines()
        if line.startswith("      - ")
    ]


def test_search_lists_every_active_announcement_in_one_line_to_read_by_ref(call, conn):
    sections = "".join(f"<p>◆ Event {n} Period: 11/01/2026 – 11/30/2026</p>" for n in range(30))
    add_post(conn, "1", "Events", sections)

    active = call("search", active_on="2026-11-20")
    refs = [line.removeprefix("- ").split(";")[0] for line in lines_with(active, "; match: ")]
    read = call("search", read=refs[:2])

    assert len(active_matches(active)) == 30 and "text:" not in active
    assert section_headings(read) == [
        f"◆ Event {n} Period: 11/01/2026 – 11/30/2026" for n in range(2)
    ]
    assert all(f"ref: {ref}" in read for ref in refs[:2])  # Claude can tell which text is which


def test_search_with_query_and_date_keeps_every_active_announcement(call, conn):
    stale = "".join(
        f"<p>◆ Zorblax Zorblax Contest {n} Period: 10/01/2026 – 10/05/2026</p>" for n in range(40)
    )
    add_post(conn, "1", "Old Events", stale)
    add_post(conn, "2", "Market", "<p>◆ Zorblax Market Period: 11/01/2026 – 11/30/2026</p>")

    reply = call("search", query="zorblax", active_on="2026-11-20")

    assert section_headings(reply) == ["◆ Zorblax Market Period: 11/01/2026 – 11/30/2026"]


def test_search_needs_query_or_valid_date(call):
    with pytest.raises(Exception, match="Give query"):
        call("search")
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        call("search", active_on="next week")


def test_listing_characters_filters_by_gender(call, conn):
    def female_names(gender: str) -> list[str]:
        return lines_with(call("list", kind="characters", gender=gender), "- ")

    assert female_names("female") == []
    tag_female(conn)
    assert female_names("Female") == [
        "- Mei Lin; rarity: 5; element: Passion; class: Striker; gender: female"
    ]
    assert female_names("male") == []


def test_list_refuses_a_filter_its_kind_does_not_take(call):
    with pytest.raises(Exception, match="cards takes no slot; its filters: base_only"):
        call("list", kind="cards", slot="Weapon")


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
    reply = call("character_guide", name="mei lin", parts=["synergies"])
    assert reply.splitlines()[1:] == [
        "synergies:",
        "  every item: for: Mei Lin; teams: Spire Fire (T1)",
        "  - Veronica; sources: own synergy list, team; reasons: Doubles as a draw unit.",
        "  - Tiphera; sources: team",
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
    reply = call("lookup", names=["Rising Dragon Spire (Ascended)"], kind="card")
    assert best_names(reply)[0] == "Rising Dragon Spire V (Ascended)"


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


def more_refs(reply: str) -> list[str]:
    """The refs of a search reply's one-line matches, in order."""
    lines = reply.splitlines()
    if "more:" not in lines:
        return []
    listed = lines[lines.index("more:") + 1 :]
    return [line.removeprefix("  - ").split(";")[0] for line in listed if line.startswith("  - ")]


def game8_sections(call, query: str) -> list[dict]:
    """Each guide section search found, best first: the one shown in full, then those it
    listed in one line, read in full by their refs. Each has its fields and its page's."""
    reply = call("search", query=query, kinds=["guides"])
    sections = full_sections(reply)
    if refs := more_refs(reply):
        sections += full_sections(call("search", read=refs))
    return sections


def full_sections(reply: str) -> list[dict]:
    """The sections a search reply gives in full: their fields, with their page's fields."""
    pages_shared, page, sections_shared, sections = {}, {}, {}, []
    for line in reply.splitlines():
        if line == "more:":
            break
        if line.startswith("  every item: "):
            pages_shared = fields(line.removeprefix("  every item: "))
        elif line.startswith("  - "):
            title, _, rest = line.removeprefix("  - ").partition("; ")
            page, sections_shared = pages_shared | {"title": title} | fields(rest), {}
        elif line.startswith("      every item: "):
            sections_shared = fields(line.removeprefix("      every item: "))
        elif line == "      -":  # a section whose long text follows on its own lines
            sections.append(page | sections_shared | {"text": ""})
        elif line.startswith("      - "):
            # The heading comes first and unlabeled; when shared, it is on the every item line
            # and the text leads instead.
            item = line.removeprefix("      - ")
            lead, _, rest = item.partition("; ")
            key = "text" if "heading" in sections_shared else "heading"
            named = {key: lead} | fields(rest) if ": " not in lead else fields(item)
            sections.append(page | sections_shared | named)
        elif line.startswith("          ") and sections:  # lines of a long section text
            text = sections[-1].get("text")
            sections[-1]["text"] = f"{text}\n{line.strip()}" if text else line.strip()
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

    found = [s for s in sections if s["title"] == "Full-Scale Offensive Guide"]
    assert sorted((s["heading"], s["text"]) for s in found) == [
        ("Best Teams > Aavarck Best Teams > Arabella Fracture Team", "Arabella, Mika, Khalipe."),
        ("Bosses > Aavarck", "Effect | Value / DoT Amount | +50%"),
    ]


def test_long_game8_section_is_served_as_overlapping_pieces(call, conn):
    lines = "".join(f"<p>Line {n} {'x' * 90}</p>" for n in range(60))
    add_game8(
        conn,
        '<html><body><div class="archive-style-wrapper">'
        f'<h2 class="a-header--2">Long Rules</h2>{lines}</div></body></html>',
    )

    pieces = [s for s in game8_sections(call, "long rules") if s.get("heading") == "Long Rules"]

    assert len(pieces) > 2
    assert all(s["heading"] == "Long Rules" and len(s["text"]) <= 2000 for s in pieces)
    served = [f"Line {n} " for n in range(60) if any(f"Line {n} " in s["text"] for s in pieces)]
    assert len(served) == 60
    assert sum(text.count("Line ") for text in (s["text"] for s in pieces)) > 60  # overlap


def test_search_returns_game8_sections_with_their_page_title_and_date(call, conn):
    add_game8(conn)

    best = game8_sections(call, "does distortion adaptation keep save data")[0]

    assert best["heading"] == "Distortion Adaptation Effects > Keep The Save Data From Runs"
    assert best["text"] == "You keep the Save Data you obtain while it is on."
    assert best["title"] == "Distortion Adaptation Explained"
    assert (best["kind"], best["updated_at"]) == ("guide", "2026-08-05 06:52 UTC")


def add_game_files(conn, tables: dict[str, list[dict]]):
    for name, rows in tables.items():
        conn.execute(
            "INSERT INTO gamefiles.file (name, build, record, rows, fetched_at)"
            " VALUES (%s, 'b', '', %s, now())",
            (name, Jsonb(rows)),
        )


def test_search_serves_game_help_topics_first_as_plain_text(call, conn):
    from czn_data.gamefiles import HELP_MENUS, HELP_PAGES, TEXT

    def page(id_, group, sort, title, desc):
        return {"id": id_, "group": group, "sort": sort, "title": title, "out_game_desc": desc}

    add_game_files(
        conn,
        {
            TEXT: [
                {"id": "t_menu", "text": "Save Data Storage Limit"},
                {"id": "t_p1", "text": "Storage Limit"},
                {"id": "d_p1", "text": "The <color_orange>minimum</> limit is 100 points."},
                {"id": "t_p2", "text": "Nightmare Mode"},
                {"id": "d_p2", "text": "Nightmare Mode adds 10 points.<br>Always."},
                {"id": "t_p3", "text": "Quuxite"},
                {"id": "d_p3", "text": "Zorblax wibble."},
                {"id": "t_tut", "text": "Card Synergy"},
                {"id": "d_tut", "text": "Card order matters."},
            ],
            HELP_MENUS[1]: [
                {"id": "m1", "name": "t_menu", "link_tutorial_popup_page_group": "limit"}
            ],
            HELP_PAGES[1]: [
                page("p2", "limit", "2", "t_p2", "d_p2"),
                page("p1", "limit", "1", "t_p1", "d_p1"),
                page("p3", "limit", "3", "t_p3", "d_p3"),
                page("tut", "tuto_only", "1", "t_tut", "d_tut"),
            ],
        },
    )

    add_game8(conn)

    reply = call("search", query="save data storage limit nightmare")

    pages = lines_with(reply, "; kind: ")
    assert pages[0].startswith(
        "- Save Data Storage Limit; kind: game rules (Help, Chaos Help menu)"
    )
    # The whole topic, one section per page in order under its title, the best page marked.
    lines = reply.splitlines()
    start = lines.index("    sections:") + 1
    assert lines[start : start + 3] == [
        "      - Storage Limit; text: The minimum limit is 100 points.",
        "      - Nightmare Mode; text: Nightmare Mode adds 10 points. / Always.; best_match: yes",
        "      - Quuxite; text: Zorblax wibble.",
    ]
    assert any("kind: guide" in page for page in pages)  # guides add detail
    tutorial = call("search", query="card synergy", kinds=["rules"]).splitlines()
    assert tutorial[1].startswith("  - Card Synergy; kind: game rules (Help, Tutorial menu)")
    assert tutorial[2:4] == ["    sections:", "      - Card order matters."]


def test_search_serves_tutorial_event_and_message_text(call, conn):
    from czn_data.gamefiles import TEXT

    add_game_files(
        conn,
        {
            TEXT: [
                {"id": "tutorial_script@tsp_1_0053_2", "text": "Only Seasonal Zorblax works."},
                {"id": "tutorial_script@tsp_1_0053_1", "text": "Welcome to Nebula."},
                {"id": "event@title@event_9", "text": "Quuxite Festival"},
                {"id": "event@info_desc@event_9", "text": "Log in daily for <cc>Quuxite</>."},
                {
                    "id": "ui_preset_reset_desc",
                    "text": "The Combatants in the Wibble preset will be reset now.",
                },
                {"id": "ui_preset_ok", "text": "Wibble OK"},
            ]
        },
    )

    tutorial = call("search", query="seasonal zorblax", kinds=["rules"])
    festival = call("search", query="quuxite festival", kinds=["rules"])
    preset = call("search", query="wibble preset", kinds=["rules"])

    def first_page(reply: str) -> str:
        return lines_with(reply, "kind: ")[0]

    assert first_page(tutorial).startswith("- Tutorial; kind: game rules (tutorial dialogue)")
    # Headed by its opening line, in script order
    assert (
        "- Welcome to Nebula.; text: Welcome to Nebula. / Only Seasonal Zorblax works." in tutorial
    )
    assert first_page(festival).startswith(
        "- Quuxite Festival; kind: game rules (event description)"
    )
    assert "Log in daily for Quuxite." in festival
    assert first_page(preset).startswith("- Messages: preset; kind: game rules (game messages)")
    assert "Wibble OK" not in preset  # a label, not an explanation


def test_search_serves_tip_pages_and_keyword_tooltips(call, conn):
    from czn_data.gamefiles import TEXT

    add_game_files(
        conn,
        {
            TEXT: [
                {"id": "tt_pg@title@006", "text": "Waypoint Area"},
                {"id": "tt_pg@desc@006", "text": "After the <cc>Waypoint</>, Save Data is saved."},
                {"id": "tt_outgame@title@ref@savedata_018", "text": "Equipment Refinement"},
                {"id": "tt_pg@desc@savedata@018", "text": "Refinement effects cost no points."},
                {"id": "tt_outgame@title@ref@town_01", "text": "Ark City"},
                {"id": "tt_outgame@desc@001", "text": "The ID Card shows Zorblax."},
                {"id": "tooltip@title@talent_retain", "text": "Retain"},
                {"id": "tooltip@desc@talent_retain", "text": "This card is not discarded."},
                {"id": "tooltip@title@t_1025_c1", "text": "Quuxite"},
            ]
        },
    )

    waypoint = call("search", query="waypoint area", kinds=["rules"])
    refinement = call("search", query="equipment refinement points", kinds=["rules"])
    retain = call("search", query="retain", kinds=["rules"])

    def first_page(reply: str) -> str:
        return lines_with(reply, "kind: ")[0]

    assert first_page(waypoint).startswith("- Waypoint Area; kind: game rules (tip page)")
    assert "After the Waypoint, Save Data is saved." in waypoint
    # A Save Data tip page takes its title from the matching reference title.
    assert first_page(refinement).startswith("- Equipment Refinement; kind: game rules (tip page)")
    assert first_page(retain).startswith("- Retain; kind: game rules (keyword tooltip)")
    assert "This card is not discarded." in retain
    # A title without a description, or a description without its own title, explains nothing.
    everything = call("search", query="quuxite zorblax ark city", kinds=["rules"])
    assert "Quuxite" not in everything and "Zorblax" not in everything


def test_game_help_wins_a_tie_with_game8(call, conn):
    from czn_data.gamefiles import HELP_MENUS, HELP_PAGES, TEXT

    add_game_files(
        conn,
        {
            TEXT: [
                {"id": "title", "text": "Link Crash"},
                {"id": "desc", "text": "Link Crash breaks shields."},
            ],
            HELP_MENUS[0]: [{"id": "m", "name": "title", "link_tutorial_popup_page_group": "g"}],
            HELP_PAGES[0]: [
                {"id": "p", "group": "g", "sort": "1", "title": "title", "out_game_desc": "desc"}
            ],
        },
    )
    add_game8(
        conn,
        '<html><body><h1 class="p-archiveHeader__title">Link Crash</h1>'
        '<div class="archive-style-wrapper"><div><h2 class="a-header--2">Link Crash</h2></div>'
        "<p>Link Crash breaks shields.</p></div></body></html>",
    )
    # Game8 updated its page after the game files were downloaded.
    conn.execute("UPDATE gamefiles.file SET fetched_at = '2026-01-01'")

    reply = call("search", query="link crash", kinds=["rules", "guides"])

    # Both pages share the title, so it moves to the every item line and kind leads.
    shown = reply.split("\nmore:")[0]
    pages = [line for line in shown.splitlines() if line.startswith("  - ")]
    assert [page.split(";")[0] for page in pages] == [
        "  - game rules (Help, Help menu)",
        "  - guide",
    ]


def test_teams_list_every_guide_team_with_the_member(call):
    reply = call("character_guide", name="mei lin", parts=["teams"])
    assert reply.splitlines()[1:3] == ["teams:", "  guide_teams:"]
    (team,) = lines_with(reply, "- ")
    assert team.startswith("- Spire Fire;") and "listed_for: Mei Lin" in team


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


def best_names(reply: str) -> list[str]:
    """The name of each name's best match in a lookup reply, in order."""
    return [line.removeprefix("name: ") for line in lines_with(reply, "name: ")]


def test_lookup_accepts_keyword_as_effect_and_rejects_unknown_kinds(call, effects):
    reply = call("lookup", names=["exhaust"], kind="keyword")
    assert best_names(reply) == ["Exhaust"] and "kind: effect" in lines_with(reply, "kind: ")
    with pytest.raises(Exception, match="kind"):
        call("lookup", names=["exhaust"], kind="spell")


def test_lookup_finds_terms_defined_inside_other_effects(call, effects):
    reply = call("lookup", names=["Polarization"], kind="effect")
    assert best_names(reply) == ["Equilux"] and "other_matches" not in reply


def test_lookup_falls_back_to_similar_spellings_only_when_nothing_else_matches(call, effects):
    assert best_names(call("lookup", names=["Ionizaton"], kind="effect")) == ["Ionization"]


def test_lookup_answers_every_name_in_one_call(call, effects):
    reply = call("lookup", names=["exhaust", "zzzzzz qqq", "Equilux"], kind="effect")
    assert best_names(reply) == ["Exhaust", "Equilux"]
    assert lines_with(reply, "best: No match.") == ["- zzzzzz qqq; best: No match."]
    many = call("lookup", names=[*["exhaust"] * 10, "Equilux"], kind="effect")
    assert lines_with(many, "- Equilux") == [
        "- Equilux; best: Not looked up: at most 10 names per call."
    ]


def test_lookup_gives_a_partners_ego_skill(call, conn):
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
    found = call("lookup", names=["sig"])
    assert "ego_name: Frail Dominion of Kyrken" in lines_with(found, "ego_name")
    assert "ego_cost: 3" in lines_with(found, "ego_cost")


def test_listing_cards_filters_by_color(call):
    listing = call("list", kind="cards", character="meilin", color="white")
    (white,) = lines_with(listing, "- ")
    assert top_field(listing, "total") == "1"
    assert white.startswith("- Strike;") and "color: White" in white


def test_lookup_detail_lists_every_place_guides_recommend_an_item(call, conn):
    fragments = ParsedPage(
        "Memory Fragments",
        "Executioner's Tool",
        {"fragment_set": [{"name": "Executioner's Tool", "bonuses": []}]},
    )
    store.save_page(conn, FRAGMENTS_URL, "memory_fragments", "", datetime.now(UTC), fragments, None)

    brief = call("lookup", names=["executioners tool"], kind="fragment_set")
    reply = call("lookup", names=["executioners tool"], kind="fragment_set", detail=True)

    assert "recommended_by" not in brief
    (row,) = lines_with(reply, "- Mei Lin")
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
            for stage in ("e1a", "e1b", "e1c")
        ],
    )
    add_e7bot(conn, "czn/encounters/e1a", MIRROR_HALL)
    add_e7bot(conn, "czn/encounters/e1b", MIRROR_HALL_B)
    # A third version whose first screen matches the second's.
    add_e7bot(conn, "czn/encounters/e1c", {**MIRROR_HALL_B, "id": "e1c"})
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

    reply = call("lookup", names=["Rising Dragon Spire"], kind="card")

    (base,) = lines_with(reply, "- Rising Dragon Spire;")
    (best,) = lines_with(reply, "- Rising Dragon Spire V;")
    assert "cost: 3" in base and "text: Game text / line two" in base
    assert "variant" not in base  # empty fields are left out
    assert "text: 9 Hits" in best and f"guide_rating: {rating}" in best
    for internal in ("source", "game_id", "character_slug", "image_url"):
        assert f"{internal}:" not in reply


def test_character_e7bot_lacks_is_served_from_prydwen(call):
    character = call("character_guide", name="mei lin")
    assert "class: Striker" in lines_with(character, "class: ")
    assert "source:" not in character and "slug:" not in character


def test_no_tool_reply_names_a_url_source_site_or_fetch_time(call, conn, game_data, effects):
    add_game8(conn)
    add_post(conn, "1", "Prydwen Notice", "<p>■ Rate-Up https://example.com/x on Game8</p>")
    replies = [
        call("lookup", names=["mei lin", "Rising Dragon Spire", "exhaust", "mirror hall"]),
        call("lookup", names=["Rising Dragon Spire"], detail=True),
        call("character_guide", name="mei lin", parts=["profile", "plan", "teams", "synergies"]),
        call("search", query="rate-up distortion adaptation mei lin"),
        call("get_encounter", name="mirror hall"),
        call("match_card_text", texts=["9 Hits"]),
        call("save_data_cost", character="mei lin", cards=[{"card": "Rising Dragon Spire"}]),
        call("search", active_on="2026-11-20"),
        *(call("list", kind=kind) for kind in ("cards", "equipment", "events", "banners")),
    ]
    for reply in replies:
        lowered = reply.lower()
        for leak in ("http", "prydwen", "game8", "e7bot", "gamewith", "data_fetched_at", "url:"):
            assert leak not in lowered, (leak, reply)


def test_data_status_route_reports_every_source(conn, database_url, monkeypatch, game_data):
    monkeypatch.setenv("DATABASE_URL", database_url)
    conn.execute(
        "INSERT INTO e7bot.run (started_at, finished_at, ok, error)"
        " VALUES (now(), now(), false, 'czn/cards/list: HTTP 500')"
    )
    build(conn)
    app = build_server(None, None).streamable_http_app()

    status = TestClient(app).get("/data-status").json()

    assert status["prydwen"]["pages"] == 0
    assert status["e7bot"]["error"] == "czn/cards/list: HTTP 500"
    assert status["e7bot"]["data_fetched_at"]
    assert status["stove"]["posts"] == 0


def plan_of(call) -> str:
    """Mei Lin's plan part, unindented, so its fields read as top-level ones."""
    lines = call("character_guide", name="mei lin", parts=["plan"]).splitlines()
    start = lines.index("plan:") + 1
    return "\n".join(line.removeprefix("  ") for line in lines[start:])


def test_plan_ranks_epiphanies_and_caps_removals(call, game_data):
    plan = plan_of(call)

    assert (top_field(plan, "season"), top_field(plan, "leaderboard_decks")) == ("s1", "1")
    cards = top_field(plan, "cards").splitlines()
    spire = cards.index(next(line for line in cards if line.startswith("- Rising Dragon Spire;")))
    assert cards[spire + 1 : spire + 3] == [
        "versions:",
        "- V; guide_rating: 5; leaderboard_share: 1.0",
    ]
    removals = lines_with(top_field(plan, "removals"), "- ")
    assert [line.split(";")[0] for line in removals] == ["- Strike"]
    assert lines_with(top_field(plan, "equipment"), "- ")[0] == "- eq_1; leaderboard_share: 1.0"


def test_listing_divine_epiphanies_pages_through_every_match(call, conn):
    sparks = [
        {"id": f"s{n:02}", "description": f"Effect {n:02}", "god_type": "vitor", "rarity": "R"}
        for n in range(35)
    ]
    add_e7bot(conn, "czn/r-sparks/list", sparks)

    first = call("list", kind="divine_epiphanies")
    rest = call("list", kind="divine_epiphanies", offset=30)
    past = call("list", kind="divine_epiphanies", offset=500)

    assert (top_field(first, "total"), "offset=30" in top_field(first, "note")) == ("35", True)
    assert top_field(rest, "note") is None
    texts = lines_with(first, "- Effect") + lines_with(rest, "- Effect")
    assert sorted(texts) == [f"- Effect {n:02}" for n in range(35)]
    assert (top_field(past, "total"), top_field(past, "results")) == ("35", "none")


def test_plan_names_where_each_decks_duplicates_go(call, conn):
    # The fixture deck holds three Strikes, a starting card, which a deck can begin with.
    starting = top_field(plan_of(call), "guide_decks")
    assert "duplicates: Strike x3: base x3 (starting card)" in starting

    # The same deck holding two copies of a unique card instead: one is a Duplicate.
    two_spires = CHARACTER_HTML.replace(
        '<p class="name White">Strike</p></div>\n            '
        '<span class="pw-card-count-badge-preview">x 2</span>',
        '<p class="name White">Rising Dragon Spire IV</p></div>\n            '
        '<span class="pw-card-count-badge-preview">x 2</span>',
        1,
    )
    assert two_spires != CHARACTER_HTML
    store.save_page(
        conn,
        CHARACTER_URL,
        page_kind(CHARACTER_URL),
        two_spires,
        datetime.now(UTC),
        parse_page(CHARACTER_URL, two_spires),
        None,
    )
    unique = top_field(plan_of(call), "guide_decks")
    assert "Rising Dragon Spire x2: IV x2 (1 Duplicates)" in unique


def test_every_tool_call_logs_its_tool_arguments_time_and_reply_size(call, caplog):
    caplog.set_level("INFO", logger="czn_data.server")
    reply = call("list", kind="banners")
    with pytest.raises(Exception, match="Unknown tool"):
        call("no_such_tool")

    (record,) = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert (record.tool, json.loads(record.arguments)) == ("list", {"kind": "banners"})
    assert record.reply_chars == len(reply) and record.seconds >= 0


@pytest.fixture
def cost_cards(conn, game_data):
    """Mei Lin starting with two Strikes, and cards of no character: a Neutral with an
    Epiphany, and a Legendary and a Rare Monster card."""
    add_e7bot(conn, "czn/characters/1027", {"combatant": {"start_cards": [{"name": "Strike"}] * 2}})
    (listed,) = conn.execute("SELECT body FROM e7bot.document WHERE path = 'czn/cards/list'")
    spark_i = card("card_n1_rsp1", "Spark", 1, "Spark I") | {"base_combatant": None}
    others = [
        card("card_n1", "Spark", 1, "Spark", [spark_i], sort="SORT_NEUTRAL"),
        card("card_mon_1", "Bloom: Slaughter", 2, "Shield", sort="SORT_MONSTER_UNIQUE")
        | {"rarity": "RARITY_LEGEND"},
        card("card_mon_2", "Triniel", 2, "Shield", sort="SORT_MONSTER_UNIQUE"),
    ]
    conn.execute(
        "UPDATE e7bot.document SET body = %s WHERE path = 'czn/cards/list'",
        (Jsonb(listed[0] + [c | {"base_combatant": None} for c in others] + [spark_i]),),
    )


def cost(call, cards: list[dict], removed: list[str] = ()) -> tuple[str, list[str]]:
    reply = call("save_data_cost", character="mei lin", cards=cards, removed=list(removed))
    return top_field(reply, "total"), lines_with(top_field(reply, "lines") or "", "- ")


def test_save_data_cost_follows_the_games_faint_memory_rules(call, cost_cards):
    # Three Divine copies of an Epiphany card plus one of another version: 3 Duplicates, the
    # third paid; removed starting cards are paid.
    total, lines = cost(
        call,
        [
            {"card": "Rising Dragon Spire", "version": "V", "divine": True, "copies": 3},
            {"card": "Rising Dragon Spire"},
        ],
        removed=["Strike", "Strike"],
    )
    assert total == "140"
    assert lines[1].startswith("- Rising Dragon Spire; copies: 1; points: 40;")

    # A Neutral is paid with or without its Epiphany; Monster cards by rarity.
    for spark in ("Spark", "Spark I"):
        assert cost(call, [{"card": spark}])[0] == "20"
    assert cost(call, [{"card": "Bloom: Slaughter"}])[0] == "80"
    assert cost(call, [{"card": "Triniel"}])[0] == "50"

    # The starting deck's own copies are free; a third Strike is a Duplicate.
    assert cost(call, [{"card": "Strike", "copies": 2}])[0] == "0"
    assert "Duplicate 1: free" in cost(call, [{"card": "Strike", "copies": 3}])[1][0]

    # Removing a card the character did not start with is free.
    assert cost(call, [], removed=["Rising Dragon Spire"])[0] == "0"


def test_save_data_cost_lists_unknown_cards_without_points(call, cost_cards):
    reply = call("save_data_cost", character="mei lin", cards=[{"card": "Zzyzx Qwv"}])
    assert (top_field(reply, "total"), top_field(reply, "unknown")) == ("0", "Zzyzx Qwv")


def test_save_data_cost_route_gives_programs_the_points_as_json(
    conn, database_url, monkeypatch, cost_cards
):
    monkeypatch.setenv("DATABASE_URL", database_url)
    build(conn)
    client = TestClient(build_server(None, None).streamable_http_app())

    found = client.post(
        "/save-data-cost",
        json={"character": "mei lin", "cards": [{"card": "Spark"}], "removed": ["Strike"]},
    )
    unknown = client.post("/save-data-cost", json={"character": "zzzz", "cards": []})
    unreadable = client.post("/save-data-cost", content=b"not json")

    assert found.status_code == 200
    assert (found.json()["character"], found.json()["total"]) == ("Mei Lin", 40)
    assert unknown.status_code == 404
    assert unreadable.status_code == 400


def test_plan_gives_each_guide_decks_faint_memory_points(call, cost_cards):
    decks = top_field(plan_of(call), "guide_decks")
    # Three Strikes, one with a Divine: the third is a free Duplicate, the Divine costs 20.
    faint_memory = decks.split("faint_memory:\n")[1].splitlines()
    assert faint_memory[0] == "total: 20"
    assert faint_memory[3].startswith("- Strike; copies: 1; points: 20;")


def test_character_guide_for_unknown_character_says_no_match(call):
    assert call("character_guide", name="zzzz", parts=["plan"]) == "No match."


def test_listing_events_filters_by_chaos_and_names_copy_answers(call, game_data):
    listing = call("list", kind="events", chaos="The Blu Pot", reward="copy")
    assert top_field(listing, "total") == "1"
    assert lines_with(listing, "- ")[0].startswith("- Mirror Hall;")
    assert lines_with(listing, "Look into the mirror") == [
        "- Look into the mirror; step: 2; gives: copy"
    ]


def overview_versions(found: str) -> list[str]:
    """Each version block of a get_encounter overview."""
    # A version leads with its screen name, or with its version numbers when every name is
    # the same.
    return re.split(
        r"^- (?=version|Mirror Hall)", top_field(found, "versions"), flags=re.MULTILINE
    )[1:]


def test_get_encounter_without_choices_gives_each_versions_first_screen(call, game_data):
    found = call("get_encounter", name="mirror hall")

    first, second = overview_versions(found)
    assert lines_with(first, "Step inside") == [
        "- Step inside; gives: Event Encounter: [The Mirror]; leads_on: yes"
    ]
    # The other version stays apart, and the first one's follow-up screen is not listed.
    assert "Leave quietly" in second and "Leave quietly" not in first
    assert "Look into the mirror" not in found
    # Versions with the same first screen are listed once, numbering both.
    assert "version: 2, 3" in second


def test_get_encounter_counts_what_the_screen_shows(call, conn, game_data):
    second = overview_versions(call("get_encounter", name="mirror hall"))[1]
    assert "shows: 3" in second
    assert lines_with(second, "- Pray") == ["- Pray; random: screen shows 1 of 2"]
    # Plural markers of a single reward are dropped, and the space after a number restored.
    assert lines_with(second, "- Sing")[0].endswith(
        "gives: Obtain Cards[Hymn] 1 Card and 1 time to 1 random card"
    )

    # A condition on a choice depends on game state, so no count is stated.
    conditional = json.loads(json.dumps(MIRROR_HALL_B))
    conditional["options"][0]["appear_link_encounter_trigger_id"] = ["trg_x"]
    conn.execute(
        "UPDATE e7bot.document SET body = %s WHERE path = 'czn/encounters/e1b'",
        (Jsonb(conditional),),
    )
    second = overview_versions(call("get_encounter", name="mirror hall"))[1]
    assert "shows:" not in second
    assert lines_with(second, "- Pray") == ["- Pray; random: may not appear"]


def test_get_encounter_gives_the_screen_with_those_choices_and_previews_the_next(call, game_data):
    found = call("get_encounter", name="mirror hall", choices=["Step inside"])

    assert top_field(found, "step") == "1"
    assert lines_with(found, "Step inside") == ["- Step inside; mystery_flag: Z"]
    assert lines_with(found, "Look into the mirror") == [
        "- Look into the mirror; dice: Insight 6; gives: Copy 1 card; fails: End the event"
    ]
    assert lines_with(found, "Turn back") == ["- Turn back; appears_when: trg_tried; leads_on: yes"]
    # The follow-up screen carries the title the game gives it, not the event's name.
    assert lines_with(found, "name: The Mirror") == ["name: The Mirror"]


def test_get_encounter_names_each_choice_when_all_share_a_text(call, conn, game_data):
    # The screen lists only the two Pray choices, each with its own reward, so each would
    # still have text to lead its line without the answer.
    same_text = json.loads(json.dumps(MIRROR_HALL_B))
    same_text["options"] = same_text["options"][1:3]
    same_text["options"][0]["success_link_encounter_option_eff"] = {"reward0_reward": COPY_REWARD}
    same_text["options"][1]["simple_flavor_text"] = "Pray"
    conn.execute(
        "UPDATE e7bot.document SET body = %s WHERE path = 'czn/encounters/e1b'",
        (Jsonb(same_text),),
    )

    found = call("get_encounter", name="mirror hall", choices=["Pray"])
    overview = overview_versions(call("get_encounter", name="mirror hall"))[1]

    assert len(lines_with(found, "- Pray")) == 2
    pray = [line for line in overview.splitlines() if "gives:" in line]
    assert [line.strip().split(";")[0] for line in pray] == ["- Pray", "- Pray"]
    assert "gives: Copy 1 card" in pray[0]


def test_get_encounter_finds_a_follow_up_screen_by_its_choices(call, game_data):
    # The screen's title is not an event name; the choices, cut off as on screen, find it.
    found = call("get_encounter", name="The Mirror", choices=["[Insight] Look into the mi…"])

    assert top_field(found, "name") == "Mirror Hall"
    assert top_field(found, "screen") == "The Mirror"
    assert top_field(found, "step") == "2"
    assert "Turn back" not in found
    assert lines_with(found, "failure") == ["- failure; text: End the event"]
    assert "Copy 1 card; class: copy" in found


def test_get_encounter_finds_the_event_by_a_follow_up_screens_title(call, game_data):
    found = call("get_encounter", name="The Mirror")

    assert top_field(found, "name") == "Mirror Hall"
    assert top_field(found, "follow_up") == (
        "The Mirror is a later screen of this event; pass its choices"
    )


def test_get_encounter_names_versions_that_lead_to_a_version(call, conn, game_data):
    # e1b's choices open e1c: one by a link, both naming the screen differently.
    hall = {
        "id": "e1c",
        "name": "Mirror Hall",
        "replace_link_encounter_id": "e1b",
        "options": [{"id": "o7", "simple_flavor_text": "Wait"}],
    }
    linked = json.loads(json.dumps(MIRROR_HALL_B))
    for option, title in ((linked["options"][1], "Altar"), (linked["options"][2], "Shrine")):
        option["success_link_encounter_option_eff"] = {
            "overview_eff_desc": f"Event Encounter: [{title}]",
            "next_encounter_id": "e1c",
            "next_encounter": hall,
        }
    for path, body in (("e1b", linked), ("e1c", hall)):
        conn.execute(
            "UPDATE e7bot.document SET body = %s WHERE path = %s",
            (Jsonb(body), f"czn/encounters/{path}"),
        )

    found = call("get_encounter", name="mirror hall")

    second, third = overview_versions(found)[1:]
    assert "opened_from: 2" in third
    # e1c's screen names e1b in replace_link_encounter_id.
    assert "replace_link_from: 3" in second
    # Openers name two titles for the screen, so it keeps the event's name.
    pray = call("get_encounter", name="mirror hall", choices=["Pray"])
    assert "Altar" not in pray.replace("[Altar]", "")


def test_build_rejects_a_link_to_a_missing_step_and_keeps_the_previous_events(
    call, conn, game_data
):
    call("get_encounter", name="mirror hall")
    broken = json.loads(json.dumps(MIRROR_HALL))
    broken["options"][0]["success_link_encounter_option_eff"] = {"next_encounter_id": "nowhere"}
    conn.execute(
        "UPDATE e7bot.document SET body = %s WHERE path = 'czn/encounters/e1a'", (Jsonb(broken),)
    )

    with pytest.raises(BuildError, match="no step nowhere"):
        build(conn)

    assert conn.execute("SELECT count(*) FROM encounter").fetchone()[0] == 3


def test_listing_equipment_by_chaos_matches_listed_and_shortened_zone_names(call, conn, game_data):
    url = "https://www.prydwen.gg/chaos-zero-nightmare/equipment"
    item = {"rarity": "Rare", "slot": "Weapon", "text": "Hit.", "stats": {}}
    places = {"Pot Blade": "Blue Pot", "Mist Blade": "City of Mist, The Blue Pot", "Shop": "Shop"}
    rows = [item | {"name": name, "source": place} for name, place in places.items()]
    parsed = ParsedPage("Equipment", "Equipment", {"equipment": rows})
    store.save_page(conn, url, "equipment", "", datetime.now(UTC), parsed, None)

    listing = call("list", kind="equipment", chaos="blue pot")

    assert [line.split(";")[0] for line in lines_with(listing, "- ")] == [
        "- Mist Blade",
        "- Pot Blade",
    ]


def test_listing_equipment_leaves_out_tutorial_copies(call, conn):
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

    listing = call("list", kind="equipment", name="Crimson Sword")

    (sword,) = lines_with(listing, "Crimson Sword")
    assert "text: Hit hard." in sword


def test_unknown_chaos_is_an_error_naming_the_known_ones(call, game_data):
    with pytest.raises(Exception, match=r"Known: The Blue Pot$"):
        call("list", kind="events", chaos="Nowhere At All")
