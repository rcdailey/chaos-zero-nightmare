"""Merge the ``e7bot`` and ``prydwen`` schemas into the tables the MCP server reads, and split
the ``stove`` schema's posts into searchable sections (``czn_data.posts``).

Scrapes only collect: e7bot JSON as received, Prydwen HTML plus rows parsed from each page.
This step turns both into one record per game entity. Game facts come from e7bot when it has
the record; a field e7bot lacks is filled from Prydwen. Records e7bot never had come from
Prydwen alone. Prydwen's opinions (ratings, reviews, guide prose) attach to the matching record.

Matching is by name: e7bot's English names are the game's, and Prydwen uses the same names
apart from case, quote characters, and the odd typo, so an exact normalized match is tried
first and a close spelling second. A Prydwen card rating that finds no card on a character e7bot
knows is counted in ``unmatched``, because that means an opinion attached to nothing.

The whole build replaces every merged table in one transaction, so readers see either the old
or the new data, and it is deterministic: running it again on the same sources gives the same
rows.
"""

import difflib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from opentelemetry.metrics import Meter
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import embed, metrics
from .e7bot import SITE
from .posts import split_post
from .stove import BOARDS, POST_URL

log = logging.getLogger(__name__)

# Arbitrary constant shared by every czn-data process; distinct from the migration lock.
BUILD_LOCK = 7_418_023_356

# e7bot's ego type -> Prydwen's element names. Verified by joining both rosters on name.
ELEMENTS = {
    "love": "Passion",
    "creed": "Void",
    "instinct": "Instinct",
    "narcissism": "Justice",
    "norms": "Order",
}
GENDERS = {"GENDER_FEMALE": "female", "GENDER_MALE": "male"}
# Card rarity -> the name color players use for it.
COLORS = {
    "RARITY_COMMON": "White",
    "RARITY_RARE": "Blue",
    "RARITY_UNIQUE": "Purple",
    "RARITY_LEGEND": "Gold",
}
CARD_TYPES = {
    "CARD_ATK": "Attack",
    "CARD_SKILL": "Skill",
    "CARD_POWER": "Upgrade",
    "CARD_ABNORM": "Status",
    "CARD_CURSE": "Curse",
}
# Combatant card id kind (c_<character>_<kind><n>) -> Prydwen's card category.
# "ego" is the character's Ego skill, cast with EP outside the deck. "collapse" cards replace a
# card in the deck when a character's Collapse triggers.
COMBATANT_CATEGORIES = {"srt": "starting", "eps": "ego", "uni": "unique", "col": "collapse"}
OTHER_CATEGORIES = {
    "SORT_NEUTRAL": "neutral",
    "SORT_MONSTER_UNIQUE": "monster",
    "SORT_TABOO": "forbidden",
}
EQUIPMENT_RARITIES = {
    "RARITY_COMMON": "Common",
    "RARITY_RARE": "Rare",
    "RARITY_UNIQUE": "Unique",
    "RARITY_LEGEND": "Legendary",
}
SLOTS = {"EQUIP_WEAPON": "Weapon", "EQUIP_ARMOR": "Armor", "EQUIP_ACC": "Trinket"}
ROMAN = {"rsp1": "I", "rsp2": "II", "rsp3": "III", "rsp4": "IV", "rsp5": "V"}
# Other game version codes (c_<id>_pot, _lbk, _mut2) -> readable labels; a trailing number
# is kept ("mutation 2").
VERSION_LABELS = {
    "pot": "potential upgrade",
    "lbk": "limit break",
    "mut": "mutation",
    "opt": "option",
}
URLS = {
    "character": f"{SITE}czn/combatant",
    "card": f"{SITE}czn/cards",
    "partner": f"{SITE}czn/partner",
    "equipment": f"{SITE}czn/relics",
    "fragment_set": f"{SITE}czn/deck-builder",
    "encounter": f"{SITE}czn/encounters",
    "divine_epiphany": f"{SITE}czn/sparks",
    "leaderboard": f"{SITE}czn/deck-builder",
}

# Merged table -> columns, in insert order. JSON columns are wrapped for psycopg.
COLUMNS = {
    "character": (
        "slug game_id name rarity element class gender image_url updated_text sections"
        " prydwen_url source url data_fetched_at"
    ),
    "card": (
        "character_slug game_id category base_name variant name alias cost type color element"
        " tags text rules seasonal rating epiphany_review target_neutral neutral_review"
        " divine_review image_url prydwen_name prydwen_url source url data_fetched_at"
    ),
    "potential": "character_slug position name text source url data_fetched_at",
    "partner": (
        "game_id name rarity class passive_name passive_text ego_name ego_cost ego_text hp"
        " attack defense image_url prydwen_name prydwen_url source url data_fetched_at"
    ),
    "equipment": (
        "game_id name rarity slot text stats obtained_from image_url prydwen_name prydwen_url"
        " source url data_fetched_at"
    ),
    "fragment_set": (
        "game_id name bonuses image_url prydwen_name prydwen_url source url data_fetched_at"
    ),
    "effect": "name type description source url data_fetched_at",
    "encounter": "id name rarity pack chaos any_chaos reward_classes url data_fetched_at",
    "encounter_option": (
        "encounter_id stage position text requirements rewards reward_classes leads_to"
    ),
    "divine_epiphany": "id gods rarities text card_types cost_min cost_max url data_fetched_at",
    "leaderboard_entry": "season rank score",
    "leaderboard_deck": ("season rank team character_slug cards equipment url data_fetched_at"),
    "post_section": (
        "article_id board post_title posted_at position heading text starts_on ends_on url"
        " data_fetched_at embedding"
    ),
}
JSON_COLUMNS = {"sections", "stats", "bonuses", "rewards", "cards"}

# Event reward type prefix -> reward class, first match wins. Classes are what a player
# planning one deck per run looks for: card copies and removals above all.
REWARD_CLASSES = (
    ("CARD_COPY", "copy"),
    ("CARD_REMOVE", "removal"),
    ("COMBATANT_SPARK", "epiphany"),
    ("ADD_R_SPARK", "divine_epiphany"),
    ("R_SPARK", "epiphany"),
    ("EQUIPMENT_REMOVE", "other"),
    ("EQUIPMENT", "equipment"),
    ("REINFORCE_EQUIPMENT", "equipment"),
    ("CARD_ID_CURSE", "other"),
    ("CARD_", "card"),
)
TEAM_SLOTS = [(team, f"t{team}_c{slot}") for team in (1, 2) for slot in (1, 2, 3)]


class BuildError(Exception):
    """The sources hold a value the build cannot map; the previous merged data stays."""


@dataclass
class BuildResult:
    rows: dict[str, int] = field(default_factory=dict)
    unmatched: Counter = field(default_factory=Counter)  # entity -> Prydwen rows matched to nothing


def normalize(name: str) -> str:
    """Name as compared across sources: quote, comma, zero-width, and spacing variants and case
    ignored (e7bot writes "C’mere, Milk & Pong!", Prydwen "C'mere、Milk & Pong!")."""
    name = name.replace("’", "'").replace("‘", "'").replace("、", ", ").replace("\u200b", "")
    return re.sub(r"\s+", " ", name).strip().casefold()


def plain(markup: str | None) -> str:
    """The game's rich text (``<color_orange>``, ``<cc>``, ``<br>``, keyword links ``$Name$``
    and ``$Name#5$``) as plain text."""
    if not markup:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", markup.replace("\u200b", ""), flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"\$([^$#\n]+)(?:#\d+)?\$", r"\1", text)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(name)).strip("-")


def build(conn: psycopg.Connection) -> BuildResult:
    result = BuildResult()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUILD_LOCK,))
        sources = _Sources(conn)
        tables = _merge(sources, result)
        for table, rows in tables.items():
            _replace(conn, table, rows)
            result.rows[table] = len(rows)
    if result.unmatched:
        log.warning("Prydwen rows matched no e7bot record", extra=dict(result.unmatched))
    log.info("build finished", extra={"rows": result.rows})
    return result


class _Sources:
    """Everything the build reads: e7bot documents and Prydwen rows."""

    def __init__(self, conn: psycopg.Connection):
        self.documents: dict[str, tuple[Any, datetime]] = {
            path: (body, fetched_at)
            for path, body, fetched_at in conn.execute(
                "SELECT path, body, fetched_at FROM e7bot.document"
            )
        }
        self.saves = [row[0] for row in conn.execute("SELECT body FROM e7bot.save")]
        cursor = conn.cursor(row_factory=dict_row)
        self.prydwen: dict[str, list[dict]] = {}
        for table in (
            "character",
            "card",
            "potential",
            "partner",
            "equipment",
            "fragment_set",
            "effect",
        ):
            self.prydwen[table] = cursor.execute(
                f"""
                SELECT t.*, p.data_fetched_at FROM prydwen.{table} t
                JOIN prydwen.page p USING (url) ORDER BY t.url, t.ctid
                """
            ).fetchall()
        self.genders = dict(
            conn.execute("SELECT image_url, gender FROM prydwen.portrait_gender").fetchall()
        )
        self.articles = cursor.execute(
            "SELECT article_id, board, title, content, created_at, fetched_at"
            " FROM stove.article ORDER BY article_id"
        ).fetchall()
        # Embedding every section takes about a minute; unchanged sections keep their vector.
        self.embeddings = {
            embed.passage_text(title, heading, text): vector
            for title, heading, text, vector in conn.execute(
                "SELECT post_title, heading, text, embedding FROM post_section"
                " WHERE embedding IS NOT NULL"
            )
        }

    def items(self, path: str) -> tuple[list, datetime | None]:
        body, fetched_at = self.documents.get(path, ([], None))
        return body or [], fetched_at

    def detail(self, path: str) -> tuple[dict | None, datetime | None]:
        return self.documents.get(path, (None, None))


def _merge(sources: _Sources, result: BuildResult) -> dict[str, list[dict]]:
    characters, slugs = _characters(sources)
    known = {c["slug"] for c in characters if c["source"] == "e7bot"}
    cards = _cards(sources, slugs, characters, known, result)
    encounters, options = _encounters(sources)
    entries, decks = _leaderboard(sources, slugs)
    return {
        "encounter": encounters,
        "encounter_option": options,
        "divine_epiphany": _divine_epiphanies(sources),
        "leaderboard_entry": entries,
        "leaderboard_deck": decks,
        "post_section": _post_sections(sources),
        "character": characters,
        "card": cards,
        "potential": _potentials(sources, slugs),
        "partner": _named(
            _e7_partners(sources),
            _prydwen_rows(sources, "partner"),
            "partner",
            ("hp", "attack", "defense", "image_url"),
            result,
        ),
        "equipment": _named(
            _e7_equipment(sources),
            _prydwen_rows(sources, "equipment"),
            "equipment",
            ("stats", "obtained_from", "image_url"),
            result,
        ),
        "fragment_set": _named(
            _e7_fragment_sets(sources),
            _prydwen_rows(sources, "fragment_set"),
            "fragment_set",
            ("image_url",),
            result,
        ),
        "effect": _named(
            _e7_effects(sources),
            _prydwen_rows(sources, "effect"),
            "effect",
            ("type",),
            result,
            prydwen_link=False,
        ),
    }


def _post_sections(sources: _Sources) -> list[dict]:
    rows = []
    for article in sources.articles:
        post = {
            "article_id": article["article_id"],
            "board": BOARDS.get(article["board"], str(article["board"])),
            "post_title": article["title"],
            "posted_at": article["created_at"],
            "url": POST_URL.format(article["article_id"]),
            "data_fetched_at": article["fetched_at"],
        }
        for section in split_post(article["content"], article["created_at"]):
            rows.append(post | vars(section))
    texts = [embed.passage_text(r["post_title"], r["heading"], r["text"]) for r in rows]
    missing = [text for text in dict.fromkeys(texts) if text not in sources.embeddings]
    vectors = sources.embeddings | dict(zip(missing, embed.passages(missing), strict=True))
    for row, text in zip(rows, texts, strict=True):
        row["embedding"] = vectors[text]
    return rows


def _prydwen_rows(sources: _Sources, table: str) -> list[dict]:
    rows = []
    for row in sources.prydwen[table]:
        row = dict(row)
        row["prydwen_url"] = row["url"]
        if table == "equipment":
            row["obtained_from"] = row.pop("source")
        rows.append(row | {"source": "prydwen"})
    return rows


def _characters(sources: _Sources) -> tuple[list[dict], dict[str, str]]:
    """Merged characters, and e7bot character id -> merged slug."""
    roster, fetched_at = sources.items("czn/characters/list")
    prydwen = {normalize(c["name"]): c for c in sources.prydwen["character"]}
    rows, slugs = [], {}
    for entry in roster:
        if entry["char_type"] != "CHAR_COMBATANT":
            continue
        name = (entry.get("names") or {}).get("en") or entry["name"]
        if entry["ego"] not in ELEMENTS:
            raise BuildError(f"character {name}: unknown ego type {entry['ego']!r}")
        found = prydwen.pop(normalize(name), None) or {}
        image_url = found.get("image_url")
        slug = found.get("slug") or slugify(name)
        slugs[str(entry["id"])] = slug
        rows.append(
            {
                "slug": slug,
                "game_id": str(entry["id"]),
                "name": name,
                "rarity": entry["rarity"],
                "element": ELEMENTS[entry["ego"]],
                "class": entry["job"].title(),
                "gender": GENDERS.get(entry["gender_type"]) or sources.genders.get(image_url),
                "image_url": image_url,
                "updated_text": found.get("updated_text"),
                "sections": found.get("sections") or [],
                "prydwen_url": found.get("url"),
                "source": "e7bot",
                "url": URLS["character"],
                "data_fetched_at": fetched_at,
            }
        )
    for found in prydwen.values():
        rows.append(
            {
                key: found[key]
                for key in (
                    "slug",
                    "name",
                    "rarity",
                    "element",
                    "class",
                    "image_url",
                    "updated_text",
                    "sections",
                    "url",
                    "data_fetched_at",
                )
            }
            | {
                "game_id": None,
                "gender": sources.genders.get(found["image_url"]),
                "prydwen_url": found["url"],
                "source": "prydwen",
            }
        )
    return rows, slugs


_COMBATANT_CARD = re.compile(r"^c_\d+_([a-z]+)")
# Prydwen card fields kept on a matched e7bot card: opinions, plus facts e7bot lacks.
_PRYDWEN_CARD_FIELDS = (
    "alias",
    "rules",
    "seasonal",
    "rating",
    "epiphany_review",
    "target_neutral",
    "neutral_review",
    "divine_review",
    "image_url",
)


def _cards(sources, slugs, characters, known, result) -> list[dict]:
    listed, fetched_at = sources.items("czn/cards/list")
    # Some game names carry zero-width spaces ("Negotiate\u200b"); Prydwen's never do.
    listed = [card | {"name": plain(card["name"])} for card in listed]
    elements = {c["slug"]: c["element"] for c in characters}
    by_id = {card["id"]: card for card in listed}
    nested = {version["id"] for card in listed for version in card.get("r_spark_cards") or []}
    versions: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    bases = []
    for card in listed:
        if card["id"] in nested or card["sort"] == "SORT_SUPPORT":
            continue  # Epiphany versions come with their base; partner EGO cards are partners'
        base_id = _base_of(card["id"], by_id)
        if base_id:
            versions[base_id].append((card["id"].removeprefix(f"{base_id}_"), card))
        else:
            bases.append(card)

    prydwen = defaultdict(list)
    for row in sources.prydwen["card"]:
        key = (row["character_slug"], normalize(row["base_name"]), row["variant"])
        prydwen[key].append(row)
    guide_names = _guide_names(bases, slugs, prydwen)

    rows = []
    for base in bases:
        combatant = base.get("base_combatant") or {}
        slug = slugs.get(str(combatant.get("id"))) if combatant else None
        guide_name = guide_names.get(base["id"], normalize(base["name"]))
        guide_rows = prydwen.get((slug, guide_name, "")) or [{}]
        prydwen_name = guide_rows[0].get("base_name")
        if prydwen_name and normalize(prydwen_name) == normalize(base["name"]):
            prydwen_name = None
        kind = _COMBATANT_CARD.match(base["id"])
        if combatant:
            derived_category = COMBATANT_CATEGORIES.get(kind.group(1) if kind else "", "generated")
        else:
            derived_category = OTHER_CATEGORIES.get(
                base["sort"], base["sort"].removeprefix("SORT_").lower()
            )
        family = [("", base)]
        family += [
            (version["id"].removeprefix(f"{base['id']}_"), version)
            for version in base.get("r_spark_cards") or []
        ]
        family += versions.get(base["id"], [])
        base_found = None
        for code, card in family:
            variant = ROMAN.get(code) or _version_label(code)
            matches = prydwen.get((slug, guide_name, variant))
            found = matches.pop(0) if matches else None
            if code == "":
                # A card the character also creates is listed twice on Prydwen (her own card
                # and the created copy), with the review on either listing.
                while matches:
                    extra = matches.pop(0)
                    found |= {k: extra.get(k) for k in _PRYDWEN_CARD_FIELDS if not found.get(k)}
                base_found = found
            category = derived_category
            if derived_category != "ego":  # Prydwen files Ego skills under starting cards
                category = (found or base_found or {}).get("category") or derived_category
            cost = card["cost"]
            row = {
                "_guide_name": guide_name,
                "character_slug": slug,
                "game_id": card["id"],
                "category": category,
                "base_name": base["name"],
                "variant": variant,
                "name": base["name"]
                if not code
                else f"{base['name']} {variant}"
                if code in ROMAN
                else f"{base['name']} ({variant})",
                "prydwen_name": prydwen_name,
                "cost": str(cost) if cost >= 0 else (found or {}).get("cost") or "X",
                "type": CARD_TYPES.get(card["card_category"], card["card_category"]),
                "color": COLORS.get(card["rarity"]),
                "element": elements.get(slug) or (found or {}).get("element"),
                "tags": [t["title"] for t in card.get("talent") or []],
                "text": plain(card["desc_display"]),
                "prydwen_url": (found or {}).get("url"),
                "source": "e7bot",
                "url": URLS["card"],
                "data_fetched_at": fetched_at,
            }
            row |= {key: (found or {}).get(key) for key in _PRYDWEN_CARD_FIELDS}
            rows.append(row)

    _match_by_text(rows, prydwen, known)

    # The game repeats some cards under several ids (c_1004_col, c_1004_col2) with the same
    # name and effect; to a player they are one card. The rated copy is kept.
    def identity(row: dict) -> tuple:
        return tuple(row[k] for k in ("character_slug", "name", "cost", "type", "text"))

    unique: dict[tuple, dict] = {}
    for row in sorted(rows, key=lambda r: r["rating"] is None):
        unique.setdefault(identity(row), row)
    rows = [row for row in rows if unique[identity(row)] is row]
    e7bot_has_neutrals = any(not card.get("base_combatant") for card in listed)
    for leftovers in prydwen.values():
        for found in leftovers:
            # Only a rating or review matters here; unrated generated cards that e7bot names
            # differently ("Hew Lv. 2") lose nothing by standing alone.
            opinion = found.get("rating") or found.get("epiphany_review")
            if opinion and (
                found["character_slug"] in known
                or (found["character_slug"] is None and e7bot_has_neutrals)
            ):
                result.unmatched["card"] += 1
            rows.append(
                {key: found.get(key) for key in COLUMNS["card"].split()}
                | {"game_id": None, "prydwen_url": found["url"], "source": "prydwen"}
            )
    return rows


def _version_label(code: str) -> str:
    match = re.fullmatch(r"([a-z]+?)(\d*)", code)
    label = VERSION_LABELS.get(match.group(1)) if match else None
    return f"{label} {match.group(2)}".strip() if label else code


def _text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def _best_pairs(pairs: list[tuple[float, Any, Any]], threshold: float):
    """Each left and right item at most once, highest score first: unlike picking each left
    item's best in turn, an early weak pick cannot take a later item's exact match."""
    used_left, used_right = set(), set()
    for score, left, right in sorted(pairs, key=lambda p: -p[0]):
        if score < threshold or id(left) in used_left or id(right) in used_right:
            continue
        used_left.add(id(left))
        used_right.add(id(right))
        yield left, right


def _guide_names(bases: list[dict], slugs: dict, prydwen: dict) -> dict[str, str]:
    """e7bot base card id -> the normalized Prydwen base name of the same card, for cards
    Prydwen names differently: a close spelling ("Soul Rip" for "Soul Riff"), else the same
    character's Prydwen card whose base text is nearly the same ("Rapid Fire (Luke)" for
    "Chain Fire", "Tracer" for "Tracer Shot"). Its Epiphany versions then match by numeral."""
    unmatched = defaultdict(set)  # character slug -> Prydwen base names no e7bot name matched
    for slug, base_name, variant in prydwen:
        if slug and variant == "":
            unmatched[slug].add(base_name)
    pending = []
    for base in bases:
        combatant = base.get("base_combatant") or {}
        slug = slugs.get(str(combatant.get("id"))) if combatant else None
        if slug and normalize(base["name"]) in unmatched[slug]:
            unmatched[slug].discard(normalize(base["name"]))
        elif slug:
            pending.append((slug, base))
    names, by_text = {}, []
    for slug, base in pending:
        close = difflib.get_close_matches(normalize(base["name"]), unmatched[slug], 1, 0.8)
        if close:
            names[base["id"]] = close[0]
            unmatched[slug].discard(close[0])
            continue
        text = plain(base["desc_display"])
        by_text += [
            (_text_similarity(text, prydwen[(slug, name, "")][0]["text"]), base, (slug, name))
            for name in unmatched[slug]
        ]
    for base, (_, name) in _best_pairs(by_text, 0.8):
        names[base["id"]] = name
    return names


def _match_by_text(rows: list[dict], prydwen: dict, known: set[str]) -> None:
    """Attach each rated Prydwen card version that matched nothing by name or numeral to the
    unrated version of the same e7bot card with the closest effect text. This covers versions
    Prydwen names instead of numbering ("Hew (Extreme)" for Hew II)."""
    candidates = defaultdict(list)  # (character slug, guide base name) -> unrated Epiphanies
    epiphanies = set(ROMAN.values())
    for row in rows:
        if (
            row["character_slug"] in known
            and row["rating"] is None
            and row["variant"] in epiphanies
        ):
            candidates[(row["character_slug"], row["_guide_name"])].append(row)
    pairs = [
        (_text_similarity(found["text"], row["text"]), (key, found), row)
        for key, leftovers in prydwen.items()
        for found in leftovers
        if found.get("rating") and found["character_slug"] in known
        for row in candidates[(found["character_slug"], normalize(found["base_name"]))]
    ]
    for (key, found), row in _best_pairs(pairs, 0.8):
        row |= {field: found.get(field) for field in _PRYDWEN_CARD_FIELDS}
        row["prydwen_url"] = found["url"]
        prydwen[key] = [row for row in prydwen[key] if row is not found]


def _base_of(card_id: str, by_id: dict) -> str | None:
    """The card a listed id is a version of (``c_1_uni4_mut1`` -> ``c_1_uni4``), if any."""
    parts = card_id.split("_")
    for end in range(len(parts) - 1, 1, -1):
        prefix = "_".join(parts[:end])
        if prefix in by_id:
            return prefix
    return None


def _potentials(sources: _Sources, slugs: dict[str, str]) -> list[dict]:
    rows, covered = [], set()
    for game_id, slug in slugs.items():
        detail, fetched_at = sources.detail(f"czn/characters/{game_id}")
        if not detail or not detail.get("potential_nodes"):
            continue
        covered.add(slug)
        for node in detail["potential_nodes"]:
            major, _, minor = str(node["node_num"]).partition("_")
            text = plain(node["desc"])
            upgrades = node.get("upgrade_nodes") or []
            if upgrades:
                text += f"\nAt max level ({upgrades[-1]['node_level']}): "
                text += plain(upgrades[-1]["desc"])
            rows.append(
                {
                    "character_slug": slug,
                    "position": major if minor in ("", "0") else f"{major}-{minor}",
                    "name": node["potential_node_type_define"]["sub_title"],
                    "text": text,
                    "source": "e7bot",
                    "url": URLS["character"],
                    "data_fetched_at": fetched_at,
                }
            )
    for row in sources.prydwen["potential"]:
        if row["character_slug"] not in covered:
            rows.append(
                {key: row[key] for key in COLUMNS["potential"].split() if key in row}
                | {"source": "prydwen"}
            )
    return rows


def _e7_partners(sources: _Sources) -> list[dict]:
    partners, fetched_at = sources.items("czn/characters/partner/list/detail")
    rows = []
    for entry in partners:
        card = entry.get("partner_card") or {}
        passives = entry.get("partner_passives") or []
        rows.append(
            {
                "game_id": str(entry["id"]),
                "name": (entry.get("names") or {}).get("en") or entry["name"],
                "rarity": entry["rarity"],
                "class": entry["job"].title(),
                "passive_name": passives[0]["name"] if passives else None,
                "passive_text": "\n".join(plain(p["description"]) for p in passives),
                "ego_name": card.get("name"),
                "ego_cost": card.get("cost"),
                "ego_text": plain(card.get("desc_display")) or None,
                "source": "e7bot",
                "url": URLS["partner"],
                "data_fetched_at": fetched_at,
            }
        )
    return rows


def _e7_equipment(sources: _Sources) -> list[dict]:
    """Equipment by name; the game repeats some items under several ids (arena copies) with
    the same name and effect, which are one item to a player."""
    relics, fetched_at = sources.items("czn/relics/list")
    rows = [
        {
            "game_id": relic["id"],
            "name": (relic.get("names") or {}).get("en") or relic["name"],
            "rarity": EQUIPMENT_RARITIES.get(relic["rarity"], relic["rarity"]),
            "slot": SLOTS[relic["relic_type"]],
            "text": "\n".join(plain(skill["description"]) for skill in relic["skills"]),
            "stats": {},
            "source": "e7bot",
            "url": URLS["equipment"],
            "data_fetched_at": fetched_at,
        }
        for relic in relics
        if relic["relic_category"] == "EQUIPMENT"
        and relic["relic_type"] in SLOTS
        and "_test_" not in relic["id"]  # developer benchmark items, never obtainable
    ]
    unique = {}
    for row in rows:
        unique.setdefault((row["name"], row["slot"], row["rarity"], row["text"]), row)
    return list(unique.values())


def _e7_fragment_sets(sources: _Sources) -> list[dict]:
    sets, fetched_at = sources.items("czn/piece-set-options/list")
    return [
        {
            "game_id": entry["id"],
            "name": entry["name"],
            "bonuses": [
                {"pieces": pieces, "text": plain(entry[f"set{pieces}"]["description"])}
                for pieces in (2, 4, 6)
                if entry.get(f"set{pieces}")
            ],
            "source": "e7bot",
            "url": URLS["fragment_set"],
            "data_fetched_at": fetched_at,
        }
        for entry in sets
    ]


def _e7_effects(sources: _Sources) -> list[dict]:
    """Keyword and tag definitions from the tooltips on every card."""
    cards, fetched_at = sources.items("czn/cards/list")
    effects: dict[str, dict] = {}
    for card in cards:
        for kind, entries in (("Tag", card.get("talent")), (None, card.get("tooltips"))):
            for entry in entries or []:
                name = plain(entry.get("title"))
                if name and normalize(name) not in effects:
                    effects[normalize(name)] = {
                        "name": name,
                        "type": kind,
                        "description": plain(entry.get("desc")),
                        "source": "e7bot",
                        "url": URLS["card"],
                        "data_fetched_at": fetched_at,
                    }
    return list(effects.values())


def reward_class(reward_type: str) -> str:
    return next((c for prefix, c in REWARD_CLASSES if reward_type.startswith(prefix)), "other")


def _encounters(sources: _Sources) -> tuple[list[dict], list[dict]]:
    """One encounter per event: e7bot lists each stage of an event (``uk_904_s01_01a``,
    ``_01b``) as its own entry, sharing ``link_encounter_set_id``; stages are numbered in id
    order and answers point at the stage number they open."""
    listed, _ = sources.items("czn/encounters/list")
    packs = {pack["id"]: pack for pack in sources.items("czn/encounters/packs/list")[0]}
    events: dict[str, list[dict]] = defaultdict(list)
    for entry in sorted(listed, key=lambda e: e["id"]):
        events[entry.get("link_encounter_set_id") or entry["id"]].append(entry)
    encounters, options = [], []
    for event_id, stages in events.items():
        numbers = {entry["id"]: number for number, entry in enumerate(stages, 1)}
        first = stages[0]
        pack = packs.get(first["link_encounter_pack_id"]) or {}
        chaos = {pack["name"]} if pack.get("pack_type") == "PACK_PLANET" else set()
        classes: set[str] = set()
        fetched = []
        for entry in stages:
            collection = entry.get("chaos_collection_info") or []
            if isinstance(collection, dict):  # one Chaos comes as an object, several as a list
                collection = [collection]
            chaos |= {
                info["chaos"]["title"]
                for info in collection
                if isinstance(info, dict) and (info.get("chaos") or {}).get("title")
            }
            detail, detail_fetched_at = sources.detail(f"czn/encounters/{entry['id']}")
            fetched.append(detail_fetched_at)
            stage_options = _encounter_options(event_id, numbers, entry, detail)
            for option in stage_options:
                classes |= set(option["reward_classes"])
            options += stage_options
        encounters.append(
            {
                "id": event_id,
                "name": plain(first["name"]),
                "rarity": (first.get("rarity") or "").title() or None,
                "pack": pack.get("name"),
                "chaos": sorted(chaos),
                "any_chaos": pack.get("pack_type") == "PACK_FACTION"
                and pack.get("link_faction_id") == "common",
                "reward_classes": sorted(classes),
                "url": URLS["encounter"],
                "data_fetched_at": min((f for f in fetched if f), default=None),
            }
        )
    return encounters, options


def _encounter_options(event_id: str, numbers: dict, entry: dict, detail) -> list[dict]:
    options, seen = [], set()
    for option in (detail or {}).get("options") or []:
        rewards = [
            {
                "outcome": outcome,
                "class": reward_class(reward["reward_type"]),
                "type": reward["reward_type"],
                "text": plain(reward.get("desc_option")),
            }
            for outcome, effect in (
                ("success", option.get("success_link_encounter_option_eff")),
                ("failure", option.get("fail_link_encounter_option_eff")),
            )
            for slot in range(10)
            if (reward := (effect or {}).get(f"reward{slot}_reward"))
        ]
        success = option.get("success_link_encounter_option_eff") or {}
        text = plain(option.get("simple_flavor_text"))
        if not text:  # answers that start a battle carry no text in the game data
            text = "Fight" if option.get("option_type") == "ATTACK" else "(no text)"
        row = {
            "encounter_id": event_id,
            "stage": numbers[entry["id"]],
            "position": len(options) + 1,
            "text": text,
            "requirements": [
                plain(c.get("encounter_option_tooltip"))
                for c in option.get("select_link_nb_conditions") or []
                if c.get("encounter_option_tooltip")
            ],
            "rewards": rewards,
            "reward_classes": sorted({r["class"] for r in rewards} - {"other"}),
            "leads_to": numbers.get(success.get("next_encounter_id")),
        }
        # The game lists some answers twice with identical effects.
        identity = json.dumps({k: v for k, v in row.items() if k != "position"}, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            options.append(row)
    return options


def _divine_epiphanies(sources: _Sources) -> list[dict]:
    """One row per distinct effect and target; the game repeats each per god and rarity."""
    sparks, fetched_at = sources.items("czn/r-sparks/list")
    effects: dict[tuple, dict] = {}
    for spark in sorted(sparks, key=lambda s: s["id"]):
        card_types = [CARD_TYPES.get(c, c) for c in spark.get("target_card_category") or []]
        cost_min, cost_max = spark.get("target_card_cost_min"), spark.get("target_card_cost_max")
        if cost_min is not None:  # the game writes "any cost" as both -1 and 0
            cost_min = max(cost_min, 0)
        text = plain(spark["description"])
        effect = effects.setdefault(
            (text, tuple(card_types), cost_min, cost_max),
            {
                "id": spark["id"],
                "gods": [],
                "rarities": [],
                "text": text,
                "card_types": card_types,
                "cost_min": cost_min,
                "cost_max": cost_max,
                "url": URLS["divine_epiphany"],
                "data_fetched_at": fetched_at,
            },
        )
        god = (spark.get("god_type") or "").title()
        rarity = spark["rarity"].removeprefix("R_SPARK_RARITY_").title()
        if god and god not in effect["gods"]:
            effect["gods"].append(god)
        if rarity not in effect["rarities"]:
            effect["rarities"].append(rarity)
    return list(effects.values())


def _leaderboard(sources: _Sources, slugs: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Leaderboard ranks per season and each ranked character's saved deck."""
    rankings, fetched_at = sources.items("czn/rankings/list")
    cards = {card["id"]: card for card in sources.items("czn/cards/list")[0]}
    for card in list(cards.values()):
        for version in card.get("r_spark_cards") or []:
            cards.setdefault(version["id"], version)
    equipment = {
        relic["id"]: (relic.get("names") or {}).get("en") or relic["name"]
        for relic in sources.items("czn/relics/list")[0]
    }
    sparks = {s["id"]: plain(s["description"]) for s in sources.items("czn/r-sparks/list")[0]}
    saves = {str(save["id"]): save for save in sources.saves if "id" in save}
    seasons = defaultdict(list)
    for entry in rankings:
        seasons[entry["season_id"]].append(entry)
    entries, decks = [], []
    for season, ranked in seasons.items():
        for rank, entry in enumerate(sorted(ranked, key=lambda e: -e["score"]), 1):
            entries.append({"season": season, "rank": rank, "score": entry["score"]})
            for team, slot in TEAM_SLOTS:
                save = saves.get(str(entry.get(f"{slot}_save_id")))
                slug = slugs.get(str(entry.get(f"{slot}_id")))
                if not save or not slug:
                    continue
                decks.append(
                    {
                        "season": season,
                        "rank": rank,
                        "team": team,
                        "character_slug": slug,
                        "cards": [_deck_card(c, cards, sparks) for c in save["card_datas"]],
                        "equipment": [
                            equipment.get(e["res_id"], e["res_id"])
                            for e in save.get("equip_datas") or []
                        ],
                        "url": URLS["leaderboard"],
                        "data_fetched_at": fetched_at,
                    }
                )
    return entries, decks


def _deck_card(saved: dict, cards: dict, sparks: dict) -> dict:
    card = cards.get(saved["res_id"]) or {}
    epiphany = saved.get("r_spark_res_id") or ""
    variant = ""
    if saved.get("r_spark_category") == "CATEGORY_UNIQUE":
        variant = ROMAN.get(epiphany.split("_", 1)[0], "")
    return {
        "base": plain(card.get("name")) or saved["res_id"],
        "variant": variant,
        "divine_epiphany": sparks.get(saved.get("r_spark_god") or ""),
    }


def _named(e7_rows, prydwen_rows, entity, fill, result, prydwen_link=True) -> list[dict]:
    """Merge one name-keyed entity: e7bot rows first, each completed from the Prydwen row of
    the same (or a very close) name, else of nearly the same effect text (Prydwen renames some
    equipment: "Tentacles of Chaos" for "Tentacles of a Ruined Age"); unmatched Prydwen rows
    are kept as they are."""
    unused: dict[str, list[dict]] = defaultdict(list)
    for row in prydwen_rows:
        unused[normalize(row["name"])].append(row)

    def attach(row: dict, found: dict) -> None:
        row |= {k: found.get(k) for k in fill if row.get(k) in (None, {}, [])}
        if prydwen_link:
            row |= {"prydwen_url": found["url"], "prydwen_name": found["name"]}

    unnamed = []
    for row in e7_rows:
        key = normalize(row["name"])
        if not unused.get(key):
            close = difflib.get_close_matches(key, [k for k, v in unused.items() if v], 1, 0.9)
            key = close[0] if close else key
        if unused.get(key):
            attach(row, unused[key].pop(0))
        else:
            unnamed.append(row)
    pairs = [
        (_text_similarity(row["text"], found["text"]), row, found)
        for row in unnamed
        if row.get("text")
        for rows in unused.values()
        for found in rows
        if found.get("text") and found.get("slot") == row.get("slot")
    ]
    for row, found in _best_pairs(pairs, 0.9):
        attach(row, found)
        key = normalize(found["name"])
        unused[key] = [row for row in unused[key] if row is not found]
    merged = list(e7_rows)
    leftovers = [row for rows in unused.values() for row in rows]
    if e7_rows and leftovers:
        result.unmatched[entity] += len(leftovers)
    return merged + leftovers


def export_metrics(result: BuildResult | None, started: float) -> bool:
    """Send the build's metrics once; ``result`` is None when the build failed."""

    def record(meter: Meter) -> None:
        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.build.success", "1 when the merged tables were rebuilt").set(
            int(result is not None)
        )
        gauge("czn.build.last_run.timestamp", "End of the build", "s").set(time.time())
        if result is None:
            return
        unmatched = gauge("czn.build.unmatched", "Prydwen rows that matched no e7bot record")
        for entity in COLUMNS:
            unmatched.set(result.unmatched[entity], {"entity": entity})
        rows = gauge("czn.data.rows", "Rows stored per merged table")
        for table, count in result.rows.items():
            rows.set(count, {"table": table})

    return metrics.export("czn_data.build", record)


def _replace(conn: psycopg.Connection, table: str, rows: list[dict]) -> None:
    columns = COLUMNS[table].split()
    conn.execute(f"DELETE FROM {table}")
    if not rows:
        return
    names = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cursor:
        cursor.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            [
                [Jsonb(row.get(c)) if c in JSON_COLUMNS else row.get(c) for c in columns]
                for row in rows
            ],
        )
