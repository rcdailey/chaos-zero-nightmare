"""Contract: Prydwen markup becomes complete records, and layout changes fail loudly."""

from pathlib import Path

import pytest

from czn_data.parse import ParseError, page_kind, parse_page

BASE = "https://www.prydwen.gg/chaos-zero-nightmare"
CHARACTER_URL = f"{BASE}/characters/mei-lin"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/characters/mei-lin", "character"),
        ("/characters", "document"),
        ("/characters/element/passion", "document"),
        ("/neutral-cards", "neutral_cards"),
        ("/equipment", "equipment"),
        ("/memory-fragments", "memory_fragments"),
        ("/partners", "partners"),
        ("/guides/effects-dictionary", "effects"),
        ("/guides/beginner-guide", "document"),
        ("", "document"),
    ],
)
def test_page_kind(path, kind):
    assert page_kind(BASE + path) == kind


def test_character_page_yields_profile_cards_variants_potentials_and_egos():
    page = parse_page(CHARACTER_URL, CHARACTER_HTML)

    (character,) = page.records["character"]
    assert character | {"sections": None} == {
        "slug": "mei-lin",
        "name": "Mei Lin",
        "rarity": 5,
        "element": "Passion",
        "class": "Striker",
        "updated_text": "25/March/2026",
        "image_url": "https://cdn.prydwen.gg/images/chaos/characters/mei-lin_card.webp",
        "sections": None,
    }

    cards = {(c["base_name"], c["variant"]): c for c in page.records["card"]}
    assert set(cards) == {("Strike", ""), ("Rising Dragon Spire", ""), ("Rising Dragon Spire", "V")}
    assert cards[("Strike", "")]["category"] == "starting"
    assert cards[("Strike", "")]["text"] == "100% Damage."
    base = cards[("Rising Dragon Spire", "")]
    assert base["tags"] == ["Unique"]
    assert base["text"] == "100% Damage.\n2 Ember."
    variant = cards[("Rising Dragon Spire", "V")]
    assert variant["rating"] == 5
    assert variant["category"] == "unique"
    assert variant["text"] == "150% Damage.\nFor 1 turn, each Passion Card used, 1 extra Hit."

    assert page.records["potential"] == [
        {
            "position": "1",
            "name": "Epiphany Bonus",
            "text": "When entering Chaos, obtain 1 random Rare Card with an activated Epiphany.",
            "character_slug": "mei-lin",
        }
    ]
    (ego,) = page.records["manifest_ego"]
    assert ego["level"] == 1
    assert ego["performance"] == "100.00%"
    assert ego["review"] == "Not a terrible first node."
    assert "Veronica" in page.body


def test_epiphany_review_is_split_over_the_cards_it_describes():
    cards = {
        (c["base_name"], c["variant"]): c
        for c in parse_page(CHARACTER_URL, CHARACTER_HTML).records["card"]
    }
    base, variant = cards[("Rising Dragon Spire", "")], cards[("Rising Dragon Spire", "V")]
    assert base["epiphany_review"] == "Spire is her main damage card."
    assert base["neutral_review"] == "Target Neutral Epiphany: Draw 1. Rarely worth it."
    assert base["target_neutral"] == "Draw 1"
    assert base["divine_review"] == "Take Cost Reduction."
    assert variant["epiphany_review"] == "The best option.\nIt scales with every Passion card."
    assert variant["alias"] == "Ascended"


def test_bracketed_target_neutral_label():
    # Seen on the Anika page: the pick is its own paragraph.
    html = CHARACTER_HTML.replace(
        "<p><strong>Neutral:</strong> Target Neutral Epiphany: Draw 1. Rarely worth it.</p>",
        "<p>[Target Neutral Epiphany: 3 Morale]</p><p>Neutrals beat specific Epiphanies.</p>",
    )
    base = next(
        c
        for c in parse_page(CHARACTER_URL, html).records["card"]
        if c["name"] == "Rising Dragon Spire"
    )
    assert base["target_neutral"] == "3 Morale"
    assert base["neutral_review"] == "Neutrals beat specific Epiphanies."


def test_character_guide_sections_become_records():
    page = parse_page(CHARACTER_URL, CHARACTER_HTML)
    records = page.records
    for rows in records.values():
        for row in rows:
            row.pop("character_slug", None)

    assert records["team"] == [
        {"name": "Spire Fire", "tier": "T1", "members": ["Mei Lin", "Veronica", "Tiphera"]}
    ]
    assert records["deck"] == [
        {
            "name": "Standard Dragon Spire",
            "partner": "Scarlet",
            "equipment": ["Criticism of Discord"],
            "fragments": ["Executioner's Tool"],
            "cards": [{"name": "Strike", "count": 2}],
            "notes": "Buff Spire, then Ravage.",
        }
    ]
    assert records["character_equipment"] == [
        {
            "tab": "Swamp",
            "best": True,
            "slots": {"Weapons": ["Criticism of Discord"]},
            "notes": None,
        },
        {
            "tab": "Season 1: Lab Zero",
            "best": False,
            "slots": {"Armor": ["Wings of Freedom"]},
            "notes": "Only for Lab Zero.",
        },
    ]
    assert records["seasonal_power"] == [
        {"season": "Season 5", "best": True, "text": "Memory cards add hits."}
    ]
    assert records["fragment_rank"] == [
        {
            "build": "Default",
            "position": 1,
            "sets": ["Executioner's Tool", "Black Wing"],
            "performance": "100.00%",
            "notes": "Consistent damage.",
        }
    ]
    assert records["partner_rank"] == [
        {
            "position": 1,
            "partner": "Scarlet",
            "dupes": "E4",
            "performance": "135.16%",
            "notes": None,
        }
    ]
    assert records["synergy"] == [{"name": "Veronica", "points": ["Doubles as a draw unit."]}]

    # Structured sections are not repeated as prose; the rest keep short titles.
    sections = page.records["character"][0]["sections"]
    assert sections == [
        {"title": "Pros", "text": "Incredible self-buffing capabilities."},
        {"title": "Stats", "text": "CRIT Rate > CRIT DMG"},
    ]


def test_structured_guide_section_that_no_longer_parses_fails():
    html = CHARACTER_HTML.replace("czn-team-tile", "team-tile-v2")
    with pytest.raises(ParseError) as failure:
        parse_page(CHARACTER_URL, html)
    assert failure.value.field == "team"


def test_epiphany_base_card_mislabeled_with_a_numeral():
    # Seen on the Blazebell page: heading and base card both read "Flametouch I".
    html = CHARACTER_HTML.replace(
        '<h3>Rising Dragon Spire</h3></div>\n      <div class="build-table-body">',
        '<h3>Rising Dragon Spire I</h3></div>\n      <div class="build-table-body">',
    ).replace(
        '<p class="name Blue">Rising Dragon Spire</p><p class="type"><strong>',
        '<p class="name Blue">Rising Dragon Spire I</p><p class="type"><strong>',
    )
    assert html.count("Rising Dragon Spire I<") == 2

    page = parse_page(CHARACTER_URL, html)

    variants = [(c["base_name"], c["variant"]) for c in page.records["card"]]
    assert ("Rising Dragon Spire", "V") in variants
    assert ("Rising Dragon Spire", "I") not in variants


@pytest.mark.parametrize(
    ("broken_markup", "field"),
    [
        # Card markup renamed inside a section that is still present.
        (("chaos-card-inside", "card-v2"), "card.starting"),
        # Potential entries renamed.
        (("constellation-database-card", "potential-v2"), "potential"),
        # Character name element renamed.
        (("czn-hero-name", "hero-v2"), "character.name"),
        # Card effect text renamed on a card without tags.
        (('skill-with-coloring Passion"><b>100%</b> Damage.&#8203;', 'x">'), "card.starting.text"),
    ],
)
def test_layout_change_names_the_failed_field(broken_markup, field):
    old, new = broken_markup
    with pytest.raises(ParseError) as failure:
        parse_page(CHARACTER_URL, CHARACTER_HTML.replace(old, new))
    assert failure.value.field == field


def test_named_epiphany_variant():
    # Seen on the Nine page: "Hew (Extreme)" instead of "Hew III".
    html = CHARACTER_HTML.replace("Rising Dragon Spire V<", "Rising Dragon Spire (Extreme)<")
    page = parse_page(CHARACTER_URL, html)
    variants = {
        c["variant"] for c in page.records["card"] if c["base_name"] == "Rising Dragon Spire"
    }
    assert variants == {"", "(Extreme)"}


def test_cards_not_yet_published_are_allowed():
    # Seen on the Dahlia page while it was new.
    start = CHARACTER_HTML.index('<section class="kit-section">')
    end = CHARACTER_HTML.index(
        '<section class="kit-section">\n      <div class="subsection-title"><h3>Potential'
    )
    placeholder = (
        '<section class="kit-section"><div class="subsection-title"><h3>Cards</h3></div>'
        '<div class="info-box"><p>Mei Lin cards aren\'t available yet.</p></div></section>'
    )
    page = parse_page(CHARACTER_URL, CHARACTER_HTML[:start] + placeholder + CHARACTER_HTML[end:])
    assert page.records["character"][0]["name"] == "Mei Lin"


def test_tags_only_status_card_has_empty_text():
    # Seen on the Kayron page: "Futility" has only an [Exhaust] tag.
    html = CHARACTER_HTML.replace(
        '<div class="skill-with-coloring Passion"><b>100%</b> Damage.<br/><b>2</b> <u>Ember</u>.'
        "</div>",
        "",
    )
    page = parse_page(CHARACTER_URL, html)
    base = next(c for c in page.records["card"] if c["name"] == "Rising Dragon Spire")
    assert (base["tags"], base["text"]) == (["Unique"], "")


def test_missing_optional_section_is_allowed():
    start = CHARACTER_HTML.index(
        '<section class="kit-section">\n      <div class="subsection-title"><h3>Potential'
    )
    end = CHARACTER_HTML.index("</section>", start) + len("</section>")
    page = parse_page(CHARACTER_URL, CHARACTER_HTML[:start] + CHARACTER_HTML[end:])
    assert page.records["potential"] == []


EQUIPMENT_HTML = """<html><body><div id="S:1"><div class="generic-page">
<h1>Chaos Zero Nightmare Equipment</h1>
<div class="chaos-eq-box box"><div class="chaos-header"><div class="chaos-eq-image rarity-3">
<img alt="A Lonesome Wedding Ring"
src="https://cdn.prydwen.gg/images/chaos/equipment/equip_206.webp"/>
</div><div class="chaos-data"><h4>A Lonesome Wedding Ring</h4><div class="chaos-info">
<p>Rarity:<!-- --> <strong class="rarity-3">Rare</strong></p><p>Slot: <strong>Trinket</strong></p>
</div></div></div><div class="chaos-content"><div class="skill-with-coloring">At the start of the
turn, Discard <b>1</b> random ally <u>Quietus</u> card in hand.</div></div><div class="chaos-stats">
<p>HP<!-- -->: <strong>20/37/54/66/75</strong></p>
<p class="source">Source: <strong>Theater of Illusions</strong></p></div></div>
</div></div></body></html>"""


def test_equipment_page():
    page = parse_page(f"{BASE}/equipment", EQUIPMENT_HTML)
    assert page.records["equipment"] == [
        {
            "name": "A Lonesome Wedding Ring",
            "rarity": "Rare",
            "slot": "Trinket",
            "text": "At the start of the turn, Discard 1 random ally Quietus card in hand.",
            "stats": {"HP": "20/37/54/66/75"},
            "source": "Theater of Illusions",
            "image_url": "https://cdn.prydwen.gg/images/chaos/equipment/equip_206.webp",
        }
    ]


def test_list_page_with_no_entries_fails():
    html = EQUIPMENT_HTML.replace("chaos-eq-box", "eq-box-v2")
    with pytest.raises(ParseError) as failure:
        parse_page(f"{BASE}/equipment", html)
    assert failure.value.field == "equipment"


EFFECTS_HTML = """<html><body><div id="S:1"><h1>Effects Dictionary</h1><table>
<tr><th class="pw-col-name">Name</th><th class="pw-col-type">Type</th>
<th class="pw-col-desc">Description</th></tr>
<tr><td class="pw-col-name"><strong>Exhaust</strong></td><td class="pw-col-type">
<strong class="type-text">Tag</strong></td>
<td class="pw-col-desc">When used, this card moves to the Exhaust pile.</td></tr>
</table></div></body></html>"""


def test_effects_dictionary_page():
    page = parse_page(f"{BASE}/guides/effects-dictionary", EFFECTS_HTML)
    assert page.records["effect"] == [
        {
            "name": "Exhaust",
            "type": "Tag",
            "description": "When used, this card moves to the Exhaust pile.",
        }
    ]
