"""MCP server that answers Chaos Zero Nightmare questions from the scraped database.

It only reads; give it a database role with SELECT rights. Tools read the merged tables that
``czn_data.build`` derives, never a source schema directly.

MCP tools serve Claude only: they reply in plain text (``czn_data.render``) with facts only,
leaving out the fields only programs use and anything naming where data was scraped
(INTERNAL_FIELDS); guide opinions are labeled (OPINION_FIELDS). No tool takes or returns a URL.
Programs, such as the companion app, use the REST routes, which reply in JSON.

Auth: the server is an OAuth 2.1 resource server. It validates JWT access tokens issued by an
OIDC provider (``OIDC_ISSUER``) and advertises that provider to MCP clients such as claude.ai,
which run the login flow themselves.
"""

import functools
import json
import logging
import os
import re
import time
import urllib.request
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Literal

import anyio
import jwt
import psycopg
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from PIL import UnidentifiedImageError
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import gear, identity, search
from .e7bot import ASSETS, GEAR_BACKGROUND
from .gamefiles import TEXT_KINDS
from .gear import MODEL as GEAR_MODEL
from .gear import GearIdentifier
from .identity import MODEL as IDENTITY_MODEL
from .identity import Identifier
from .render import render

log = logging.getLogger(__name__)

IDENTIFY_MAX_BYTES = 10 * 1024 * 1024
LOG_ARGUMENT_CHARS = 500
# Fields tools leave out of their replies: build bookkeeping (source: which scraper a merged row
# came from), ids and slugs that only join tables, and anything naming where data was scraped:
# page and art URLs, fetch times, and a site's spelling of a name. Where data comes from is the
# scrapers' business; Claude gets facts, with guide opinions labeled (OPINION_FIELDS).
INTERNAL_FIELDS = frozenset(
    {
        "source",
        "game_id",
        "slug",
        "character_slug",
        "image_url",
        "prydwen_name",
        "url",
        "prydwen_url",
        "data_fetched_at",
    }
)
# Fields holding a guide's opinion rather than game data, renamed so every reply labels them.
OPINION_FIELDS = {
    "rating": "guide_rating",
    "review": "guide_review",
    "epiphany_review": "guide_review",
    "neutral_review": "guide_neutral_review",
    "divine_review": "guide_divine_review",
    "target_neutral": "guide_neutral_pick",
    "tier": "guide_tier",
    "performance": "guide_performance",
}
# A card's long guide text, which lookup adds only with detail.
GUIDE_ADVICE = frozenset({"epiphany_review", "neutral_review", "divine_review", "target_neutral"})
# Deck notes end with a disclaimer and the Team Database links that every deck repeats.
DECK_NOTES = r"regexp_replace(notes, '\s*Note: This is not an exhaustive list.*$', '', 's')"

LOOKUP_SQL = """
SELECT kind, name, summary, ref FROM (
SELECT entity.*, d.holds, d.described,
       bool_or(d.holds OR d.described) OVER () AS any_close,
       row_number() OVER (ORDER BY {match} DESC, {order}, name) AS rank
FROM (
    -- ref: the row's own name in its table, for reading its details.
    SELECT 'character' AS kind, name, concat_ws(' ', rarity || '*', element, class) AS summary,
           name AS ref FROM character
    UNION ALL
    -- A version's game name joins its card name, so "Flicker Blade (Unbound)" finds V first.
    SELECT 'card', concat_ws(' ', name, '(' || nullif(alias, '') || ')'),
           concat_ws(' | ', color, type, 'cost ' || cost, text), name FROM card
    UNION ALL
    SELECT 'equipment', name, concat_ws(' | ', rarity, slot, text), name FROM equipment
    UNION ALL
    SELECT 'fragment_set', name,
           (SELECT string_agg((b->>'pieces') || '-piece: ' || (b->>'text'), ' | ')
            FROM jsonb_array_elements(bonuses) b), name
    FROM fragment_set
    UNION ALL
    SELECT 'partner', name, concat_ws(' | ', rarity || '*', class, passive_text,
           'EGO ' || ego_name || ' (' || ego_cost || ' EP): ' || ego_text), name FROM partner
    UNION ALL
    SELECT 'effect', name, concat_ws(' | ', type, description), name FROM effect
    UNION ALL
    SELECT 'encounter', name, concat_ws(' | ', pack, nullif(array_to_string(chaos, ', '), ''),
           CASE WHEN any_chaos THEN 'any Chaos' END,
           'gives ' || nullif(array_to_string(reward_classes, ', '), '')), name
    FROM encounter
    UNION ALL
    SELECT 'divine_epiphany', text,
           concat_ws(' | ', {divine_applies}, nullif(array_to_string(gods, ', '), '')), text
    FROM divine_epiphany
    UNION ALL
    SELECT 'chaos', name, 'Chaos (run map); list and get_encounter take this name as chaos',
           name FROM chaos
) entity
, LATERAL (
    SELECT entity.kind = 'effect'
           AND to_tsvector('english', summary) @@ plainto_tsquery('english', %(name)s)
           AS described,
           name ILIKE '%%' || %(name)s || '%%' AS holds
) d
WHERE (%(kind)s::text IS NULL OR entity.kind = %(kind)s)
  AND ({match} OR d.described)
) found
-- Fuzzy spelling matches only stand in for a typo: when a name holds the query or an effect's
-- description uses it, a loose match ("Ionization" for "Polarization") is noise.
WHERE holds OR described OR NOT any_close
-- Names holding the query first, then effects whose description uses it.
ORDER BY holds DESC, described DESC, rank
LIMIT %(limit)s
"""
# Which cards a Divine Epiphany applies to, as text: "Attack, Skill | cost 1".
DIVINE_APPLIES = """concat_ws(' | ',
    coalesce(nullif(array_to_string(card_types, ', '), ''), 'any card type'),
    CASE WHEN cost_min <= 0 AND cost_max >= 99 THEN 'any cost'
         WHEN cost_max >= 99 THEN 'cost ' || cost_min || '+'
         WHEN cost_min = cost_max THEN 'cost ' || cost_min
         ELSE 'cost ' || cost_min || '-' || cost_max END)"""
# Other words for effect: the dictionary holds keywords, statuses, and card tags.
LOOKUP_KIND_ALIASES = {"keyword": "effect", "status": "effect", "tag": "effect"}
LookupKind = Literal[
    "character",
    "card",
    "equipment",
    "fragment_set",
    "partner",
    "effect",
    "keyword",
    "status",
    "tag",
    "encounter",
    "divine_epiphany",
    "chaos",
]
SearchKind = Literal["rules", "announcements", "guides"]
SEARCH_KINDS: tuple[SearchKind, ...] = ("rules", "announcements", "guides")
# Matches search lists one line each per kind after the best, and refs one read call takes;
# search's description states both.
SEARCH_MORE = 4
SEARCH_READ = 10
# Passage sources whose published_at is a date worth showing: when the post or page last
# changed. The game's own text carries its fetch time, and guide pages carry none.
DATED_SOURCES = ("post", "game8")
# What search shows of an announcement section.
POST_FIELDS = (
    "post_title AS title, 'announcement' AS kind, board, posted_at, heading, text, starts_on,"
    " ends_on"
)
ListKind = Literal["cards", "equipment", "characters", "events", "divine_epiphanies", "banners"]
Reward = Literal["copy", "removal", "epiphany", "divine_epiphany", "equipment", "card"]
# The filters list takes for each kind.
LIST_FILTERS: dict[str, set[str]] = {
    "cards": {"character", "category", "type", "tag", "color", "base_only"},
    "equipment": {"slot", "rarity", "name", "chaos", "offset"},
    "characters": {"gender", "element", "character_class", "rarity"},
    "events": {"chaos", "reward", "offset"},
    "divine_epiphanies": {"type", "cost", "god", "offset"},
    "banners": set(),
}


# Fuzzy name match: substring, pg_trgm word similarity ("flicker blad" finds "Flicker Blade
# I"), or trigram similarity ignoring spaces ("Meilin" finds "Mei Lin", "Blazbell" finds
# "Blazebell"). Shared by every tool that resolves a name typed from a screenshot; NAME_ORDER
# ranks the matches, best first.
def _name_match(column: str) -> str:
    squashed = f"similarity(replace({column}, ' ', ''), replace(%(name)s, ' ', ''))"
    return (
        f"({column} ILIKE '%%' || %(name)s || '%%' OR %(name)s <%% {column} OR {squashed} >= 0.3)"
    )


_SQUASHED = "similarity(replace(name, ' ', ''), replace(%(name)s, ' ', ''))"
NAME_MATCH = _name_match("name")
NAME_ORDER = (
    f"(name ILIKE %(name)s) DESC, word_similarity(%(name)s, name) DESC, {_SQUASHED} DESC,"
    " similarity(name, %(name)s) DESC"
)
LOOKUP_SQL = LOOKUP_SQL.format(match=NAME_MATCH, order=NAME_ORDER, divine_applies=DIVINE_APPLIES)
# /deck-details: the best-matching card of one character or with no character, and the
# best-matching equipment, with what a deck view draws.
DECK_CARD_SQL = f"""
SELECT game_id, name, base_name, variant, cost, image_url FROM card
WHERE (character_slug = %(slug)s OR character_slug IS NULL) AND {NAME_MATCH}
ORDER BY {NAME_ORDER} LIMIT 1
"""
DECK_GEAR_SQL = f"""
SELECT game_id, name, slot, rarity, image_url FROM equipment
WHERE {NAME_MATCH} ORDER BY {NAME_ORDER} LIMIT 1
"""

# character_guide part name -> query for that part of one character's data (%(slug)s). The
# plan, teams, and synergies parts have their own queries: they span every guide page.
CHARACTER_SECTIONS = {
    # Reviews are left out to keep this small; lookup with detail returns them for one card.
    "cards": "SELECT category, name, variant, alias, color, cost, type, tags, text, rating,"
    " target_neutral FROM card WHERE character_slug = %(slug)s ORDER BY category, base_name, variant",
    "potentials": "SELECT position, name, text FROM potential WHERE character_slug = %(slug)s",
    "manifest_egos": "SELECT level, name, text, performance, review FROM manifest_ego"
    " WHERE character_slug = %(slug)s ORDER BY level",
    "equipment": "SELECT tab, best, slots, notes FROM character_equipment"
    " WHERE character_slug = %(slug)s",
    "seasonal_powers": "SELECT season, best, text FROM seasonal_power"
    " WHERE character_slug = %(slug)s",
    "fragments": "SELECT build, position, sets, performance, notes FROM fragment_rank"
    " WHERE character_slug = %(slug)s ORDER BY build, position",
    "partners": "SELECT position, partner, dupes, performance, notes FROM partner_rank"
    " WHERE character_slug = %(slug)s ORDER BY position",
}
_SECTION_COUNTS_SQL = "SELECT " + ", ".join(
    f"(SELECT count(*) FROM ({sql}) s) AS {name}" for name, sql in CHARACTER_SECTIONS.items()
)

# Where guides recommend an item: the best-matching equipment, fragment set, or partner name
# (fuzzy), then every deck, equipment tab, and ranking row that lists it. Guides spell items
# Prydwen's way (guide_name), which can differ from the game's name.
RECOMMENDED_BY_SQL = """
WITH item AS (
    SELECT name, guide_name FROM (
        SELECT name, coalesce(prydwen_name, name) AS guide_name FROM equipment
        UNION SELECT name, coalesce(prydwen_name, name) FROM fragment_set
        UNION SELECT name, coalesce(prydwen_name, name) FROM partner
    ) names
    WHERE {match}
    ORDER BY {order}
    LIMIT 1
)
SELECT c.name AS character, array_agg(DISTINCT concat_ws(': ', r.place, r.detail)) AS places
FROM item, LATERAL (
    SELECT character_slug, 'deck' AS place, name AS detail FROM deck
    WHERE item.guide_name = ANY (equipment) OR item.guide_name = ANY (fragments)
       OR partner = item.guide_name
    UNION ALL
    -- One entry for any number of season tabs, so "who uses this" stays short.
    SELECT character_slug, 'equipment tab', NULL FROM character_equipment
    WHERE EXISTS (
        SELECT FROM jsonb_each(slots) slot, jsonb_array_elements_text(slot.value) listed
        WHERE listed = item.guide_name
    )
    UNION ALL
    SELECT character_slug, 'fragment ranking',
           concat_ws(' ', build, '#' || position, performance) FROM fragment_rank
    WHERE item.guide_name = ANY (sets)
    UNION ALL
    SELECT character_slug, 'partner ranking',
           concat_ws(' ', '#' || position, dupes, performance) FROM partner_rank
    WHERE partner = item.guide_name
) r
JOIN character c ON c.slug = r.character_slug
GROUP BY c.name
ORDER BY count(*) DESC, c.name
"""
RECOMMENDED_BY_SQL = RECOMMENDED_BY_SQL.format(match="name = %(name)s", order="name")

LIST_CHARACTERS_SQL = """
SELECT c.name, c.rarity, c.element, c.class, c.gender, c.image_url
FROM character c
WHERE (%(gender)s::text IS NULL OR c.gender = lower(%(gender)s))
  AND (%(element)s::text IS NULL OR c.element ILIKE %(element)s)
  AND (%(class)s::text IS NULL OR c.class ILIKE %(class)s)
  AND (%(rarity)s::int IS NULL OR c.rarity = %(rarity)s)
ORDER BY c.name
"""

# Characters the guides pair with one character, from three sources: that character's own
# synergy list (with Prydwen's reasons), other characters' synergy lists that name it, and
# recommended teams it is in. Teams are listed on several pages, so they are deduplicated.
SYNERGIES_SQL = """
WITH me AS (
    SELECT slug, name FROM character
    WHERE slug = %(name)s OR {match}
    ORDER BY slug = %(name)s DESC, {order}
    LIMIT 1
),
teams AS (
    SELECT DISTINCT t.name, t.tier, t.members FROM team t, me WHERE me.name = ANY (t.members)
),
mentions AS (
    SELECT s.name AS partner, 'own synergy list' AS source, s.points, NULL AS team
    FROM synergy s, me WHERE s.character_slug = me.slug
    UNION ALL
    SELECT c.name, 'their synergy list', s.points, NULL
    FROM synergy s JOIN character c ON c.slug = s.character_slug, me WHERE s.name = me.name
    UNION ALL
    SELECT member, 'team', NULL,
           teams.name || coalesce(' (' || teams.tier || ')', '')
    FROM teams, me, unnest(teams.members) member WHERE member <> me.name
)
SELECT (SELECT name FROM me) AS "for", partner AS name,
       array_agg(DISTINCT source) AS sources,
       array_agg(DISTINCT team) FILTER (WHERE team IS NOT NULL) AS teams,
       jsonb_agg(points) FILTER (WHERE source = 'own synergy list') -> 0 AS reasons,
       jsonb_agg(points) FILTER (WHERE source = 'their synergy list') -> 0 AS their_reasons
FROM mentions
GROUP BY partner
ORDER BY count(DISTINCT source) DESC, count(DISTINCT team) DESC, partner
"""
SYNERGIES_SQL = SYNERGIES_SQL.format(match=NAME_MATCH, order=NAME_ORDER)

# The plan part: one character's decks are its Save Data decks in the newest leaderboard season
# (%(season)s) plus Prydwen's guide decks. Prydwen lists deck cards by name with the Epiphany
# numeral ("Anchor Shot IV"), and lists complete decks: they are the size of saved decks.
# Prydwen names some cards differently from the game (card.prydwen_name), so Prydwen deck
# card names are translated to the game's base name before counting.
_DECKS = """
lb AS (
    SELECT rank, team, cards, equipment FROM leaderboard_deck
    WHERE character_slug = %(slug)s AND season = %(season)s
),
pdecks AS (
    SELECT row_number() OVER () AS id, cards, equipment FROM deck
    WHERE character_slug = %(slug)s
),
pd_cards AS (
    SELECT pdecks.id, coalesce(game.base_name, listed.base) AS base, listed.variant
    FROM pdecks, jsonb_array_elements(pdecks.cards) e,
         LATERAL (SELECT regexp_replace(e->>'name', '\\s+(I|II|III|IV|V)$', '') AS base,
                         coalesce(substring(e->>'name' from '\\s(I|II|III|IV|V)$'), '')
                             AS variant) listed
    LEFT JOIN LATERAL (
        SELECT base_name FROM card
        WHERE (character_slug = %(slug)s OR character_slug IS NULL)
          -- Guide decks spell some apostrophes straight where the game's names curl them.
          AND translate(listed.base, '’‘', '''''')
              IN (translate(base_name, '’‘', ''''''), translate(prydwen_name, '’‘', ''''''))
        ORDER BY character_slug NULLS LAST LIMIT 1
    ) game ON true
)
"""
PLAN_CARDS_SQL = f"""
WITH {_DECKS},
total AS (SELECT count(*) AS n FROM lb),
lb_picks AS (
    SELECT c->>'base' AS base, c->>'variant' AS variant, count(DISTINCT (rank, team)) AS decks,
           array_remove(array_agg(DISTINCT c->>'divine_epiphany'), NULL) AS divine
    FROM lb, jsonb_array_elements(lb.cards) c GROUP BY 1, 2
),
pd_picks AS (
    SELECT base, variant, count(DISTINCT id) AS decks FROM pd_cards GROUP BY 1, 2
),
picks AS (
    SELECT coalesce(l.base, p.base) AS base, coalesce(l.variant, p.variant) AS variant,
           l.decks AS lb_decks, l.divine, p.decks AS pd_decks
    FROM lb_picks l FULL JOIN pd_picks p ON p.base = l.base AND p.variant = l.variant
)
SELECT picks.base AS card, base.category, base.cost, base.color,
       -- json, not jsonb, keeps the field order: the variant names each version.
       json_agg(json_strip_nulls(json_build_object(
           'variant', coalesce(nullif(picks.variant, ''), 'base'),
           'rating', r.rating,
           'leaderboard_share', round(picks.lb_decks::numeric / nullif(total.n, 0), 2),
           'guide_decks', picks.pd_decks,
           'divine_epiphanies', nullif(picks.divine, '{{}}')
       )) ORDER BY coalesce(picks.lb_decks, 0) DESC, coalesce(picks.pd_decks, 0) DESC,
                   picks.variant) AS versions
FROM picks CROSS JOIN total
LEFT JOIN LATERAL (
    SELECT rating FROM card
    WHERE base_name = picks.base AND variant = picks.variant
      AND (character_slug = %(slug)s OR character_slug IS NULL)
    ORDER BY character_slug NULLS LAST LIMIT 1
) r ON true
LEFT JOIN LATERAL (
    SELECT category, cost, color FROM card
    WHERE base_name = picks.base AND variant = ''
      AND (character_slug = %(slug)s OR character_slug IS NULL)
    ORDER BY character_slug NULLS LAST LIMIT 1
) base ON true
GROUP BY picks.base, base.category, base.cost, base.color
ORDER BY max(coalesce(picks.lb_decks, 0)) DESC, sum(coalesce(picks.pd_decks, 0)) DESC, picks.base
LIMIT 40
"""
# Removal candidates are starting cards; the Ego skill is not in the deck (Prydwen-only
# characters mark it by type, game data by category).
PLAN_REMOVALS_SQL = f"""
WITH {_DECKS},
starting AS (
    SELECT DISTINCT base_name FROM card
    WHERE character_slug = %(slug)s AND category = 'starting' AND variant = ''
      AND type IS DISTINCT FROM 'Ego Skill'
),
decks AS (
    SELECT array(SELECT c->>'base' FROM jsonb_array_elements(lb.cards) c) AS names FROM lb
    UNION ALL
    SELECT array_agg(base) FROM pd_cards GROUP BY id
)
SELECT s.base_name AS card,
       round(avg((NOT s.base_name = ANY (d.names))::int), 2)::float8 AS dropped_share,
       count(*) AS decks
FROM starting s CROSS JOIN decks d
GROUP BY s.base_name
HAVING bool_or(NOT s.base_name = ANY (d.names))
ORDER BY dropped_share DESC, card
LIMIT 5
"""
# Equipment worn in leaderboard decks (share of them) and listed in Prydwen decks (count),
# under the game's item name.
PLAN_EQUIPMENT_SQL = f"""
WITH {_DECKS},
worn AS (
    SELECT item, (rank, team)::text AS deck, 'leaderboard' AS source
    FROM lb, unnest(lb.equipment) item
    UNION ALL
    SELECT coalesce(e.name, listed), id::text, 'prydwen'
    FROM pdecks, unnest(pdecks.equipment) listed
    LEFT JOIN LATERAL (
        SELECT name FROM equipment WHERE listed IN (name, prydwen_name) LIMIT 1
    ) e ON true
)
SELECT worn.item, max(e.slot) AS slot,
       round(count(DISTINCT deck) FILTER (WHERE worn.source = 'leaderboard')::numeric
             / nullif((SELECT count(*) FROM lb), 0), 2)::float8 AS leaderboard_share,
       nullif(count(DISTINCT deck) FILTER (WHERE worn.source = 'prydwen'), 0) AS guide_decks
FROM worn LEFT JOIN equipment e ON e.name = worn.item
GROUP BY worn.item
ORDER BY leaderboard_share DESC NULLS LAST, guide_decks DESC NULLS LAST, worn.item
LIMIT 20
"""

# Events that can appear in a Chaos (%(chaos)s, an exact name from the chaos view): those
# listed for it, plus common events of every Chaos.
_IN_CHAOS = "(%(chaos)s::text IS NULL OR e.any_chaos OR %(chaos)s = ANY (e.chaos))"
# Every version of the best-matching event: all listed entries with that exact name. Follow-up
# screen titles match too ("Into the Base" finds Differing Creeds); title is the one matched.
ENCOUNTER_VERSIONS_SQL = f"""
WITH titles AS (
    SELECT e.id, e.name AS event, e.name FROM encounter e WHERE {_IN_CHAOS}
    UNION ALL
    SELECT e.id, e.name, s.name
    FROM encounter e JOIN encounter_step s ON s.id = ANY (e.steps[2:])
    WHERE {_IN_CHAOS}
), best AS (
    SELECT event, name AS title FROM titles
    WHERE {NAME_MATCH}
    ORDER BY {NAME_ORDER}, id
    LIMIT 1
)
SELECT e.name, e.steps, e.url, e.data_fetched_at, best.title
FROM encounter e JOIN best ON e.name = best.event
WHERE {_IN_CHAOS}
ORDER BY e.id
"""
# Steps offering every choice text in %(choices)s (patterns: lowercased, straight quotes, LIKE
# wildcards escaped, ending in % so text cut off on screen still matches), with the events that
# reach them; events whose name matches best come first.
_CHOICE_TEXT = "replace(lower(c.text), '’', '''')"
ENCOUNTER_SCREEN_SQL = f"""
WITH screen AS (
    SELECT s.id FROM encounter_step s
    WHERE NOT EXISTS (
        SELECT 1 FROM unnest(%(choices)s::text[]) AS g (pattern)
        WHERE NOT EXISTS (
            SELECT 1 FROM encounter_choice c
            WHERE c.step_id = s.id AND {_CHOICE_TEXT} LIKE g.pattern
        )
    )
)
SELECT screen.id AS step_id, e.name, e.url, e.data_fetched_at,
       array_position(e.steps, screen.id) AS step
FROM screen JOIN encounter e ON screen.id = ANY (e.steps)
WHERE {_IN_CHAOS}
ORDER BY {NAME_MATCH} DESC, {NAME_ORDER}, e.id
LIMIT 6
"""
ENCOUNTER_STEPS_SQL = """
SELECT s.id, s.name, s.shown_choices, s.auto_choice, s.appears_when, s.extra,
       (SELECT json_agg(c ORDER BY c.position) FROM encounter_choice c WHERE c.step_id = s.id)
           AS choices
FROM encounter_step s
WHERE s.id = ANY (%(steps)s)
"""
# Rows past LIST_LIMIT are cut; total says how many matched.
LIST_LIMIT = 60
# Rows per call of the list kinds that page (a page of Divine Epiphanies is about 10,000
# characters); list's description states the number.
LIST_PAGE = 30
# Names per lookup call, and matches listed per name; lookup's description states both.
LOOKUP_NAMES = 10
LOOKUP_MATCHES = 5

# Words players use that the official posts never do, mapped to the words the posts use
# instead, which search adds to the query when it covers announcements. The meaning search catches most rewording;
# these are words it ranked too low (audit, 2026-11-20). Add one when an audit finds another
# miss.
POST_SYNONYMS = {
    "ban": "season restrictions",
    "banned": "season restrictions",
    "bans": "season restrictions",
    "shop": "supply store",
    "shops": "supply store",
}


def _with_synonyms(query: str) -> str:
    """query with the post words for its player words (POST_SYNONYMS) added."""
    words = re.findall(r"\w+", query.lower())
    phrases = dict.fromkeys(POST_SYNONYMS[w] for w in words if w in POST_SYNONYMS)
    return " ".join([query, *phrases])


LIST_ENCOUNTERS_SQL = f"""
SELECT e.name, e.pack, e.chaos, e.any_chaos, e.reward_classes,
       (SELECT json_agg(json_build_object('answer', c.text, 'step', s.n,
                                          'gives', c.reward_classes)
                         ORDER BY s.n, c.position)
        FROM unnest(e.steps) WITH ORDINALITY AS s (id, n)
        JOIN encounter_choice c ON c.step_id = s.id
        WHERE c.reward_classes && CASE WHEN %(reward)s::text IS NULL
              THEN ARRAY['copy', 'removal'] ELSE ARRAY[%(reward)s::text] END
       ) AS answers,
       e.url, e.data_fetched_at, count(*) OVER () AS total
FROM encounter e
WHERE {_IN_CHAOS} AND (%(reward)s::text IS NULL OR %(reward)s = ANY (e.reward_classes))
ORDER BY 'copy' = ANY (e.reward_classes) DESC, 'removal' = ANY (e.reward_classes) DESC,
         e.any_chaos, e.name
LIMIT {LIST_PAGE} OFFSET %(offset)s
"""
CHAOS_MATCH_SQL = f"""
SELECT name FROM chaos WHERE {NAME_MATCH} ORDER BY {NAME_ORDER} LIMIT 1
"""


def _database_url() -> str:
    return os.environ["DATABASE_URL"]


async def _query(sql: str, params: dict | None = None) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(_database_url(), row_factory=dict_row) as conn:
        rows = await (await conn.execute(sql, params or {})).fetchall()
    return [_present(row) for row in rows]


def _present(row: dict) -> dict:
    """Drop null and empty fields to save model tokens; keep 0 and False, which carry meaning."""
    return {k: v for k, v in row.items() if v is not None and v != "" and v != [] and v != {}}


def _choice_key(text: str) -> str:
    """A choice text as get_encounter compares it: lowercase, straight quotes, without the
    [Tag] the screen puts before dice choices or the ellipsis of text cut off on screen."""
    key = text.lower().replace("’", "'").strip()
    key = re.sub(r"^\[[^\]]*\]\s*", "", key)
    return re.sub(r"\s*(…|\.\.\.)$", "", key)


async def _encounter_step_rows(step_ids: list[str]) -> dict[str, dict]:
    rows = await _query(ENCOUNTER_STEPS_SQL, {"steps": sorted(set(step_ids))})
    return {row["id"]: row for row in rows}


def _collapse_copies(choices: list[dict]) -> list[dict]:
    """choices with each run of identical ones shown once, counted in copies."""
    kept: list[dict] = []
    for choice in choices:
        if kept and kept[-1] | {"copies": 0} == choice | {"copies": 0}:
            kept[-1]["copies"] = kept[-1].get("copies", 1) + 1
        else:
            kept.append(choice)
    return kept


def _dice(choice: dict) -> str | None:
    return choice["dice_skill"] and f"{choice['dice_skill']} {choice['dice_target']}"


def _requires(choice: dict) -> list[str]:
    return [r["tooltip"] or r["condition"] for r in choice["requirements"]]


def _full_choices(shown: list[dict], step_id: str, following: dict[str, dict]) -> list[dict]:
    """get_encounter's on-screen choices with every result, each next screen in short form.
    Uninterpreted game fields (extra) sit beside the named ones, so the renderer states a value
    every choice shares only once."""
    return _collapse_copies(
        [
            _present(
                {
                    "answer": choice["text"],
                    "kind": choice["kind"],
                    "random_group": choice["random_group"],
                    "dice": _dice(choice),
                    "battle": choice["battle"],
                    "requires": _requires(choice),
                    "appears_when": choice["appears_when"],
                }
                | choice["extra"]
                | {
                    "results": [
                        _present(
                            {
                                "outcome": result["outcome"],
                                "text": result["text"],
                                "battle": result["battle"],
                                # type is the game's code for what class and text state.
                                "rewards": [
                                    {"text": r["text"], "class": r["class"]}
                                    for r in result["rewards"]
                                ],
                            }
                            | result["extra"]
                            | {"next": _next_screen(result["next_step"], step_id, following)}
                        )
                        for result in choice["results"]
                    ]
                }
            )
            for choice in shown
        ]
    )


def _next_screen(next_step: str | None, step_id: str, following: dict[str, dict]):
    if not next_step:
        return None
    if next_step == step_id:
        return "this screen"
    return _screen_preview(following[next_step])


def _counted(step: dict) -> bool:
    """Whether the screen's counts can be stated. A choice with a condition depends on game
    state the data does not hold (trigger flags), so the count is unknown."""
    choices = step.get("choices") or []
    return step.get("shown_choices") is not None and not any(c["appears_when"] for c in choices)


def _shows(step: dict) -> int | None:
    """How many choices the screen displays. The game's count (shown_choices) leaves out
    battle choices, which the screen always adds, and can exceed the choices there are."""
    if not _counted(step):
        return None
    battles = sum(c["kind"] == "battle" for c in step["choices"])
    return min(step["shown_choices"], len(step["choices"]) - battles) + battles


def _random_draw(step: dict) -> str | None:
    """The random marker for a screen's random choices: the slots left after its fixed
    choices (not random, no battle), out of all random choices."""
    choices = [c for c in step.get("choices") or [] if c["kind"] != "battle"]
    drawn_from = sum(bool(c["random_group"]) for c in choices)
    if not drawn_from:
        return None
    if not _counted(step):
        return "may not appear"
    fixed = sum(not c["random_group"] for c in choices)
    slots = min(max(step["shown_choices"] - fixed, 0), drawn_from)
    return f"screen shows {slots} of {drawn_from}"


def _screen_preview(step: dict) -> dict:
    """A screen in short form: every choice it can offer, one line each."""
    draw = _random_draw(step)
    return _present(
        {
            "name": step.get("name"),
            "shows": _shows(step),
            "choices": _collapse_copies(
                [_short_choice(c, draw) for c in step.get("choices") or []]
            ),
        }
    )


def _short_choice(choice: dict, draw: str | None) -> dict:
    by_outcome = {r["outcome"]: r for r in choice["results"]}

    def outcome(name: str) -> str | None:
        result = by_outcome.get(name)
        if not result:
            return None
        parts = [r["text"] for r in result["rewards"]] or [result["text"]]
        return " + ".join(p for p in parts if p) or None

    return _present(
        {
            "answer": choice["text"],
            "random": choice["random_group"] and choice["kind"] != "battle" and draw,
            "appears_when": choice["appears_when"],
            "dice": _dice(choice),
            "requires": _requires(choice),
            "gives": outcome("success"),
            "fails": outcome("failure"),
            "leads_on": any(r["next_step"] for r in choice["results"]) or None,
        }
    )


def _for_model(value: object) -> object:
    """value without INTERNAL_FIELDS and null fields, with OPINION_FIELDS renamed, at any
    depth."""
    if isinstance(value, dict):
        return {
            OPINION_FIELDS.get(k, k): _for_model(v)
            for k, v in value.items()
            if k not in INTERNAL_FIELDS and v is not None
        }
    if isinstance(value, list):
        return [_for_model(item) for item in value]
    return value


def _deck_duplicates(cards: list[dict], starting: set[str]) -> list[str]:
    """The cards a deck holds more than one of, as "Sword Resonance x4: II x4 (3 Duplicates)".
    A run gains a unique card once, so its further copies are Duplicates; starting cards (base
    names in starting) can begin with several copies, so only their count is given."""
    versions: dict[str, Counter] = {}
    for card in cards:
        name = card.get("name") or ""
        base = re.sub(r"\s+(I|II|III|IV|V)$", "", name)
        variant = name[len(base) :].strip() or "base"
        versions.setdefault(base, Counter())[variant] += card.get("count") or 1
    lines = []
    for base, counts in versions.items():
        total = sum(counts.values())
        if total > 1:
            held = ", ".join(f"{variant} x{n}" for variant, n in counts.items())
            gained = "starting card" if base in starting else f"{total - 1} Duplicates"
            lines.append(f"{base} x{total}: {held} ({gained})")
    return lines


class DeckCard(BaseModel):
    """One card in a Save Data deck: copies of one version, each carrying a Divine Epiphany
    when divine is set."""

    card: str
    version: str | None = None
    divine: bool = False
    copies: int = Field(default=1, ge=1)


# Faint Memory points, from the in-game Help topic "Save Rules for Faint Memories".
DIVINE_POINTS = 20
CARD_POINTS = 20  # Neutral and Forbidden cards
MONSTER_POINTS = {"White": 20, "Blue": 50, "Gold": 80}  # Normal, Rare, Legendary
FREE_DUPLICATES = 2
DUPLICATE_POINTS = 40
REMOVED_STARTING_POINTS = 20
MAX_DUPLICATES = 3
MAX_REMOVALS = 5
# Cards the character starts with or gains as her own (Epiphany cards): no points.
OWN_CATEGORIES = frozenset({"starting", "unique", "ego", "desire"})
NEUTRAL_CATEGORIES = frozenset({"neutral", "forbidden"})


def _card_points(row: dict) -> tuple[int, str]:
    """A card's own points and the rule that set them, before Divines and Duplicates."""
    category = row["category"]
    if category in OWN_CATEGORIES:
        return 0, f"{category} card: free"
    # Season 5's Memory cards, which Prydwen also files under Neutral: the game's counter showed
    # 60 points for a deck holding Memory: Loss and three removed starting cards.
    if "Memory" in row.get("tags", ()):
        return 0, "Memory card: free"
    if category in NEUTRAL_CATEGORIES:
        return CARD_POINTS, f"{category} card"
    if category == "monster":
        points = MONSTER_POINTS.get(row.get("color"))
        if points is not None:
            return points, f"monster card ({row['color']})"
        return 0, f"monster card of unknown rarity ({row.get('color')}): not counted"
    return 0, f"{category} card: the Help names no rule, not counted"


def _save_data_cost(
    entries: list[tuple[DeckCard, dict | None]], removed: list[tuple[str, dict | None]]
) -> dict:
    """The Faint Memory points of a deck: entries pair each DeckCard with its card row (None
    when no card matched), removed pairs each removed card's name with its row."""
    lines = []
    unknown = []
    held: Counter = Counter()
    duplicates = 0
    for entry, row in entries:
        name = f"{entry.card} {entry.version}" if entry.version else entry.card
        if row is None:
            unknown.append(name)
            continue
        points, rule = _card_points(row)
        total = points * entry.copies
        rules = [rule + (f" x{entry.copies}" if points and entry.copies > 1 else "")]
        if entry.divine:
            total += DIVINE_POINTS * entry.copies
            rules.append(f"Divine Epiphany x{entry.copies}")
        # Copies beyond the starting deck's (one for any card it does not start with) are
        # Duplicates, whichever versions they are.
        free = row.get("starting_copies") or 1
        before = held[row["base_name"]]
        held[row["base_name"]] += entry.copies
        new = held[row["base_name"]] - max(before, free)
        for _ in range(max(new, 0)):
            duplicates += 1
            if duplicates > FREE_DUPLICATES:
                total += DUPLICATE_POINTS
                rules.append(f"Duplicate {duplicates}")
            else:
                rules.append(f"Duplicate {duplicates}: free")
        lines.append({"card": row["name"], "copies": entry.copies, "points": total, "rules": rules})
    for name, row in removed:
        if row is None:
            unknown.append(name)
            continue
        starting = row["category"] == "starting"
        lines.append(
            {
                "removed": row["name"],
                "points": REMOVED_STARTING_POINTS if starting else 0,
                "rules": ["removed starting card" if starting else "removed card: free"],
            }
        )
    warnings = []
    if duplicates > MAX_DUPLICATES:
        warnings.append(f"{duplicates} Duplicates; Save Data keeps at most {MAX_DUPLICATES}")
    if len(removed) > MAX_REMOVALS:
        warnings.append(f"{len(removed)} removals; Save Data keeps the first {MAX_REMOVALS} only")
    return {
        "total": sum(line["points"] for line in lines),
        "lines": lines,
        "unknown": unknown or None,
        "warnings": warnings or None,
    }


def _log_arguments(arguments: dict) -> str:
    """A tool call's arguments as one JSON string for its log line, cut to LOG_ARGUMENT_CHARS
    so card text sent to match_card_text cannot flood the log."""
    text = json.dumps({k: v for k, v in arguments.items() if v is not None}, default=str)
    return text if len(text) <= LOG_ARGUMENT_CHARS else text[:LOG_ARGUMENT_CHARS] + "..."


def build_server(auth: AuthSettings | None, verifier: TokenVerifier | None) -> MCPServer:
    mcp = MCPServer(
        "czn-data",
        auth=auth,
        token_verifier=verifier,
    )

    def tool(function=None, *, name: str | None = None):
        """Register function as an MCP tool, named name or the function's name, whose reply is
        the plain text of its result. Every call logs one ``tool call`` line: the tool, its
        arguments, seconds, and reply size, or the error it failed with."""
        if function is None:
            return functools.partial(tool, name=name)
        tool_name = name or function.__name__

        @functools.wraps(function)
        async def text_reply(*args, **kwargs) -> str:
            started = time.monotonic()
            fields = {"tool": tool_name, "arguments": _log_arguments(kwargs)}
            try:
                reply = render(_for_model(await function(*args, **kwargs)))
            except Exception as error:
                fields |= {"seconds": round(time.monotonic() - started, 3), "error": str(error)}
                level = logging.INFO if isinstance(error, ToolError) else logging.ERROR
                log.log(level, "tool call failed", extra=fields, exc_info=level == logging.ERROR)
                raise
            fields |= {"seconds": round(time.monotonic() - started, 3), "reply_chars": len(reply)}
            log.info("tool call", extra=fields)
            return reply

        mcp.tool(name=tool_name, structured_output=False)(text_reply)
        return function

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def authorized(request: Request) -> bool:
        """Custom routes are outside the MCP auth middleware: they take the same Bearer token,
        checked here."""
        if verifier is None:
            return True
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return bool(token) and await verifier.verify_token(token) is not None

    @mcp.custom_route("/characters", methods=["GET"])
    async def characters(request: Request) -> JSONResponse:
        """REST endpoint for the companion app: every character's name and card art URL, by
        name."""
        if not await authorized(request):
            return JSONResponse({"error": "invalid or missing Bearer token"}, 401)
        rows = await _query("SELECT name, image_url FROM character ORDER BY name")
        return JSONResponse({"characters": rows})

    async def screenshot(request: Request) -> bytes | JSONResponse:
        """The screenshot a screenshot route was sent, or the error response to send back. The
        body is the JPEG or PNG; the Bearer token is the one the MCP endpoint takes."""
        if not await authorized(request):
            return JSONResponse({"error": "invalid or missing Bearer token"}, 401)
        if request.headers.get("content-type") not in ("image/jpeg", "image/png"):
            return JSONResponse({"error": "send the screenshot as image/jpeg or image/png"}, 400)
        image = await request.body()
        if len(image) > IDENTIFY_MAX_BYTES:
            return JSONResponse({"error": "screenshot larger than 10 MB"}, 413)
        return image

    identifier = Identifier()
    gear_identifier = GearIdentifier()
    identity_prints = identity.prints_digest()
    gear_prints = gear.prints_digest()

    @mcp.custom_route("/identify", methods=["POST"])
    async def identify(request: Request) -> JSONResponse:
        """REST endpoint for the companion app: names the characters on a game screenshot (see
        ``czn_data.identity``)."""
        image = await screenshot(request)
        if isinstance(image, JSONResponse):
            return image
        rows = await _query(
            """
            SELECT c.name, i.print FROM character c
            JOIN prydwen.portrait_identity i ON i.image_url = c.image_url AND i.digest = %(code)s
            """,
            {"code": identity_prints},
        )
        prints = {row["name"]: row["print"] for row in rows}
        started = time.monotonic()
        try:
            figures = await anyio.to_thread.run_sync(
                identifier.identify, image, prints, limiter=search.MODELS
            )
        except UnidentifiedImageError:
            return JSONResponse({"error": "body is not a readable image"}, 400)
        log.info(
            "identified screenshot",
            extra={
                "figures": len(figures),
                "seconds": round(time.monotonic() - started, 2),
                "bytes": len(image),
            },
        )
        return JSONResponse(
            {"model": IDENTITY_MODEL, "characters": [asdict(figure) for figure in figures]}
        )

    @mcp.custom_route("/identify-gear", methods=["POST"])
    async def identify_gear(request: Request) -> JSONResponse:
        """REST endpoint for the companion app: names the equipment a game screenshot shows as
        icons in slots (see ``czn_data.gear``)."""
        image = await screenshot(request)
        if isinstance(image, JSONResponse):
            return image
        rows = await _query(
            """
            SELECT p.icon_name, p.prints,
                   array_agg(DISTINCT e.name ORDER BY e.name)
                       FILTER (WHERE e.name IS NOT NULL) AS names,
                   jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                       'name', e.name, 'rarity', e.rarity, 'slot', e.slot, 'text', e.text,
                       'stats', nullif(e.stats, '{}'::jsonb),
                       'transformed', coalesce(e.transformed, false))))
                       FILTER (WHERE e.name IS NOT NULL) AS items
            FROM e7bot.gear_print p
            LEFT JOIN equipment e ON e.icon_name = p.icon_name
            WHERE p.digest = %(code)s
            GROUP BY p.icon_name, p.digest
            """,
            {"code": gear_prints},
        )
        # Icons that name no item still compete, so a slot showing one is left out rather than
        # named as the next most similar gear.
        prints = {row["icon_name"]: row["prints"] for row in rows}
        names = {row["icon_name"]: row.get("names", []) for row in rows}
        started = time.monotonic()
        try:
            gear = await anyio.to_thread.run_sync(
                gear_identifier.identify, image, prints, names, limiter=search.MODELS
            )
        except UnidentifiedImageError:
            return JSONResponse({"error": "body is not a readable image"}, 400)
        log.info(
            "identified gear",
            extra={
                "slots": len(gear),
                "seconds": round(time.monotonic() - started, 2),
                "bytes": len(image),
            },
        )
        # Each slot carries its items' details, so Claude can compare worn gear without
        # looking every name up.
        items = {item["name"]: item for row in rows for item in row.get("items", [])}
        return JSONResponse(
            {
                "model": GEAR_MODEL,
                "gear": [
                    asdict(slot) | {"items": [items[name] for name in slot.names]} for slot in gear
                ],
            }
        )

    @tool
    async def lookup(
        names: list[str], kind: LookupKind | None = None, detail: bool = False
    ) -> list[dict]:
        """Find any named game entity, for each name read from the screen; send every name a
        screen shows in one call (at most 10). Per name: best, the best match with its facts,
        and other_matches (up to 4 more, one line each) when the name is unclear.

        kind narrows the search: character, card, equipment, fragment_set, partner, effect
        (keywords, statuses and card tags such as Exhaust; keyword, status and tag are other
        names for it), encounter (Chaos event; get_encounter reads its screens),
        divine_epiphany, or chaos (the Chaos maps). A term without its own entry, such as
        Polarization, finds the effects whose description uses it.

        A card's best match lists every version of it (base, Epiphany I-V, and others such as
        mutations, some under their own name like Shard:Light) with cost, tags, text, and the
        guide's rating (1 to 5); alias is the version's name in the game (Unbound in "Flicker
        Blade (Unbound)"); starting_copies is how many copies a starting deck holds. With
        detail, cards add the guides' reviews and their Neutral and Divine advice, and
        equipment, fragment sets, and partners add which characters' guides recommend them
        and where. Characters list the parts character_guide gives.

        Season content is not in this data: season events and Interaction Events (such as
        Elaborate Illusion), Chaos difficulty modifiers (such as Gangrene), and Environment
        Variables nodes; search has those."""
        kind = LOOKUP_KIND_ALIASES.get(kind, kind) if kind else None
        found = [await _lookup_one(name, kind, detail) for name in names[:LOOKUP_NAMES]]
        skipped = [
            {"name": name, "best": f"Not looked up: at most {LOOKUP_NAMES} names per call."}
            for name in names[LOOKUP_NAMES:]
        ]
        return found + skipped

    async def _lookup_one(name: str, kind: str | None, detail: bool) -> dict:
        found = await _query(LOOKUP_SQL, {"name": name, "kind": kind, "limit": LOOKUP_MATCHES})
        if not found:
            return {"name": name, "best": "No match."}
        details = await _entity_details(found[0]["kind"], found[0]["ref"], detail)
        # A card's other versions are already listed under its best match.
        versions = {v["name"] for v in details.get("versions", [])}
        others = [
            {k: v for k, v in row.items() if k != "ref"}
            for row in found[1:]
            if not (row["kind"] == "card" and row["ref"] in versions)
        ]
        best = {k: v for k, v in found[0].items() if k != "ref"} | details
        return {"name": name, "best": best, "other_matches": others or None}

    async def _entity_details(kind: str, ref: str, detail: bool) -> dict:
        """The facts lookup adds to its best match, by kind; ref is the row's own name."""
        params = {"name": ref}
        if kind == "card":
            rows = await _query(
                """
                WITH best AS (SELECT base_name, character_slug FROM card WHERE name = %(name)s
                              LIMIT 1)
                SELECT card.*, character.name AS character
                FROM card JOIN best USING (base_name)
                LEFT JOIN character ON character.slug = card.character_slug
                WHERE card.character_slug IS NOT DISTINCT FROM best.character_slug
                ORDER BY variant
                """,
                params,
            )
            dropped = {"base_name"} | (set() if detail else GUIDE_ADVICE)
            return {"versions": [{k: v for k, v in r.items() if k not in dropped} for r in rows]}
        if kind == "character":
            character = await _find_character(ref)
            return {"gender": character.get("gender"), "parts": await _guide_parts(character)}
        if kind == "partner":
            rows = await _query("SELECT * FROM partner WHERE name = %(name)s", params)
        elif kind == "equipment":
            rows = await _query(
                "SELECT obtained_from, stats FROM equipment WHERE name = %(name)s", params
            )
        else:
            rows = [{}]
        facts = rows[0] if rows else {}
        if detail and kind in ("equipment", "fragment_set", "partner"):
            facts["recommended_by"] = await _query(RECOMMENDED_BY_SQL, params) or None
        return facts

    async def _guide_parts(character: dict) -> list[str]:
        """The parts character_guide has for character: fixed ones with data, then the
        guide's prose sections by title."""
        counts = (await _query(_SECTION_COUNTS_SQL, {"slug": character["slug"]}))[0]
        prose = [s["title"] for s in character.get("sections") or []]
        return (
            ["profile", "plan", "teams", "synergies"] + [s for s, n in counts.items() if n] + prose
        )

    @tool
    async def character_guide(name: str, parts: list[str] | None = None) -> dict | None:
        """Parts of the best-matching character's guide, as many as needed in one call
        (default profile). profile: rarity, element, class, gender, and every part available.
        plan: everything to plan a Chaos run's deck: the newest leaderboard season's Save Data
        decks and the guides' decks. Its cards are each card the decks use, best first, with
        category, cost and color, and per version (base or Epiphany I-V) the guide's rating,
        the share of leaderboard decks taking it, how many guide decks list it, and common
        Divine Epiphanies; removals are up to 5 starting cards the decks drop (a deck can
        remove at most 5), with the share dropping each; equipment is each item with its slot,
        leaderboard share, and guide deck count; guide_decks are the guides' decks, each with
        its deck code (to paste into the game's deck import) when given, the Chaos it farms,
        duplicates (the cards it holds more than one of, where its Duplicates go), and
        faint_memory: its points as save_data_cost gives them, without removals.
        teams: up to 30 teams with the character: guide_teams (name, tier, members, and
        listed_for, the characters whose guides list it) and leaderboard (the newest season's
        lineups: teams using it and best_rank). synergies: characters the guides pair with
        it, best supported first, with where each is named and why. Also cards (every card
        and version, without reviews), potentials, manifest_egos, equipment (per season tab),
        seasonal_powers, fragments, partners, and prose sections by title (Pros, Cons,
        Introduction, Key Mechanics, Stats, Potential Priority, ...). note explains missing
        data."""
        character = await _find_character(name)
        if character is None:
            return None
        available = await _guide_parts(character)
        prose = character.pop("sections", [])
        slug = {"slug": character["slug"]}
        reply: dict = {"name": character["name"]}
        notes = []
        if "cards" not in available:
            notes.append("No card data for this character yet.")
        for part in parts or ["profile"]:
            key = part.strip().lower()
            if key == "profile":
                reply["profile"] = {
                    k: character.get(k) for k in ("rarity", "element", "class", "gender")
                } | {"parts": available}
            elif key == "plan":
                reply["plan"] = await _plan(character)
            elif key == "teams":
                reply["teams"] = await _teams(character["name"])
            elif key == "synergies":
                reply["synergies"] = await _query(SYNERGIES_SQL, {"name": character["slug"]})
            elif key in CHARACTER_SECTIONS:
                reply[key] = await _query(CHARACTER_SECTIONS[key], slug)
            elif found := [s for s in prose if key in s["title"].lower()]:
                reply.setdefault("sections", []).extend(found)
            else:
                notes.append(f"No part {part!r}. Parts: {', '.join(available)}.")
        return reply | {"note": " ".join(notes) or None}

    @tool
    async def match_card_text(
        texts: list[str], character: str | None = None, limit: int = 3
    ) -> list[dict]:
        """Find the card and Epiphany variant whose effect text best matches each text read from
        the screen (the game shows Epiphany choices as text, without variant numbers); send
        every card's text on a screen in one call. Numbers in the text decide between versions
        that differ only in them; a screen value raised by an upgrade (360% for a stored 300%)
        lowers match. character narrows the search to one character's cards. Returns one item
        per text, in order: the text and its matches, best first, at most limit (up to 10)
        each. match is 0 to 1; alias is the version's name in the game, when it has one."""
        slug = None
        if character:
            found = await _find_character(character)
            if found is None:
                return [{"text": text, "matches": []} for text in texts]
            slug = found["slug"]
        return [
            {"text": text, "matches": await _match_card_text(text, slug, min(limit, 10))}
            for text in texts
        ]

    async def _match_card_text(text: str, slug: str | None, limit: int) -> list[dict]:
        rows = await _query(
            """
            SELECT card.name, card.base_name, card.variant, card.alias, card.color, card.cost,
                   card.type, card.text, card.rating, character.name AS character,
                   card.url, card.prydwen_url, card.data_fetched_at,
                   word_similarity(%(text)s, card.text)::float8 AS match
            FROM card LEFT JOIN character ON character.slug = card.character_slug
            WHERE %(slug)s::text IS NULL OR card.character_slug = %(slug)s
            ORDER BY word_similarity(%(text)s, card.text) DESC, card.name
            LIMIT 40
            """,
            {"text": text, "slug": slug},
        )
        wanted = Counter(re.findall(r"\d+", text))
        for row in rows:
            have = Counter(re.findall(r"\d+", row["text"]))
            shared = sum((wanted & have).values())
            # Half the score is the words, half the numbers (when the screen text has any).
            numbers = shared / max(sum(wanted.values()), sum(have.values())) if wanted else 1
            row["match"] = round(row["match"] * (0.5 + 0.5 * numbers), 2)
        rows.sort(key=lambda row: -row["match"])
        return rows[:limit]

    async def _teams(character: str) -> dict | None:
        """character_guide's teams part: guide teams and leaderboard lineups with character."""
        found = await _query(
            f"""
            WITH member AS (
                SELECT name FROM (
                    SELECT DISTINCT unnest(members) AS name FROM team
                    UNION SELECT name FROM character
                ) members
                WHERE {NAME_MATCH}
                ORDER BY {NAME_ORDER}
                LIMIT 1
            ),
            ranked AS (
                SELECT d.season, d.rank, d.team, max(d.url) AS url,
                       max(d.data_fetched_at) AS data_fetched_at,
                       array_agg(c.name ORDER BY c.name) AS members
                FROM leaderboard_deck d JOIN character c ON c.slug = d.character_slug
                WHERE d.season = (SELECT max(season) FROM leaderboard_entry)
                GROUP BY d.season, d.rank, d.team
            )
            SELECT * FROM (
                SELECT 'guide' AS source, team.name, team.tier, team.members,
                       array_agg(owner.name ORDER BY owner.name) AS listed_for,
                       NULL::text AS season, NULL::bigint AS teams, NULL::int AS best_rank,
                       min(team.url) AS url, min(team.data_fetched_at) AS data_fetched_at
                FROM member, team JOIN character owner ON owner.slug = team.character_slug
                WHERE member.name = ANY (team.members)
                GROUP BY team.name, team.tier, team.members
                UNION ALL
                SELECT 'leaderboard', NULL, NULL, ranked.members, NULL, ranked.season,
                       count(*), min(ranked.rank), max(ranked.url), max(ranked.data_fetched_at)
                FROM member, ranked WHERE member.name = ANY (ranked.members)
                GROUP BY ranked.members, ranked.season
            ) found
            ORDER BY source DESC, tier NULLS LAST, teams DESC NULLS LAST, best_rank, name
            LIMIT 30
            """,
            {"name": character},
        )
        # Two lists, so each states once what all its rows share (season).
        return {
            group: [{k: v for k, v in row.items() if k != "source"} for row in rows]
            for group, source in (("guide_teams", "guide"), ("leaderboard", "leaderboard"))
            if (rows := [row for row in found if row["source"] == source])
        } or None

    async def _find_character(name: str) -> dict | None:
        found = await _query(
            f"""
            SELECT * FROM character WHERE slug = %(name)s OR {NAME_MATCH}
            ORDER BY slug = %(name)s DESC, {NAME_ORDER} LIMIT 1
            """,
            {"name": name},
        )
        return found[0] if found else None

    def _by_source(listing: dict, source_fields: tuple[str, ...]) -> dict:
        """listing with its section rows grouped under the post or page they come from (the
        source_fields every section of it repeats), in order of each source's best section."""
        groups: dict[tuple, dict] = {}
        for row in listing["results"]:
            key = tuple(row.get(field) for field in source_fields)
            group = groups.setdefault(
                key, {f: row[f] for f in source_fields if f in row} | {"sections": []}
            )
            group["sections"].append({k: v for k, v in row.items() if k not in source_fields})
        return listing | {"results": list(groups.values())}

    async def _details(sql: str, ranked: list[dict]) -> dict[tuple, dict]:
        """(key, position) -> the fields sql (given %(keys)s and %(positions)s, returning key
        and position) reads for each ranked passage."""
        rows = await _query(
            sql,
            {
                "keys": [r["key"] for r in ranked],
                "positions": [r["position"] for r in ranked],
            },
        )
        return {(row.pop("key"), row.pop("position")): row for row in rows}

    def _page(rows: list[dict], limit: int, offset: int) -> dict:
        """The reply showing up to limit (at most 20) of rows from offset."""
        offset = max(offset, 0)
        return _reply(rows[offset : offset + max(1, min(limit, 20))], len(rows), offset)

    async def _listing(sql: str, params: dict, offset: int | None = None) -> dict:
        """A list tool's reply: total matches, and the rows shown. offset is given by tools
        that page (their SQL applies it), so the note says how to read on."""
        rows = await _query(sql, params)
        if rows:
            total = rows[0]["total"]
        elif offset:
            # Past the end no row carries the window count; count from the first page.
            first = await _query(sql, params | {"offset": 0})
            total = first[0]["total"] if first else 0
        else:
            total = 0
        for row in rows:
            del row["total"]
        return _reply(rows, total, offset)

    def _reply(rows: list[dict], total: int, offset: int | None) -> dict:
        """A list tool's reply: total matches, the rows shown, and a note when more remain."""
        reply = {"total": total, "results": rows}
        start = offset or 0
        if total > start + len(rows):
            reply["note"] = (
                f"Showing {start + 1}-{start + len(rows)} of {total}; offset="
                f"{start + len(rows)} reads on, or add filters to narrow."
                if offset is not None
                else f"Showing {len(rows)} of {total}; add filters to narrow."
            )
        return reply

    async def _list_cards(
        character: str | None,
        category: str | None,
        type: str | None,
        tag: str | None,
        color: str | None,
        base_only: bool,
    ) -> dict:
        slug = None
        if character:
            found = await _find_character(character)
            if found is None:
                return {"total": 0, "results": [], "note": f"No character matches {character!r}."}
            slug = found["slug"]
        listing = await _listing(
            f"""
            SELECT card.name, card.category, card.color, card.cost, card.type, card.tags,
                   card.text, card.variant, card.alias, card.rating,
                   character.name AS character, card.url, card.data_fetched_at,
                   count(*) OVER () AS total
            FROM card LEFT JOIN character ON character.slug = card.character_slug
            WHERE (%(slug)s::text IS NULL OR card.character_slug = %(slug)s)
              AND (%(category)s::text IS NULL OR card.category = lower(%(category)s))
              AND (%(type)s::text IS NULL OR card.type ILIKE %(type)s)
              AND (%(tag)s::text IS NULL OR %(tag)s ILIKE ANY (card.tags))
              AND (%(color)s::text IS NULL OR card.color ILIKE %(color)s)
              AND (NOT %(base_only)s OR card.variant = '')
            ORDER BY character.name NULLS LAST, card.category, card.name
            LIMIT {LIST_LIMIT * 2}
            """,
            {
                "slug": slug,
                "category": category,
                "type": type,
                "tag": tag,
                "color": color,
                "base_only": base_only,
            },
        )
        if slug and not listing["total"]:
            listing["note"] = f"No card data for {found['name']} yet."
        return listing

    async def _list_equipment(
        slot: str | None, rarity: str | None, name: str | None, chaos: str | None, offset: int
    ) -> dict:
        return await _listing(
            f"""
            SELECT name, rarity, slot, text, obtained_from,
                   CASE WHEN %(name)s::text IS NOT NULL THEN stats END AS stats,
                   prydwen_name, url, data_fetched_at, count(*) OVER () AS total
            FROM equipment
            WHERE (%(slot)s::text IS NULL OR slot ILIKE regexp_replace(%(slot)s, 's$', ''))
              AND (%(rarity)s::text IS NULL OR rarity ILIKE %(rarity)s)
              AND (%(name)s::text IS NULL OR {NAME_MATCH}
                   OR {_name_match("prydwen_name")})
              AND (%(chaos)s::text IS NULL OR EXISTS (
                  -- obtained_from lists places, some shortened ("Kaleidoscope" for "The
                  -- Kaleidoscope Hatchery"), so a place matches when the zone name holds it.
                  SELECT FROM unnest(string_to_array(obtained_from, ', ')) place
                  WHERE %(chaos)s ILIKE '%%' || place || '%%'))
            ORDER BY CASE WHEN %(name)s::text IS NOT NULL THEN word_similarity(%(name)s, name)
                     END DESC NULLS LAST, name
            LIMIT {LIST_PAGE} OFFSET %(offset)s
            """,
            {
                "slot": slot,
                "rarity": rarity,
                "name": name,
                "chaos": await _resolve_chaos(chaos),
                "offset": offset,
            },
            offset=offset,
        )

    async def _list_banners() -> dict:
        rows = await _query(
            """
            SELECT CASE WHEN starts_at <= now() THEN 'live' ELSE 'upcoming' END AS status,
                   name, kind, rarity, rerun, phase, starts_at, ends_at, featured, secondary
            FROM banner WHERE ends_at > now()
            ORDER BY starts_at > now(), ends_at, starts_at, kind, name
            """
        )
        return _reply(rows, len(rows), None)

    # Named search for Claude; the function name keeps the search module in reach here.
    @tool(name="search")
    async def search_text(
        query: str | None = None,
        kinds: list[SearchKind] | None = None,
        active_on: str | None = None,
        read: list[str] | None = None,
    ) -> dict:
        """Search text about the game, split into sections: how the game works, what is on
        now, and strategy. Use it for "how does X work", "what is X", events, season content
        (Season Restrictions, Supply Store, Environment Variables), shops, rate-up periods,
        rewards, and strategy questions the other tools do not cover. Each kind is ranked on
        its own: results holds the full text of each kind's best match, and more lists each
        kind's next 4 matches, one line each (ref, kind, title > heading, date). When results
        miss the answer, pass the refs of the lines that look right as read (up to 10) to get
        their full text, instead of searching again. kinds narrows it (default all):
        - rules: the game's own Help topics and other rule text from its files (tip pages,
          keyword tooltips, tutorial dialogue, event descriptions, popup messages). Exact but
          short; it wins over guides when they disagree.
        - announcements: the official update notices, news, patch notes and event overviews,
          with posted dates and each section's period (starts_on, ends_on) when it has one.
          The newest source: it wins over guides on season content, events, and values that
          change between seasons; between announcements, the newest wins.
        - guides: player guides (game systems, tier lists, team building, character reviews
          and builds), with updated dates when known. More detail and opinion; can be out of
          date.
        query: a question or words, using the game's own words when known. active_on: a date
        (YYYY-MM-DD, or today) to get only announcement sections whose period covers it,
        which answers "what events are on now"; give query, active_on, or both, or read
        alone. active_on without query lists every active section in one line each (ref,
        title > heading, period), soonest ending first. Full text is grouped by page (title,
        kind), each read passage with its ref; a Help topic comes whole, best_match marking
        its page that answers best."""
        if read:
            reply = await _read_passages(read[:SEARCH_READ])
            if skipped := read[SEARCH_READ:]:
                note = f"Not read: {', '.join(skipped)}; at most {SEARCH_READ} refs per call."
                reply["note"] = " ".join(filter(None, [reply.get("note"), note]))
            return reply
        if query is None and active_on is None:
            raise ToolError("Give query, active_on, or both, or read.")
        day = None
        if active_on is not None:
            text = active_on.strip().lower()
            try:
                day = datetime.now(UTC).date() if text == "today" else date.fromisoformat(text)
            except ValueError as error:
                raise ToolError(
                    f"active_on must be YYYY-MM-DD or today, not {active_on!r}"
                ) from error
        group_fields = ("title", "kind", "board", "posted_at", "updated_at")
        if query is None:
            active = await _query(
                """
                SELECT p.ref, concat_ws(' > ', s.post_title, s.heading) AS match, s.starts_on,
                       s.ends_on
                FROM post_section s
                JOIN passage p ON p.source = 'post' AND p.key = s.article_id::text
                              AND p.position = s.position
                WHERE %(day)s::date BETWEEN s.starts_on AND s.ends_on
                ORDER BY s.ends_on, s.posted_at DESC, s.position
                """,
                {"day": day},
            )
            return {"active": active}
        sources = tuple(s for kind in kinds or SEARCH_KINDS for s in search.KIND_SOURCES[kind])
        within = None
        if day is not None:
            sources = ("post",)
            active = await _query(
                "SELECT article_id::text AS key, position FROM post_section"
                " WHERE %(day)s::date BETWEEN starts_on AND ends_on",
                {"day": day},
            )
            within = [(r["key"], r["position"]) for r in active]
        if "post" in sources:
            query = _with_synonyms(query)
        async with await psycopg.AsyncConnection.connect(_database_url()) as conn:
            ranked = await search.search(conn, sources, query, within)
        by_kind = search.rank_by_kind(ranked)
        best = [rows[0] for rows in by_kind.values()]
        more = []
        for kind, rows in by_kind.items():
            # A long section is stored as overlapping pieces that share its title > heading;
            # a piece after the first one shown says so.
            listed = {_match_line(rows[0])}
            for r in rows[1 : 1 + SEARCH_MORE]:
                match = _match_line(r)
                if match in listed:
                    match += " (another part of this section)"
                listed.add(_match_line(r))
                date_ = r["published_at"].date() if r["source"] in DATED_SOURCES else None
                more.append({"ref": r["ref"], "kind": kind, "match": match, "date": date_})
        reply = {"results": await _search_rows(best)}
        return _by_source(reply, group_fields) | {"more": more or None}

    def _match_line(row: dict) -> str:
        """A passage's title > heading, as search's one-line matches name it."""
        return " > ".join(filter(None, [row.get("title"), row.get("heading")]))

    async def _read_passages(refs: list[str]) -> dict:
        """search's reply for read: the full text of the passages with those refs, in order."""
        rows = await _query(
            "SELECT source, key, position, ref, title, heading, text, published_at FROM passage"
            " WHERE ref = ANY(%(refs)s) ORDER BY array_position(%(refs)s, ref)",
            {"refs": refs},
        )
        group_fields = ("title", "kind", "board", "posted_at", "updated_at")
        reply = _by_source({"results": await _search_rows(rows, with_ref=True)}, group_fields)
        found = await _query("SELECT ref FROM passage WHERE ref = ANY(%(refs)s)", {"refs": refs})
        if missing := sorted(set(refs) - {row["ref"] for row in found}):
            reply["note"] = f"No passage {', '.join(missing)}; search again for current refs."
        return reply

    async def _search_rows(ranked: list[dict], with_ref: bool = False) -> list[dict]:
        """search's result rows for ranked passages: each section with its page's title and
        kind, and per kind its dates; a Help topic as all its pages. with_ref adds each
        passage's ref (on a Help topic's first page)."""

        def of(source: str) -> list[dict]:
            return [r for r in ranked if r["source"] == source]

        posts = await _details(
            f"""
            SELECT article_id::text AS key, position, {POST_FIELDS} FROM post_section
            WHERE (article_id::text, position)
                  IN (SELECT * FROM unnest(%(keys)s::text[], %(positions)s::int[]))
            """,
            of("post"),
        )
        updated = await _details(
            """
            SELECT url AS key, position, updated_at FROM game8_section JOIN game8_page USING (url)
            WHERE (url, position) IN (SELECT * FROM unnest(%(keys)s::text[], %(positions)s::int[]))
            """,
            of("game8"),
        )
        # A Help topic ranks by its best page (czn_data.search) and is shown with all its pages.
        best_page = {r["key"]: r["position"] for r in of("help")}
        menus = {
            row["id"]: row["menu"]
            for row in await _query(
                "SELECT id, menu FROM game_help WHERE id = ANY(%(keys)s)", {"keys": list(best_page)}
            )
        }
        help_pages: dict[str, list[dict]] = {}
        for page in await _query(
            "SELECT key, position, heading, text FROM passage"
            " WHERE source = 'help' AND key = ANY(%(keys)s) ORDER BY key, position",
            {"keys": list(best_page)},
        ):
            help_pages.setdefault(page["key"], []).append(page)
        rows = []
        for r in ranked:
            start = len(rows)
            # Read rows come through _query, which drops empty fields such as a missing title
            # or the text of a section that is only a heading.
            section = {k: r.get(k) for k in ("title", "heading", "text")}
            match r["source"]:
                case "post":
                    rows.append(posts[r["key"], r["position"]] | {"kind": "announcement"})
                case "guide":
                    rows.append(section | {"kind": "guide"})
                case "game8":
                    rows.append(section | {"kind": "guide"} | updated[r["key"], r["position"]])
                case "game_text":
                    text_kind = TEXT_KINDS[r["key"].split(":", 1)[0]]
                    rows.append(section | {"kind": f"game rules ({text_kind})"})
                case "help":
                    pages = help_pages[r["key"]]
                    kind = f"game rules (Help, {menus[r['key']]} menu)"
                    rows.extend(
                        {"title": r.get("title"), "kind": kind}
                        | {"heading": page.get("heading"), "text": page["text"]}
                        | (
                            {"best_match": True}
                            if len(pages) > 1 and page["position"] == r["position"]
                            else {}
                        )
                        for page in pages
                    )
            if with_ref:
                rows[start]["ref"] = r["ref"]
        return rows

    @mcp.custom_route("/data-status", methods=["GET"])
    async def data_status(request: Request) -> JSONResponse:
        """REST endpoint for maintainers: freshness of each source (prydwen, e7bot game data,
        stove posts, game8, gamefiles): items stored, newest fetch, and the last attempt with
        its error; for gamefiles, the game build its text comes from. Failed fetches never
        replace served data."""
        if not await authorized(request):
            return JSONResponse({"error": "invalid or missing Bearer token"}, 401)
        summary = await _query(
            """
            SELECT count(*) AS pages, min(data_fetched_at) AS oldest_data,
                   max(fetched_at) AS last_fetch,
                   count(*) FILTER (WHERE parse_error IS NOT NULL) AS pages_with_errors
            FROM prydwen.page
            """
        )
        errors = await _query(
            "SELECT url, parse_error FROM prydwen.page WHERE parse_error IS NOT NULL ORDER BY url"
        )
        e7bot = await _query(
            """
            SELECT (SELECT max(fetched_at) FROM e7bot.document) AS data_fetched_at,
                   (SELECT count(*) FROM e7bot.save) AS leaderboard_saves,
                   latest.finished_at AS last_attempt, latest.ok AS last_attempt_ok,
                   latest.error
            FROM (SELECT 1) one
            LEFT JOIN LATERAL (
                SELECT * FROM e7bot.run ORDER BY started_at DESC LIMIT 1
            ) latest ON true
            """
        )
        stove = await _query(
            """
            SELECT (SELECT count(*) FROM stove.article) AS posts,
                   (SELECT max(fetched_at) FROM stove.article) AS data_fetched_at,
                   latest.finished_at AS last_attempt, latest.ok AS last_attempt_ok,
                   latest.error
            FROM (SELECT 1) one
            LEFT JOIN LATERAL (
                SELECT * FROM stove.run ORDER BY started_at DESC LIMIT 1
            ) latest ON true
            """
        )
        game8 = await _query(
            """
            SELECT (SELECT count(*) FROM game8.page) AS pages,
                   (SELECT max(fetched_at) FROM game8.page) AS data_fetched_at,
                   latest.finished_at AS last_attempt, latest.ok AS last_attempt_ok,
                   latest.error
            FROM (SELECT 1) one
            LEFT JOIN LATERAL (
                SELECT * FROM game8.run ORDER BY started_at DESC LIMIT 1
            ) latest ON true
            """
        )
        gamefiles = await _query(
            """
            SELECT (SELECT min(build) FROM gamefiles.file) AS build,
                   (SELECT count(*) FROM gamefiles.file) AS files,
                   (SELECT max(fetched_at) FROM gamefiles.file) AS data_fetched_at,
                   latest.finished_at AS last_attempt, latest.ok AS last_attempt_ok,
                   latest.error
            FROM (SELECT 1) one
            LEFT JOIN LATERAL (
                SELECT * FROM gamefiles.run ORDER BY started_at DESC LIMIT 1
            ) latest ON true
            """
        )
        status = {
            "prydwen": summary[0] | {"errors": errors},
            "e7bot": e7bot[0],
            "stove": stove[0],
            "game8": game8[0],
            "gamefiles": gamefiles[0],
        }
        return JSONResponse(json.loads(json.dumps(status, default=str)))

    async def _plan(found: dict) -> dict:
        """character_guide's plan part for the character row found."""
        slug = {"slug": found["slug"]}
        season_rows = await _query(
            "SELECT max(season) AS season, count(*) AS decks FROM leaderboard_deck"
            " WHERE character_slug = %(slug)s"
            " AND season = (SELECT max(season) FROM leaderboard_entry)",
            slug,
        )
        season = season_rows[0].get("season") if season_rows else None
        params = slug | {"season": season}
        cards = await _query(PLAN_CARDS_SQL, params)
        removals = await _query(PLAN_REMOVALS_SQL, params)
        equipment = await _query(PLAN_EQUIPMENT_SQL, params)
        decks = await _query(
            "SELECT name, code, farming_zone, partner, equipment, fragments, cards,"
            f" {DECK_NOTES} AS notes, url FROM deck WHERE character_slug = %(slug)s",
            slug,
        )
        starting = {c["card"] for c in cards if c.get("category") == "starting"}
        starting |= {r["card"] for r in removals}
        for deck in decks:
            if duplicates := _deck_duplicates(deck.get("cards") or [], starting):
                deck["duplicates"] = duplicates
            held = [
                DeckCard(
                    card=card["name"],
                    divine=bool(card.get("divine_epiphany")),
                    copies=card.get("count") or 1,
                )
                for card in deck.get("cards") or []
            ]
            deck["faint_memory"] = await _cost(found["slug"], held, [])
        plan = {
            "season": season,
            "leaderboard_decks": season_rows[0].get("decks", 0) if season_rows else 0,
            "guide_decks": decks,
            "cards": cards,
            "removals": removals,
            "equipment": equipment,
        }
        if not cards:
            plan["note"] = (
                "No leaderboard or guide deck uses this character yet, so there is nothing"
                " to plan from."
            )
        return plan

    async def _deck_card(slug: str, name: str) -> dict | None:
        """The best-matching card of this character or with no character (Neutral, Monster,
        Forbidden), with its base card's starting deck copies."""
        found = await _query(
            f"""
            SELECT name, base_name, category, color, tags,
                   (SELECT b.starting_copies FROM card b
                    WHERE b.base_name = card.base_name AND b.variant = ''
                      AND b.character_slug IS NOT DISTINCT FROM card.character_slug
                    LIMIT 1) AS starting_copies
            FROM card
            WHERE (character_slug = %(slug)s OR character_slug IS NULL) AND {NAME_MATCH}
            ORDER BY {NAME_ORDER} LIMIT 1
            """,
            {"slug": slug, "name": name},
        )
        return found[0] if found else None

    async def _cost(slug: str, cards: list[DeckCard], removed: list[str]) -> dict:
        entries = [
            (card, await _deck_card(slug, f"{card.card} {card.version or ''}".strip()))
            for card in cards
        ]
        removals = [(name, await _deck_card(slug, name)) for name in removed]
        return _save_data_cost(entries, removals)

    @tool
    async def save_data_cost(
        character: str, cards: list[DeckCard], removed: list[str] | None = None
    ) -> dict | None:
        """Faint Memory points of one character's Save Data, by the game's Save Rules for Faint
        Memories. Use it for every point total or a pick's cost; never add points up yourself.
        cards: the deck's cards (the whole deck, or only those gained or changed), one entry
        per version: card (name as
        shown, may include the Epiphany numeral), version (I-V, when not in the name), divine
        (every copy in the entry carries a Divine Epiphany), copies (how many copies of that
        version the deck holds, starting copies included). Copies of a card beyond the starting
        deck's (one for a card it does not start with) are Duplicates, across versions. removed:
        removed card names. lines: each entry's points and the rules that set them. A pick's
        cost is the total with the pick minus the total without it."""
        found = await _find_character(character)
        if found is None:
            return None
        return {"character": found["name"]} | await _cost(found["slug"], cards, removed or [])

    @mcp.custom_route("/save-data-cost", methods=["POST"])
    async def save_data_cost_route(request: Request) -> JSONResponse:
        """REST endpoint for programs: save_data_cost's input as a JSON body, its result as
        JSON."""
        if not await authorized(request):
            return JSONResponse({"error": "invalid or missing Bearer token"}, 401)
        try:
            body = await request.json()
            character = str(body["character"])
            cards = [DeckCard.model_validate(card) for card in body.get("cards") or []]
            removed = [str(name) for name in body.get("removed") or []]
        except (ValueError, KeyError, TypeError, ValidationError) as error:
            return JSONResponse({"error": f"invalid body: {error}"}, 400)
        found = await _find_character(character)
        if found is None:
            return JSONResponse({"error": f"no character matches {character}"}, 404)
        return JSONResponse(
            {"character": found["name"]} | await _cost(found["slug"], cards, removed)
        )

    @mcp.custom_route("/deck-details", methods=["POST"])
    async def deck_details_route(request: Request) -> JSONResponse:
        """REST endpoint for programs that draw a deck: matches card names (of the character's
        cards or cards with no character, an Epiphany numeral naming the version) and equipment
        names, in order, to the game's data and pictures; null where nothing matches."""
        if not await authorized(request):
            return JSONResponse({"error": "invalid or missing Bearer token"}, 401)
        try:
            body = await request.json()
            character = str(body["character"])
            cards = [str(name) for name in body.get("cards") or []]
            equipment = [str(name) for name in body.get("equipment") or []]
        except (ValueError, KeyError, TypeError) as error:
            return JSONResponse({"error": f"invalid body: {error}"}, 400)
        found = await _find_character(character)
        if found is None:
            return JSONResponse({"error": f"no character matches {character}"}, 404)
        card_rows = [await _query(DECK_CARD_SQL, {"slug": found["slug"], "name": n}) for n in cards]
        gear = []
        for name in equipment:
            rows = await _query(DECK_GEAR_SQL, {"name": name})
            item = rows[0] if rows else None
            if item and (background := GEAR_BACKGROUND.get(item["rarity"])):
                item |= {"background_url": ASSETS + background}
            gear.append(item)
        return JSONResponse(
            {
                "character": found["name"],
                "cards": [rows[0] if rows else None for rows in card_rows],
                "equipment": gear,
            }
        )

    @tool
    async def get_encounter(
        name: str, choices: list[str] | None = None, chaos: str | None = None
    ) -> dict | None:
        """One screen of a Chaos event (question-mark node). name: the event title on screen;
        most event screens show none, so pass the screen's first line of text instead, never a
        name from a choice's [brackets], which is a screen that choice opens.
        choices: the choice texts on screen, as shown (cut-off text and a leading [Tag] are
        fine); pass them on every lookup of a screen in front of the player. They find the
        screen, including follow-up screens whose title is not an event name, and the result
        covers only those choices. Look up each new screen the same way.
        Without choices: each version of the event (the game picks one per run) with its
        first screen in short form, versions with the same first screen sharing one entry;
        version numbers the versions it covers. opened_from numbers versions whose choices
        lead to this one; replace_link_from numbers versions with a screen whose
        replace_link_encounter_id names it. follow_up: the name matched a later screen's
        title, not the event's.
        With choices, screen is the screen's own title when it differs from the event's.
        A screen: step (its number within the event), shows (how many choices the screen
        offers; left out when conditions decide it), auto_choice (a choice the game takes
        without asking), appears_when (game condition codes), then choices: kind (battle, info, end; none is a plain choice),
        random_group (the game draws random choices per group; undrawn ones are not on
        screen), copies (identical choices listed more than once), dice (skill tag and
        target), requires (what picking it needs; a game condition code when the game gives
        no text), appears_when, and results: outcome success or failure (a dice choice has
        both), text, battle, rewards (text and class: copy, removal, epiphany,
        divine_epiphany, equipment, card, or other), and next: the screen it opens, in short
        form, listing every choice that screen can offer. A short choice: random (it appears only if drawn: the screen shows N of the
        M random choices, or may not appear when conditions decide the count),
        appears_when, dice, requires, gives (rewards on success), fails (on failure), leads_on (it opens
        a further screen). Fields named in the game's own snake_case are passed on unchanged
        from the game data; their meaning is unconfirmed. other_matches names other events
        with a screen offering the same choices. chaos narrows to events that can appear in
        that Chaos."""
        chaos_name = await _resolve_chaos(chaos)
        params = {"name": name, "chaos": chaos_name}
        if not choices:
            versions = await _query(ENCOUNTER_VERSIONS_SQL, params)
            if not versions:
                return None
            steps = await _encounter_step_rows([s for v in versions for s in v["steps"]])
            # Versions with the same first screen are listed once, numbered by their order.
            overview: dict[str, dict] = {}
            for number, v in enumerate(versions, 1):
                start = v["steps"][0]
                preview = _screen_preview(steps[start])
                links = {
                    "opened_from": [
                        n for n, o in enumerate(versions, 1) if start in o["steps"][1:]
                    ],
                    "replace_link_from": [
                        n
                        for n, o in enumerate(versions, 1)
                        if o is not v
                        and any(
                            steps[s].get("extra", {}).get("replace_link_encounter_id") == start
                            for s in o["steps"]
                        )
                    ],
                }
                shown = overview.setdefault(
                    json.dumps(preview, sort_keys=True, default=str), {"version": []} | preview
                )
                shown["version"].append(number)
                shown |= {key: numbers for key, numbers in links.items() if numbers}
            title = versions[0]["title"]
            return _present(
                {
                    "name": versions[0]["name"],
                    "follow_up": f"{title} is a later screen of this event; pass its choices"
                    if title != versions[0]["name"]
                    else None,
                    "versions": list(overview.values()),
                }
            )
        keys = [_choice_key(text) for text in choices]
        patterns = [re.sub(r"([\\%_])", r"\\\1", key) + "%" for key in keys]
        found = await _query(ENCOUNTER_SCREEN_SQL, params | {"choices": patterns})
        if not found:
            return None
        match = found[0]
        steps = await _encounter_step_rows([match["step_id"]])
        screen = steps[match["step_id"]]
        shown = [
            choice
            for choice in screen.get("choices") or []
            if any(_choice_key(choice["text"]).startswith(key) for key in keys)
        ]
        following = await _encounter_step_rows(
            [r["next_step"] for c in shown for r in c["results"] if r["next_step"]]
        )
        others = list(
            dict.fromkeys(row["name"] for row in found[1:] if row["name"] != match["name"])
        )
        return _present(
            {
                "name": match["name"],
                "screen": screen.get("name") if screen.get("name") != match["name"] else None,
                "step": match["step"],
                "shows": _shows(screen),
                "auto_choice": screen.get("auto_choice"),
                "appears_when": screen.get("appears_when"),
            }
            | screen.get("extra", {})
            | {
                "choices": _full_choices(shown, match["step_id"], following),
                "other_matches": others,
            }
        )

    @tool(name="list")
    async def list_data(
        kind: ListKind,
        character: str | None = None,
        category: str | None = None,
        type: str | None = None,
        tag: str | None = None,
        color: str | None = None,
        base_only: bool | None = None,
        slot: str | None = None,
        rarity: str | None = None,
        name: str | None = None,
        chaos: str | None = None,
        reward: Reward | None = None,
        cost: int | None = None,
        god: str | None = None,
        gender: str | None = None,
        element: str | None = None,
        character_class: str | None = None,
        offset: int | None = None,
    ) -> dict:
        """Every item of one kind, optionally filtered; each kind takes only its own filters.
        - cards (up to 120): character; category (starting: in the starting deck, unique:
          gained in a run, ego: the Ego skill cast with EP and not in the deck, collapse:
          replaces a card when the character's Collapse triggers, generated: created by other
          cards, neutral, monster, forbidden); type (Attack, Skill, Upgrade, ...); tag (a card
          tag such as Exhaust); color (the rarity color players name cards by: White, Blue,
          Purple, Gold, Orange); base_only (default true) skips Epiphany and other versions.
        - equipment (30 per offset): slot (Weapon, Armor, Trinket), rarity (Rare, Legendary,
          ...), name, chaos (only what drops in that Chaos); with effect text and where it
          drops (obtained_from), and stats per level when name is given.
        - characters: gender (female, male), element (Passion, Justice, Order, Void,
          Instinct), character_class (Striker, Vanguard, Ranger, Hunter, Controller,
          Psionic), rarity (4 or 5).
        - events (30 per offset): Chaos events, by chaos (those that can appear in it) and
          reward (events with an answer giving that class); copies first, then removals. Each
          row is one version of an event with the answers giving the class (or copies and
          removals) and step, the screen they are on (1 is the first).
        - divine_epiphanies (30 per offset): by type (Attack, Skill, Upgrade) and cost (the
          card's), or god (Vitor, Caligo, Circen, Diallos, Nihilum, Secred); each with effect
          text, the card types and costs it applies to (applies_to), and the gods offering it.
        - banners: gacha banners live or upcoming (Global server), live first, each group
          ending soonest first: kind (character or partner), rarity, rerun, season phase,
          start and end (UTC), featured rate-ups, and the 4-star secondary rate-ups."""
        filters = {
            "character": character,
            "category": category,
            "type": type,
            "tag": tag,
            "color": color,
            "base_only": base_only,
            "slot": slot,
            "rarity": rarity,
            "name": name,
            "chaos": chaos,
            "reward": reward,
            "cost": cost,
            "god": god,
            "gender": gender,
            "element": element,
            "character_class": character_class,
            "offset": offset,
        }
        given = {filter_name for filter_name, value in filters.items() if value is not None}
        if wrong := given - LIST_FILTERS[kind]:
            valid = ", ".join(sorted(LIST_FILTERS[kind])) or "none"
            raise ToolError(f"{kind} takes no {', '.join(sorted(wrong))}; its filters: {valid}.")
        start = max(offset or 0, 0)
        match kind:
            case "cards":
                return await _list_cards(
                    character, category, type, tag, color, True if base_only is None else base_only
                )
            case "equipment":
                return await _list_equipment(slot, rarity, name, chaos, start)
            case "characters":
                rows = await _query(
                    LIST_CHARACTERS_SQL,
                    {
                        "gender": gender,
                        "element": element,
                        "class": character_class,
                        "rarity": int(rarity) if rarity else None,
                    },
                )
                return _reply(rows, len(rows), None)
            case "events":
                params = {"chaos": await _resolve_chaos(chaos), "reward": reward, "offset": start}
                return await _listing(LIST_ENCOUNTERS_SQL, params, offset=start)
            case "divine_epiphanies":
                return await _list_divine_epiphanies(type, cost, god, start)
            case "banners":
                return await _list_banners()

    async def _list_divine_epiphanies(
        card_type: str | None, cost: int | None, god: str | None, offset: int
    ) -> dict:
        return await _listing(
            f"""
            SELECT text, {DIVINE_APPLIES} AS applies_to, gods, rarities,
                   count(*) OVER () AS total
            FROM divine_epiphany
            WHERE (%(type)s::text IS NULL OR card_types = '{{}}'
                   OR %(type)s ILIKE ANY (card_types))
              AND (%(cost)s::int IS NULL OR %(cost)s BETWEEN cost_min AND cost_max
                   OR cost_min IS NULL)
              AND (%(god)s::text IS NULL OR %(god)s ILIKE ANY (gods))
            ORDER BY text, applies_to
            LIMIT {LIST_PAGE} OFFSET %(offset)s
            """,
            {"type": card_type, "cost": cost, "god": god, "offset": offset},
            offset=offset,
        )

    async def _resolve_chaos(name: str | None) -> str | None:
        """The Chaos name the data uses for name; an unknown name raises an error listing the
        known ones, so a wrong name never silently yields only the any-Chaos events."""
        if name is None:
            return None
        found = await _query(CHAOS_MATCH_SQL, {"name": name})
        if found:
            return found[0]["name"]
        known = [row["name"] for row in await _query("SELECT name FROM chaos ORDER BY name")]
        raise ToolError(f"Unknown Chaos {name!r}. Known: {', '.join(known)}")

    return mcp


class OidcTokenVerifier(TokenVerifier):
    """Validates JWT access tokens against the provider's published signing keys (JWKS)."""

    ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA")

    def __init__(self, issuer: str, audience: str, jwks_uri: str):
        self.issuer = issuer
        self.audience = audience
        self._keys = jwt.PyJWKClient(jwks_uri, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await anyio.to_thread.run_sync(self._keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(self.ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            log.info("rejected token", extra={"error": str(error)})
            return None
        scopes = claims.get("scope", "").split() or list(claims.get("scp", []))
        return AccessToken(
            token=token,
            client_id=claims.get("client_id") or claims.get("azp") or claims.get("sub", ""),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            subject=claims.get("sub"),
        )


def _discover_jwks_uri(issuer: str) -> str:
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)["jwks_uri"]


def serve_stdio() -> None:
    """Serve one local client over stdin/stdout. The client starts this process, so it needs
    no auth."""
    build_server(None, None).run("stdio")


def serve() -> None:
    public_url = os.environ.get("MCP_PUBLIC_URL")
    issuer = os.environ.get("OIDC_ISSUER")
    auth, verifier = None, None
    if issuer:
        if not public_url:
            raise SystemExit("MCP_PUBLIC_URL is required with OIDC_ISSUER")
        # Plain strings let AuthSettings keep URLs exactly as configured (issuer comparison
        # is exact, so an added trailing slash would break logins).
        auth = AuthSettings(
            issuer_url=issuer,
            resource_server_url=public_url,
            required_scopes=os.environ.get("OIDC_REQUIRED_SCOPES", "").split(),
            # Most OIDC providers put their own audience in `aud` instead of binding tokens to
            # the RFC 8707 resource, so the verifier checks `aud` instead.
            validate_token_resource=False,
        )
        verifier = OidcTokenVerifier(
            issuer, os.environ.get("OIDC_AUDIENCE", public_url), _discover_jwks_uri(issuer)
        )
    elif os.environ.get("MCP_ALLOW_UNAUTHENTICATED") != "true":
        raise SystemExit("set OIDC_ISSUER, or MCP_ALLOW_UNAUTHENTICATED=true for local use")

    # The ingress controls the Host header, so DNS-rebinding checks add nothing here.
    app = build_server(auth, verifier).streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
