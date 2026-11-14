"""Turn saved prydwen.gg HTML into database records.

Prydwen renders every page on the server, so the data sits in the HTML markup (CSS classes such
as ``chaos-card-inside``), not in a JSON payload. These selectors are the part of the system that
breaks when Prydwen changes its layout. Each parser raises ``ParseError`` naming the field that
failed, so a layout change shows up as an alert instead of silently empty data.

Validation rule: a section that exists on the page must parse into at least one valid record.
A missing section is allowed (new characters often have incomplete pages); a present section that
yields nothing means the selectors no longer match.
"""

import copy
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, Tag

GAME_PATH = "/chaos-zero-nightmare"

# Character listing pages that share the characters/ prefix but are not character profiles.
CHARACTER_LISTING_SEGMENTS = {"class", "element", "rarity"}

ROMAN_VARIANTS = {"I", "II", "III", "IV", "V"}

CARD_CATEGORIES = {
    "Starting Cards & Ego Card": "starting",
    "Unique Cards": "unique",
    "Generated Cards": "generated",
}


class ParseError(Exception):
    """The page no longer matches the expected layout."""

    def __init__(self, field_name: str, message: str):
        super().__init__(f"{field_name}: {message}")
        self.field = field_name


@dataclass
class ParsedPage:
    """Everything one page contributes: a searchable document plus structured rows per table."""

    title: str
    body: str
    records: dict[str, list[dict]] = field(default_factory=dict)


def page_kind(url: str) -> str:
    """Classify a Prydwen URL; ``document`` pages only feed full-text search."""
    path = urlparse(url).path.removeprefix(GAME_PATH).strip("/")
    parts = path.split("/") if path else []
    if len(parts) == 2 and parts[0] == "characters":
        return "character"
    if len(parts) == 3 and parts[0] == "characters" and parts[1] not in CHARACTER_LISTING_SEGMENTS:
        return "character"
    return {
        "neutral-cards": "neutral_cards",
        "equipment": "equipment",
        "memory-fragments": "memory_fragments",
        "partners": "partners",
        "guides/effects-dictionary": "effects",
    }.get(path, "document")


def parse_page(url: str, html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    root = _content_root(soup)
    page = ParsedPage(title=_title(soup, root), body=text_of(root))
    if not page.body:
        raise ParseError("document.body", "page has no text content")
    kind = page_kind(url)
    parser = _PARSERS.get(kind)
    if parser:
        page.records = parser(root, url)
    return page


def text_of(el: Tag | None) -> str:
    """Visible text with line breaks kept at block boundaries and spacing normalized."""
    if el is None:
        return ""
    el = copy.copy(el)
    # Source line breaks are layout-insignificant, as in a browser; breaks come from markup.
    for string in el.find_all(string=True):
        if isinstance(string, Comment):
            string.extract()
        else:
            string.replace_with(re.sub(r"\s+", " ", string))
    for br in el.find_all("br"):
        br.replace_with("\n")
    for block in el.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.insert_before("\n")
        block.insert_after("\n")
    for cell in el.find_all(["td", "th"]):
        cell.insert_after(" | ")
    text = el.get_text().replace("\u200b", "")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip(" |") for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _content_root(soup: BeautifulSoup) -> Tag:
    # Next.js streams the page body into a hidden Suspense container; fall back to <body>.
    root = soup.find(id="S:1") or soup.find("main") or soup.body
    if root is None:
        raise ParseError("document.root", "no page body")
    for noise in root.select(".breadcrumb, .czn-category-links, .global-how-to-get"):
        noise.decompose()
    return root


def _title(soup: BeautifulSoup, root: Tag) -> str:
    h1 = root.find("h1")
    if h1:
        return text_of(h1).replace("\n", " ")
    return soup.title.get_text(strip=True) if soup.title else ""


def _required_text(el: Tag | None, field_name: str) -> str:
    text = text_of(el)
    if not text:
        raise ParseError(field_name, "missing or empty")
    return text


def _image(el: Tag) -> str | None:
    img = el.find("img")
    return img.get("src") if img else None


def _card(el: Tag, field_prefix: str) -> dict:
    """One rendered card (``.chaos-card-inside``)."""
    name = _required_text(el.select_one(".name"), f"{field_prefix}.name")
    content = el.select_one(".chaos-content")
    text_el = content.select_one(".skill-with-coloring:not(.rules)") if content else None
    rules_el = content.select_one(".rules") if content else None
    border = el.select_one(".left-border")
    border_classes = [c for c in border.get("class", []) if c != "left-border"] if border else []
    name_classes = [c for c in el.select_one(".name").get("class", []) if c != "name"]
    tags = [t.get_text(strip=True).strip("[]") for t in el.select(".tags .inline-name")]
    # Some status cards have only tags ("[Exhaust]") and no effect text.
    text = text_of(text_el) if tags else _required_text(text_el, f"{field_prefix}.text")
    return {
        "name": name,
        "cost": _required_text(el.select_one(".cost"), f"{field_prefix}.cost"),
        "type": _required_text(el.select_one(".type"), f"{field_prefix}.type"),
        # Name color marks card rarity in game (White, Blue, Purple, Gold, Orange).
        "color": name_classes[0] if name_classes else None,
        "element": border_classes[0] if border_classes else None,
        "tags": tags,
        "text": text,
        "rules": text_of(rules_el) or None,
        "image_url": _image(el),
    }


def _section(root: Tag, heading: str) -> Tag | None:
    """A character kit section (``section.kit-section``) found by its exact heading text."""
    for section in root.select("section.kit-section"):
        title = section.select_one(".subsection-title h3")
        if title and text_of(title) == heading:
            return section
    return None


def _numbered_entries(section: Tag, field_prefix: str) -> list[dict]:
    entries = []
    for article in section.select("article.constellation-database-card"):
        entries.append(
            {
                "label": _required_text(article.select_one(".mini-label"), f"{field_prefix}.label"),
                "name": _required_text(article.select_one("h3"), f"{field_prefix}.name"),
                "text": _required_text(
                    article.select_one(".kit-description"), f"{field_prefix}.text"
                ),
            }
        )
    if not entries and not section.select_one(".info-box"):  # .info-box: "added soon" notice
        raise ParseError(field_prefix, "section present but no entries parsed")
    return entries


def _chip(root: Tag, selector: str, field_name: str) -> str:
    return _required_text(root.select_one(f".czn-character-page .chip.{selector}"), field_name)


def parse_character(root: Tag, url: str) -> dict[str, list[dict]]:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if root.select_one(".czn-character-page") is None:
        raise ParseError("character.root", "no .czn-character-page container")
    name = _required_text(root.select_one("h1.czn-hero-name"), "character.name")
    rarity_text = _chip(root, "rarity", "character.rarity")
    rarity = re.match(r"\d+", rarity_text)
    if not rarity:
        raise ParseError("character.rarity", f"not a number: {rarity_text!r}")

    # Kit card sections vary by character: usually "Starting Cards & Ego Card" and "Unique
    # Cards", sometimes a single "Cards", plus "Generated Cards" for cards created in combat.
    # New characters show a "cards aren't available yet" notice (.info-box) instead.
    cards: list[dict] = []
    pending = False
    for section in root.select("section.kit-section"):
        title = text_of(section.select_one(".subsection-title h3"))
        if "Cards" not in title.split():
            continue
        category = CARD_CATEGORIES.get(title, title.lower().replace(" ", "_"))
        found = section.select(".chaos-card-inside")
        if not found and section.select_one(".info-box"):
            pending = True
            continue
        if not found:
            raise ParseError(f"card.{category}", "section present but no cards parsed")
        for el in found:
            card = _card(el, f"card.{category}")
            cards.append(card | {"category": category, "base_name": card["name"], "variant": ""})
    if not cards and not pending:
        raise ParseError("card", "no card sections found")

    cards.extend(_epiphany_variants(root, cards))

    potential_section = _section(root, "Potential")
    potentials = [
        {"position": p["label"], "name": p["name"], "text": p["text"]}
        for p in (_numbered_entries(potential_section, "potential") if potential_section else [])
    ]
    ego_section = _section(root, "Manifest Ego (dupes)")
    egos = _manifest_egos(root, ego_section) if ego_section else []

    for record in (*cards, *potentials, *egos):
        record["character_slug"] = slug
    character = {
        "slug": slug,
        "name": name,
        "rarity": int(rarity.group()),
        "element": _chip(root, "character-element", "character.element"),
        "class": _chip(root, "character-class", "character.class"),
        "updated_text": text_of(root.select_one(".freshness strong")) or None,
        "image_url": _image(root.select_one(".czn-character-page") or root),
        "sections": _guide_sections(root),
    }
    return {
        "character": [character],
        "card": cards,
        "potential": potentials,
        "manifest_ego": egos,
    }


def _epiphany_variants(root: Tag, base_cards: list[dict]) -> list[dict]:
    """Epiphany versions (I-V) from the build section, rated by Prydwen with stars.

    The base card already comes from the kit section; this adds its rating and review to that
    base row and returns one new row per variant. Prydwen lists the base card first and the
    variants after it. Names are not reliable for telling them apart: some pages title the
    base card and heading with a numeral too ("Flametouch I" twice), so position decides.
    """
    by_name = {c["base_name"]: c for c in base_cards}
    variants = []
    for article in root.select("article.build-table-card"):
        review = article.select_one(".pw-card-epi-review")
        if review is None:
            continue
        heading = _required_text(article.select_one(".build-table-title h3"), "epiphany.base")
        base_name = _strip_variant(heading)[0]
        comment = text_of(article.select_one(".czn-card-group-comment")) or None
        singles = review.select(".single-card")
        if len(singles) < 2:
            raise ParseError("epiphany.variant", f"{heading}: no variants parsed")
        for position, single in enumerate(singles):
            el = single.select_one(".chaos-card-inside")
            if el is None:
                raise ParseError("epiphany.variant", f"{heading}: variant without card")
            card = _card(el, "epiphany.variant")
            rating = single.select_one(".rating").get_text().count("★") or None
            if position == 0:
                if base_name in by_name:
                    by_name[base_name].update(epiphany_review=comment, rating=rating)
                continue
            # Most variants are numbered ("Flametouch IV"); some are named ("Hew (Extreme)").
            stem, suffix = _strip_variant(card["name"])
            if stem != base_name:
                suffix = card["name"].removeprefix(base_name).strip() or card["name"]
            base = by_name.get(base_name, {})
            variants.append(
                card
                | {
                    "category": base.get("category", "unique"),
                    "base_name": base_name,
                    "variant": suffix,
                    "rating": rating,
                }
            )
    return variants


def _strip_variant(name: str) -> tuple[str, str]:
    """Split "Flametouch IV" into ("Flametouch", "IV"); names without a numeral get ""."""
    stem, _, last = name.rpartition(" ")
    return (stem, last) if stem and last in ROMAN_VARIANTS else (name, "")


def _manifest_egos(root: Tag, section: Tag) -> list[dict]:
    """Manifest Ego levels E1-E6, merged with Prydwen's per-level performance review."""
    reviews = {}
    for row in root.select(".czn-dupe-row"):
        cells = row.select(".czn-dupe-cell")
        level = re.fullmatch(r"Ego (\d)", text_of(cells[0])) if len(cells) == 3 else None
        if level:
            reviews[int(level.group(1))] = (text_of(cells[1]) or None, text_of(cells[2]) or None)
    egos = []
    for entry in _numbered_entries(section, "manifest_ego"):
        level = re.fullmatch(r"E\s*(\d)", entry["label"])
        if not level:
            raise ParseError("manifest_ego.label", f"unexpected label {entry['label']!r}")
        performance, review = reviews.get(int(level.group(1)), (None, None))
        egos.append(
            {
                "level": int(level.group(1)),
                "name": entry["name"],
                "text": entry["text"],
                "performance": performance,
                "review": review,
            }
        )
    return egos


def _guide_sections(root: Tag) -> list[dict]:
    """Prydwen's written review: pros, cons, intro, teams, partners, gear advice, and so on."""
    sections = []
    for el in root.select("article.pros, article.cons, .review-source-panel, .build-table-card"):
        if el.select_one(".pw-card-epi-review"):
            continue
        heading = el.select_one("h3")
        title = text_of(heading).replace("\n", " ") if heading else ""
        body = text_of(el)
        if title and body.startswith(title):
            body = body[len(title) :].strip()
        if body:
            sections.append({"title": title, "text": body})
    return sections


def parse_neutral_cards(root: Tag, url: str) -> dict[str, list[dict]]:
    cards = []
    for el in root.select(".chaos-card-inside"):
        card = _card(el, "card.neutral")
        cards.append(
            card
            | {
                "category": "neutral",
                "base_name": card["name"],
                "variant": "",
                "seasonal": el.select_one(".seasonal") is not None,
            }
        )
    if not cards:
        raise ParseError("card.neutral", "no cards parsed")
    return {"card": cards}


def parse_equipment(root: Tag, url: str) -> dict[str, list[dict]]:
    items = []
    for box in root.select(".chaos-eq-box"):
        info = {
            text_of(p).split(":", 1)[0]: text_of(p.find("strong"))
            for p in box.select(".chaos-info p")
        }
        stats = {}
        for p in box.select(".chaos-stats p:not(.source)"):
            key, _, value = text_of(p).partition(":")
            stats[key.strip()] = value.strip()
        items.append(
            {
                "name": _required_text(box.select_one("h4"), "equipment.name"),
                "rarity": info.get("Rarity") or _missing("equipment.rarity"),
                "slot": info.get("Slot") or _missing("equipment.slot"),
                "text": _required_text(
                    box.select_one(".chaos-content .skill-with-coloring"), "equipment.text"
                ),
                "stats": stats,
                "source": text_of(box.select_one(".source strong")) or None,
                "image_url": _image(box),
            }
        )
    if not items:
        raise ParseError("equipment", "no equipment parsed")
    return {"equipment": items}


def parse_memory_fragments(root: Tag, url: str) -> dict[str, list[dict]]:
    sets = []
    for box in root.select(".czn-fragment-box"):
        heading = box.select_one("h4")
        name_text = "".join(heading.find_all(string=True, recursive=False)) if heading else ""
        bonuses = []
        for bonus in box.select(".set-bonus"):
            pieces = re.search(r"(\d)-PC", text_of(bonus.select_one(".title")))
            if not pieces:
                raise ParseError("fragment_set.pieces", "bonus without piece count")
            bonuses.append(
                {
                    "pieces": int(pieces.group(1)),
                    "text": _required_text(
                        bonus.select_one(".skill-with-coloring"), "fragment_set.text"
                    ),
                }
            )
        if not bonuses:
            raise ParseError("fragment_set.bonuses", "set without bonuses")
        sets.append(
            {
                "name": name_text.strip() or _missing("fragment_set.name"),
                "bonuses": bonuses,
                "image_url": _image(box),
            }
        )
    if not sets:
        raise ParseError("fragment_set", "no sets parsed")
    return {"fragment_set": sets}


def parse_partners(root: Tag, url: str) -> dict[str, list[dict]]:
    # Only the active tab (Partner Passive) is rendered on the server; the EGO Skill and Stats
    # tabs load with JavaScript, so they are not available without a browser.
    partners = []
    for el in root.select(".single-partner"):
        info = {
            text_of(p).split(":", 1)[0]: text_of(p.find("strong"))
            for p in el.select(".chaos-partner-info p")
        }
        rarity = re.match(r"\d", info.get("Rarity", ""))
        if not rarity:
            raise ParseError("partner.rarity", f"not a number: {info.get('Rarity')!r}")
        passive = el.select_one(".passive")
        partners.append(
            {
                "name": _required_text(el.select_one("h4"), "partner.name"),
                "rarity": int(rarity.group()),
                "class": info.get("Class") or _missing("partner.class"),
                "passive_name": text_of(passive.select_one("h5") if passive else None) or None,
                "passive_text": _required_text(
                    passive.select_one(".skill-with-coloring") if passive else None,
                    "partner.passive_text",
                ),
                "image_url": _image(el),
            }
        )
    if not partners:
        raise ParseError("partner", "no partners parsed")
    return {"partner": partners}


def parse_effects(root: Tag, url: str) -> dict[str, list[dict]]:
    effects = []
    for row in root.select("tr:has(td.pw-col-name)"):
        effects.append(
            {
                "name": _required_text(row.select_one(".pw-col-name"), "effect.name"),
                "type": text_of(row.select_one(".pw-col-type")) or None,
                "description": _required_text(row.select_one(".pw-col-desc"), "effect.description"),
            }
        )
    if not effects:
        raise ParseError("effect", "no effects parsed")
    return {"effect": effects}


def _missing(field_name: str):
    raise ParseError(field_name, "missing or empty")


_PARSERS = {
    "character": parse_character,
    "neutral_cards": parse_neutral_cards,
    "equipment": parse_equipment,
    "memory_fragments": parse_memory_fragments,
    "partners": parse_partners,
    "effects": parse_effects,
}
