"""MCP server that answers Chaos Zero Nightmare questions from the scraped database.

It only reads; give it a database role with SELECT rights. Tools read the merged tables that
``czn_data.build`` derives (plus Prydwen's stored pages for guide search), never a source schema
directly. Results read from a stored record carry its URL and the time that data was fetched,
so answers can be cited and checked for staleness.

Auth: the server is an OAuth 2.1 resource server. It validates JWT access tokens issued by an
OIDC provider (``OIDC_ISSUER``) and advertises that provider to MCP clients such as claude.ai,
which run the login flow themselves.
"""

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
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import embed
from .identity import MODEL as IDENTITY_MODEL
from .identity import Identifier

log = logging.getLogger(__name__)

IDENTIFY_MAX_BYTES = 10 * 1024 * 1024

LOOKUP_SQL = """
SELECT entity.* FROM (
    SELECT 'character' AS kind, name, concat_ws(' ', rarity || '*', element, class) AS summary,
           url, data_fetched_at FROM character
    UNION ALL
    -- A version's game name joins its card name, so "Flicker Blade (Unbound)" finds V first.
    SELECT 'card', concat_ws(' ', name, '(' || nullif(alias, '') || ')'),
           concat_ws(' | ', color, type, 'cost ' || cost, text), url, data_fetched_at FROM card
    UNION ALL
    SELECT 'equipment', name, concat_ws(' | ', rarity, slot, text), url, data_fetched_at
    FROM equipment
    UNION ALL
    SELECT 'fragment_set', name,
           (SELECT string_agg((b->>'pieces') || '-piece: ' || (b->>'text'), ' | ')
            FROM jsonb_array_elements(bonuses) b), url, data_fetched_at
    FROM fragment_set
    UNION ALL
    SELECT 'partner', name, concat_ws(' | ', rarity || '*', class, passive_text,
           'EGO ' || ego_name || ' (' || ego_cost || ' EP): ' || ego_text), url,
           data_fetched_at FROM partner
    UNION ALL
    SELECT 'effect', name, concat_ws(' | ', type, description), url, data_fetched_at
    FROM effect
    UNION ALL
    SELECT 'encounter', name, concat_ws(' | ', pack, array_to_string(chaos, ', '),
           CASE WHEN any_chaos THEN 'any Chaos' END,
           'gives ' || nullif(array_to_string(reward_classes, ', '), '')), url,
           data_fetched_at
    FROM encounter
    UNION ALL
    SELECT 'divine_epiphany', text,
           concat_ws(' | ', {divine_applies}, nullif(array_to_string(gods, ', '), '')), url,
           data_fetched_at
    FROM divine_epiphany
    UNION ALL
    SELECT 'chaos', name,
           'Chaos (run map); list_encounters, get_encounter, and list_equipment take this'
           ' name as chaos',
           NULL, NULL FROM chaos
) entity
, LATERAL (
    SELECT entity.kind = 'effect'
           AND to_tsvector('english', summary) @@ plainto_tsquery('english', %(name)s)
           AS described
) d
WHERE (%(kind)s::text IS NULL OR entity.kind = %(kind)s)
  AND ({match} OR d.described)
-- Names holding the query first, then effects whose description uses it, then fuzzy name
-- matches: a loose spelling match ("Ionization" for "Polarization") must not outrank an effect
-- that is about the term.
ORDER BY name ILIKE '%%' || %(name)s || '%%' DESC, d.described DESC, {match} DESC, {order}, name
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

# get_character section name -> query for that part of one character's data (%(slug)s).
CHARACTER_SECTIONS = {
    # Reviews are left out to keep this small; get_card returns them for one card.
    "cards": "SELECT category, name, variant, alias, color, cost, type, tags, text, rating,"
    " target_neutral FROM card WHERE character_slug = %(slug)s ORDER BY category, base_name, variant",
    "potentials": "SELECT position, name, text FROM potential WHERE character_slug = %(slug)s",
    "manifest_egos": "SELECT level, name, text, performance, review FROM manifest_ego"
    " WHERE character_slug = %(slug)s ORDER BY level",
    "teams": "SELECT name, tier, members FROM team WHERE character_slug = %(slug)s",
    "decks": "SELECT name, code, farming_zone, partner, equipment, fragments, cards, notes"
    " FROM deck WHERE character_slug = %(slug)s",
    "equipment": "SELECT tab, best, slots, notes FROM character_equipment"
    " WHERE character_slug = %(slug)s",
    "seasonal_powers": "SELECT season, best, text FROM seasonal_power"
    " WHERE character_slug = %(slug)s",
    "fragments": "SELECT build, position, sets, performance, notes FROM fragment_rank"
    " WHERE character_slug = %(slug)s ORDER BY build, position",
    "partners": "SELECT position, partner, dupes, performance, notes FROM partner_rank"
    " WHERE character_slug = %(slug)s ORDER BY position",
    "synergies": "SELECT name, points FROM synergy WHERE character_slug = %(slug)s",
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
SELECT item.name AS item, c.name AS character,
       array_agg(DISTINCT concat_ws(': ', r.place, r.detail)) AS places, r.url
FROM item, LATERAL (
    SELECT character_slug, 'deck' AS place, name AS detail, url FROM deck
    WHERE item.guide_name = ANY (equipment) OR item.guide_name = ANY (fragments)
       OR partner = item.guide_name
    UNION ALL
    SELECT character_slug, 'equipment tab', tab, url FROM character_equipment
    WHERE EXISTS (
        SELECT FROM jsonb_each(slots) slot, jsonb_array_elements_text(slot.value) listed
        WHERE listed = item.guide_name
    )
    UNION ALL
    SELECT character_slug, 'fragment ranking',
           concat_ws(' ', build, '#' || position, performance), url FROM fragment_rank
    WHERE item.guide_name = ANY (sets)
    UNION ALL
    SELECT character_slug, 'partner ranking',
           concat_ws(' ', '#' || position, dupes, performance), url FROM partner_rank
    WHERE partner = item.guide_name
) r
JOIN character c ON c.slug = r.character_slug
GROUP BY item.name, c.name, r.url
ORDER BY count(*) DESC, c.name
"""
RECOMMENDED_BY_SQL = RECOMMENDED_BY_SQL.format(match=NAME_MATCH, order=NAME_ORDER)

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

# plan_deck: one character's decks are its Save Data decks in the newest leaderboard season
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
          AND listed.base IN (base_name, prydwen_name)
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
       jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
           'variant', coalesce(nullif(picks.variant, ''), 'base'),
           'rating', r.rating,
           'leaderboard_share', round(picks.lb_decks::numeric / nullif(total.n, 0), 2),
           'prydwen_decks', picks.pd_decks,
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
       nullif(count(DISTINCT deck) FILTER (WHERE worn.source = 'prydwen'), 0) AS prydwen_decks
FROM worn LEFT JOIN equipment e ON e.name = worn.item
GROUP BY worn.item
ORDER BY leaderboard_share DESC NULLS LAST, prydwen_decks DESC NULLS LAST, worn.item
LIMIT 20
"""

# Events that can appear in a Chaos (%(chaos)s, an exact name from the chaos view): those
# listed for it, plus common events of every Chaos.
_IN_CHAOS = "(%(chaos)s::text IS NULL OR e.any_chaos OR %(chaos)s = ANY (e.chaos))"
ENCOUNTER_MATCH_SQL = f"""
SELECT e.id, e.name, e.rarity, e.pack, e.chaos, e.any_chaos, e.reward_classes, e.url,
       e.data_fetched_at
FROM encounter e
WHERE {NAME_MATCH} AND {_IN_CHAOS}
ORDER BY {NAME_ORDER}, e.id
LIMIT 1
"""
# Rows past LIST_LIMIT are cut; total says how many matched.
LIST_LIMIT = 60
# Characters of page text per get_page call; get_page's description states the number.
PAGE_CHARS = 15_000
# Rows per call of the list tools that page (a page of Divine Epiphanies is about 10,000
# characters); their descriptions state the number.
LIST_PAGE = 30

# Sections closest in meaning that search_posts merges with its word matches.
MEANING_CANDIDATES = 20
# Weight of the newest-first ranking next to the two relevance rankings (each weight 1).
RECENCY_WEIGHT = 0.5
# Words players use that the official posts never do, mapped to the heading words the posts use
# instead; sections with such a heading form a third ranking. The meaning search catches most
# rewording; these are words it ranked too low (audit, 2026-11-20). Add one when an audit finds
# another miss.
POST_SYNONYMS = {
    "ban": "season restrictions",
    "banned": "season restrictions",
    "bans": "season restrictions",
    "shop": "supply store",
    "shops": "supply store",
}


def _synonym_headings(query: str) -> str | None:
    """A websearch query for headings naming what the query's player words mean."""
    words = re.findall(r"\w+", query.lower())
    phrases = dict.fromkeys(POST_SYNONYMS[w] for w in words if w in POST_SYNONYMS)
    return " or ".join(f'"{phrase}"' for phrase in phrases) or None


LIST_ENCOUNTERS_SQL = f"""
SELECT e.name, e.pack, e.chaos, e.any_chaos, e.reward_classes,
       (SELECT jsonb_agg(jsonb_build_object('stage', o.stage, 'answer', o.text,
                                            'gives', o.reward_classes)
                         ORDER BY o.stage, o.position)
        FROM encounter_option o
        WHERE o.encounter_id = e.id
          AND o.reward_classes && CASE WHEN %(reward)s::text IS NULL
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
    return [_jsonable(row) for row in rows]


def _jsonable(row: dict) -> dict:
    """Drop null and empty fields to save model tokens; keep 0 and False, which carry meaning.
    Also drop source (which scraper a merged row came from): it is build bookkeeping that
    tools selecting whole rows would otherwise pass to the model."""
    return {
        k: v.isoformat() if hasattr(v, "isoformat") else v
        for k, v in row.items()
        if k != "source" and v is not None and v != "" and v != [] and v != {}
    }


def build_server(auth: AuthSettings | None, verifier: TokenVerifier | None) -> MCPServer:
    mcp = MCPServer(
        "czn-data",
        auth=auth,
        token_verifier=verifier,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    identifier = Identifier()
    # One screenshot at a time: matching is CPU-bound, and parallel runs would each add the
    # models' working memory.
    identifying = anyio.Lock()

    @mcp.custom_route("/identify", methods=["POST"])
    async def identify(request: Request) -> JSONResponse:
        """REST endpoint for the companion app: names the characters on a game screenshot (see
        ``czn_data.identity``). The body is the JPEG or PNG; the Bearer token is the same one
        the MCP endpoint takes. Custom routes are outside the MCP auth middleware, so the token
        is checked here."""
        if verifier is not None:
            token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not token or await verifier.verify_token(token) is None:
                return JSONResponse({"error": "invalid or missing Bearer token"}, 401)
        if request.headers.get("content-type") not in ("image/jpeg", "image/png"):
            return JSONResponse({"error": "send the screenshot as image/jpeg or image/png"}, 400)
        image = await request.body()
        if len(image) > IDENTIFY_MAX_BYTES:
            return JSONResponse({"error": "screenshot larger than 10 MB"}, 413)
        rows = await _query(
            """
            SELECT c.name, i.print FROM character c
            JOIN prydwen.portrait_identity i ON i.image_url = c.image_url AND i.model = %(model)s
            """,
            {"model": IDENTITY_MODEL},
        )
        prints = {row["name"]: row["print"] for row in rows}
        started = time.monotonic()
        try:
            async with identifying:
                figures = await anyio.to_thread.run_sync(identifier.identify, image, prints)
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

    @mcp.tool()
    async def lookup(name: str, kind: LookupKind | None = None, limit: int = 10) -> list[dict]:
        """Fuzzy-find any named game entity, tolerant of typos and partial names. Use it first
        for any name read from the screen, then the specific tools for details.

        kind narrows the search: character, card, equipment, fragment_set, partner, effect
        (keyword, status and tag are other names for it), encounter (Chaos event),
        divine_epiphany, or chaos (the Chaos maps, for names list_encounters accepts).
        Effects are keywords, statuses and card tags (for example Exhaust, Retain, Vulnerable);
        a term without its own entry, such as Polarization, finds the effects whose description
        uses it. Card summaries start with the card's color (its rarity in game: White, Blue,
        Purple, Gold, Orange). limit is at most 50.
        Season content is not in the game data this searches: season events and Interaction
        Events (such as Elaborate Illusion), Chaos difficulty modifiers (such as Gangrene),
        and Environment Variables nodes. When lookup finds nothing for such a name, use
        search_posts, newest post first, since values change between seasons.
        """
        kind = LOOKUP_KIND_ALIASES.get(kind, kind) if kind else None
        return await _query(LOOKUP_SQL, {"name": name, "kind": kind, "limit": min(limit, 50)})

    @mcp.tool()
    async def get_partner(name: str) -> dict | None:
        """The best-matching partner: rarity, class, passive, EGO skill (name, EP cost, text)
        and base stats. Use recommended_by for which characters' guides rank it. The
        Epiphanies a partner unlocks are in neither source."""
        found = await _query(
            f"SELECT * FROM partner WHERE {NAME_MATCH} ORDER BY {NAME_ORDER} LIMIT 1",
            {"name": name},
        )
        return found[0] if found else None

    @mcp.tool()
    async def get_card(name: str) -> list[dict]:
        """Every version of the best-matching card: the base card and its Epiphany variants
        (I-V), each with full text, cost, tags, Prydwen's star rating (1 to 5) and its own
        review; alias is the name the game shows for a version, when it has one (Unbound in
        "Flicker Blade (Unbound)"). The base card also carries the overall review, the recommended Neutral Epiphany
        (target_neutral) and Prydwen's Neutral and Divine advice."""
        return await _query(
            f"""
            WITH best AS (
                SELECT base_name, character_slug FROM card
                WHERE {NAME_MATCH} OR prydwen_name ILIKE %(name)s
                ORDER BY coalesce(prydwen_name ILIKE %(name)s, false) DESC, {NAME_ORDER}
                LIMIT 1
            )
            SELECT card.* FROM card JOIN best USING (base_name)
            WHERE card.character_slug IS NOT DISTINCT FROM best.character_slug
            ORDER BY variant
            """,
            {"name": name},
        )

    @mcp.tool()
    async def get_character(name: str, section: str | None = None) -> dict | None:
        """Profile of the best-matching character. Without section: rarity, element, class,
        gender (from the game data; for characters only Prydwen has, judged from the art by an
        image tagger), and the list of available sections. With section, that part of
        Prydwen's guide: cards (without reviews, use get_card; alias is the name the game
        shows for a version, when it has one), potentials, manifest_egos,
        teams, decks (with the deck code players paste into the game's deck import, when
        Prydwen gives one, and the Chaos it farms), equipment (per season tab),
        seasonal_powers, fragments, partners, synergies, or a prose section by title (Pros,
        Cons, Introduction, Key Mechanics, Stats, Potential Priority, ...). The teams and
        synergies sections hold only this character's own guide page; synergies also gathers
        other guide pages, and find_teams other guide pages and the leaderboard. A section
        reply names the character but repeats no profile.
        note explains missing data, such as a character neither source covers yet."""
        character = await _find_character(name)
        if character is None:
            return None
        prose = character.pop("sections", [])
        slug = {"slug": character["slug"]}
        counts = (await _query(_SECTION_COUNTS_SQL, slug))[0]
        if not counts.get("cards"):
            character["note"] = (
                "No cards in either source yet: the game data (e7bot) does not have this"
                " character, and Prydwen's page has no card data."
            )
        if section is None:
            available = [s for s, n in counts.items() if n]
            return character | {"sections": available + [s["title"] for s in prose]}
        header = {
            k: character[k]
            for k in ("name", "slug", "prydwen_url", "data_fetched_at", "note")
            if k in character
        }
        key = section.strip().lower()
        if key in CHARACTER_SECTIONS:
            return header | {key: await _query(CHARACTER_SECTIONS[key], slug)}
        found = [s for s in prose if key in s["title"].lower()]
        if found:
            return header | {"sections": found}
        available = [s for s, n in counts.items() if n] + [s["title"] for s in prose]
        missing = f"No section {section!r}. Sections: {', '.join(available)}."
        return header | {"note": " ".join(filter(None, [header.get("note"), missing]))}

    @mcp.tool()
    async def match_card_text(
        texts: list[str], character: str | None = None, limit: int = 3
    ) -> list[dict]:
        """Find the card and Epiphany variant whose effect text best matches each text read from
        the screen (the game shows Epiphany choices as text, without variant numbers); send
        every card's text on a screen in one call. Numbers in the text decide between versions
        that differ only in them; a screen value raised by an upgrade (360% for a stored 300%)
        lowers match. character narrows the search to one character's cards (name or slug,
        typos tolerated). Returns one item per text, in order: the text and its matches, best
        first, at most limit (up to 10) each. match is 0 to 1; alias is the name the game shows
        for that version, when it has one (Unbound in "Flicker Blade (Unbound)")."""
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

    @mcp.tool()
    async def find_teams(character: str) -> list[dict]:
        """Up to 30 teams that include the best-matching character: first teams in Prydwen's
        guides (name, tier, members, listed_for: the characters whose pages list it), then
        lineups of the newest leaderboard season (season, members, teams: how many ranked teams
        used the lineup, best_rank), most used first. Use it for "which
        team should X be in"; synergies ranks single characters to pair with X, with reasons;
        get_character section teams lists only the teams on X's own guide page."""
        return await _query(
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
                SELECT 'prydwen' AS source, team.name, team.tier, team.members,
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

    @mcp.tool()
    async def list_characters(
        gender: str | None = None,
        element: str | None = None,
        character_class: str | None = None,
        rarity: int | None = None,
    ) -> list[dict]:
        """Every character, one short line each (name, rarity, element, class, gender, card art
        image_url), optionally filtered: gender female or male, element (Passion, Justice,
        Order, Void, Instinct), character_class (Striker, Vanguard, Ranger, Hunter, Controller,
        Psionic), rarity 4 or 5. It cannot tell who a portrait on a game screen shows; the
        companion app names those from the screenshot."""
        return await _query(
            LIST_CHARACTERS_SQL,
            {"gender": gender, "element": element, "class": character_class, "rarity": rarity},
        )

    @mcp.tool()
    async def synergies(character: str) -> list[dict]:
        """Characters Prydwen's guides recommend using with the best-matching character, best
        supported first. sources says where each is named: the character's own synergy list
        (reasons gives Prydwen's explanation), the other character's synergy list
        (their_reasons), or recommended teams (teams, with tier). Use it for "who should I
        use with X"; find_teams gives whole lineups, including leaderboard ones."""
        return await _query(SYNERGIES_SQL, {"name": character})

    @mcp.tool()
    async def recommended_by(name: str) -> list[dict]:
        """Which characters' guides recommend the best-matching equipment item, memory
        fragment set, or partner, and where: deck, equipment tab, fragment or partner
        ranking. Use it for "who uses X" questions."""
        return await _query(RECOMMENDED_BY_SQL, {"name": name})

    async def _find_character(name: str) -> dict | None:
        found = await _query(
            f"""
            SELECT * FROM character WHERE slug = %(name)s OR {NAME_MATCH}
            ORDER BY slug = %(name)s DESC, {NAME_ORDER} LIMIT 1
            """,
            {"name": name},
        )
        return found[0] if found else None

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

    @mcp.tool()
    async def list_cards(
        character: str | None = None,
        category: str | None = None,
        type: str | None = None,
        tag: str | None = None,
        color: str | None = None,
        base_only: bool = True,
    ) -> dict:
        """List cards with optional filters; total counts every match, and note says when
        results shows only part of them. character is a name or slug (typos tolerated);
        category is starting (in the starting deck), unique (gained in a run), ego (the Ego
        skill, cast with EP, not in the deck), collapse (replaces a card when the character's
        Collapse triggers), generated (created by other cards), neutral, monster or
        forbidden; type is Attack, Skill, Upgrade and so on; tag is a card tag such as
        Exhaust; color is the card's rarity color (White, Blue, Purple, Gold, Orange), which
        players use to name cards ("her purple card"). base_only skips Epiphany and other
        versions; alias is the name the game shows for a version, when it has one."""
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
            listing["note"] = f"No cards for {found['name']} in either source yet."
        return listing

    @mcp.tool()
    async def list_equipment(
        slot: str | None = None,
        rarity: str | None = None,
        name: str | None = None,
        chaos: str | None = None,
        offset: int = 0,
    ) -> dict:
        """List equipment, optionally by slot (Weapon, Armor, Trinket; plurals work), rarity
        (Rare, Legendary, ...), name (typos tolerated) or chaos (a Chaos zone such as
        Celestial Bulwark, typos tolerated: only equipment that drops there), with effect text
        and where it drops (obtained_from); total counts every match; results holds 30 from
        offset, and note says how to read on. Stats per level are included when name is
        given. An unknown Chaos name is an error that lists the known ones."""
        offset = max(offset, 0)
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

    @mcp.tool()
    async def search_guides(query: str, limit: int = 5) -> list[dict]:
        """Full-text search over every scraped Prydwen page (guides, tier list, team database,
        character reviews). Returns the best pages with highlighted excerpts; get_page reads
        a page's text in parts. Use it for strategy and opinions the other tools do not cover;
        official news, events and patch notes are in search_posts, and how game systems and
        modes work in search_game_systems. limit is at most 20."""
        return await _query(
            """
            SELECT url, title, page.data_fetched_at,
                   ts_headline('english', body, q,
                       'MaxFragments=3, MaxWords=40, MinWords=10') AS excerpt
            -- Any query word matches (players name things the guides never do, such as the
            -- Policy Office). Pages with more of the query's words come first; ts_rank alone
            -- would put a page repeating one common word ("team") above one with every word.
            -- Ties rank by the all-words query, which favors the words appearing close
            -- together over one of them repeating.
            FROM prydwen.document JOIN prydwen.page USING (url),
                 CAST(replace(plainto_tsquery('english', %(q)s)::text, '&', '|') AS tsquery) q,
                 websearch_to_tsquery('english', %(q)s) every_word,
                 LATERAL (
                     SELECT count(*) AS matched
                     FROM unnest(tsvector_to_array(to_tsvector('english', %(q)s))) word
                     WHERE search @@ quote_literal(word)::tsquery
                 ) m
            WHERE search @@ q
            ORDER BY matched DESC, ts_rank(search, every_word) DESC, ts_rank(search, q) DESC
            LIMIT %(limit)s
            """,
            {"q": query, "limit": max(1, min(limit, 20))},
        )

    @mcp.tool()
    async def list_banners() -> list[dict]:
        """Gacha banners that are live or upcoming, from Prydwen's banner schedule (Global
        server): name, kind (character or partner), rarity, rerun, season phase, start and end
        times (UTC), featured rate-up characters, the 4-star secondary rate-ups, and status
        (live or upcoming). Live banners first, each group ending soonest first."""
        return await _query(
            """
            SELECT CASE WHEN starts_at <= now() THEN 'live' ELSE 'upcoming' END AS status,
                   name, kind, rarity, rerun, phase, starts_at, ends_at, featured, secondary,
                   url, data_fetched_at
            FROM banner WHERE ends_at > now()
            ORDER BY starts_at > now(), ends_at, starts_at, kind, name
            """
        )

    @mcp.tool()
    async def search_posts(
        query: str | None = None, active_on: str | None = None, limit: int = 5, offset: int = 0
    ) -> dict:
        """Search the game's official Stove posts (update notices, news and event overviews,
        current community events), split into sections. Use it for events, season content
        (Season Restrictions, Supply Store, Environment Variables), rate-up periods and
        rewards; list_banners gives the banner schedule itself. query: a question or words;
        matches the posts' words, titles and headings (typos tolerated), and sections close in
        meaning. active_on: a date (YYYY-MM-DD, or today) to get only sections whose period
        covers it, which answers "what events are on now". Give either or both. Each result
        has the post title, board, posted_at, the section heading ("■ Section > ◆
        Subsection"), its full text, its period (starts_on, ends_on) when the post gives one,
        and the post url. Images in posts (some calendars) are not included. total counts
        every match; offset pages through them (at most 20 per call)."""
        if query is None and active_on is None:
            raise ToolError("Give query, active_on, or both.")
        day = None
        if active_on is not None:
            text = active_on.strip().lower()
            try:
                day = datetime.now(UTC).date() if text == "today" else date.fromisoformat(text)
            except ValueError as error:
                raise ToolError(
                    f"active_on must be YYYY-MM-DD or today, not {active_on!r}"
                ) from error
        vector = await anyio.to_thread.run_sync(embed.query, query) if query else None
        # Two rankings merged by reciprocal rank fusion: sections matching the query's words
        # (or, with typos, a title or heading), and the sections closest in meaning.
        return await _listing(
            f"""
            WITH base AS (
                SELECT * FROM post_section
                WHERE %(day)s::date IS NULL OR %(day)s::date BETWEEN starts_on AND ends_on
            ),
            words AS (
                SELECT article_id, position,
                       row_number() OVER (ORDER BY ts_rank(search, q)
                           + greatest(word_similarity(%(name)s, post_title),
                                      word_similarity(%(name)s, coalesce(heading, '')))
                           DESC) AS rank
                -- Any query word matches (players' phrasing rarely uses every word a post
                -- does); ranking puts sections with more of them first.
                FROM base,
                     CAST(replace(plainto_tsquery('english', %(words)s)::text, '&', '|')
                          AS tsquery) q
                WHERE %(words)s::text IS NOT NULL
                  AND (search @@ q OR {_name_match("post_title")} OR {_name_match("heading")})
            ),
            meaning AS (
                SELECT article_id, position, row_number() OVER (ORDER BY similarity DESC) AS rank
                FROM base, LATERAL (
                    SELECT sum(a * b) AS similarity
                    FROM unnest(embedding, %(vector)s::real[]) AS pair(a, b)
                ) s
                WHERE %(vector)s::real[] IS NOT NULL
                ORDER BY similarity DESC NULLS LAST
                LIMIT {MEANING_CANDIDATES}
            ),
            synonyms AS (
                SELECT article_id, position,
                       row_number() OVER (ORDER BY posted_at DESC, position) AS rank
                FROM base
                WHERE to_tsvector('english', coalesce(heading, ''))
                      @@ websearch_to_tsquery('english', %(synonyms)s)
            ),
            relevance AS (
                SELECT article_id, position, sum(1.0 / (60 + rank)) AS score
                FROM (SELECT * FROM words UNION ALL SELECT * FROM meaning
                      UNION ALL SELECT * FROM synonyms) ranked
                GROUP BY article_id, position
            ),
            -- Newer posts win close calls: players mostly ask about the current season.
            fused AS (
                SELECT article_id, position,
                       score + {RECENCY_WEIGHT} / (60 + row_number() OVER (
                           ORDER BY posted_at DESC, position)) AS score
                FROM relevance JOIN base USING (article_id, position)
            )
            SELECT post_title, board, posted_at, heading, text, starts_on, ends_on, url,
                   data_fetched_at, count(*) OVER () AS total
            FROM base LEFT JOIN fused USING (article_id, position)
            WHERE %(words)s::text IS NULL OR fused.score IS NOT NULL
            ORDER BY fused.score DESC NULLS LAST, ends_on NULLS LAST, posted_at DESC, position
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "words": query,
                "synonyms": _synonym_headings(query) if query else None,
                "name": query,
                "vector": vector,
                "day": day,
                "limit": min(limit, 20),
                "offset": max(offset, 0),
            },
            offset=max(offset, 0),
        )

    @mcp.tool()
    async def search_game_systems(query: str, limit: int = 5, offset: int = 0) -> dict:
        """Search Game8's guides on how the game works, split into sections: game systems
        (Save Data, Distortion Adaptation, Epiphany, stress, Link Crash, potentials, gacha),
        Chaos modes and areas, Simulation and boss modes (Full-Scale Offensive), Ark City,
        events, seasons, missions, and a glossary. Use it for "how does X work" and "what is
        X"; characters, cards and equipment are in the other tools. query: a question or
        words; matches words, page titles and headings (typos tolerated), and sections close
        in meaning. Each result is one section: page_title, heading (its parent headings, "A
        > B"), text, page_summary (the page's opening), updated_at (when Game8 last updated
        the page), url, and offset and length (where text sits in get_page's body; read
        around it when a section refers to another one). Game8 pages can be out of date: the
        official posts (search_posts) win when they disagree. total counts every match;
        offset pages through them (at most 20 per call)."""
        vector = await anyio.to_thread.run_sync(embed.query, query)
        # Ranked like search_posts: words, titles and headings, and meaning, merged by
        # reciprocal rank fusion; newer pages win close calls.
        return await _listing(
            f"""
            WITH base AS (
                SELECT s.*, p.title, p.summary, p.updated_at, p.data_fetched_at
                FROM game8_section s JOIN game8_page p USING (url)
            ),
            words AS (
                SELECT url, position,
                       row_number() OVER (ORDER BY ts_rank(search, q)
                           + greatest(word_similarity(%(name)s, title),
                                      word_similarity(%(name)s, heading))
                           DESC) AS rank
                FROM base,
                     CAST(replace(plainto_tsquery('english', %(words)s)::text, '&', '|')
                          AS tsquery) q
                WHERE search @@ q OR {_name_match("title")} OR {_name_match("heading")}
            ),
            meaning AS (
                SELECT url, position, row_number() OVER (ORDER BY similarity DESC) AS rank
                FROM base, LATERAL (
                    SELECT sum(a * b) AS similarity
                    FROM unnest(embedding, %(vector)s::real[]) AS pair(a, b)
                ) s
                ORDER BY similarity DESC NULLS LAST
                LIMIT {MEANING_CANDIDATES}
            ),
            relevance AS (
                SELECT url, position, sum(1.0 / (60 + rank)) AS score
                FROM (SELECT * FROM words UNION ALL SELECT * FROM meaning) ranked
                GROUP BY url, position
            ),
            fused AS (
                SELECT url, position,
                       score + {RECENCY_WEIGHT} / (60 + row_number() OVER (
                           ORDER BY updated_at DESC NULLS LAST, position)) AS score
                FROM relevance JOIN base USING (url, position)
            )
            SELECT title AS page_title, heading, text, summary AS page_summary, updated_at,
                   url, "offset", length, data_fetched_at, count(*) OVER () AS total
            FROM base JOIN fused USING (url, position)
            ORDER BY fused.score DESC, url, position
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "words": query,
                "name": query,
                "vector": vector,
                "limit": max(1, min(limit, 20)),
                "offset": max(offset, 0),
            },
            offset=max(offset, 0),
        )

    @mcp.tool()
    async def get_page(url: str, offset: int = 0, length: int = PAGE_CHARS) -> dict | None:
        """Text of one scraped page, by URL: a prydwen.gg URL from search_guides or another
        tool (url or prydwen_url), or a game8.co URL from search_game_systems. Other URLs
        return nothing. Pages can run to 40,000 characters, so body holds at most length
        characters (up to 15,000) from offset (a character position); the reply's length is
        the whole page's, and note says how to read on. To read around a search_game_systems
        section, start a little before its offset (say 1,500) with a length of a few
        thousand."""
        rows = await _query(
            """
            SELECT url, title, body, page.data_fetched_at
            FROM prydwen.document JOIN prydwen.page USING (url) WHERE url = %(url)s
            UNION ALL
            SELECT url, title, body, data_fetched_at FROM game8_page WHERE url = %(url)s
            """,
            {"url": url},
        )
        if not rows:
            return None
        page = rows[0]
        body = page.pop("body", "")
        start = max(offset, 0)
        end = start + max(1, min(length, PAGE_CHARS))
        page |= {"length": len(body), "body": body[start:end]}
        if end < len(body):
            page["note"] = (
                f"Showing characters {start}-{end} of {len(body)}; offset={end} reads on."
            )
        return page

    @mcp.tool()
    async def data_status() -> dict:
        """Freshness of each source. prydwen: page counts, oldest and newest fetch times, and
        pages whose latest version failed to parse. e7bot: when the served game data was
        fetched, and the last attempt with its error. stove: official posts stored, the newest
        fetch, and the last attempt with its error. game8: Game8 guide pages stored, the newest
        fetch, and the last attempt with its error. Failed fetches never replace served data."""
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
        return {
            "prydwen": summary[0] | {"errors": errors},
            "e7bot": e7bot[0],
            "stove": stove[0],
            "game8": game8[0],
        }

    @mcp.tool()
    async def plan_deck(character: str) -> dict | None:
        """Plan the one character's deck to build this Chaos run (players build one per run;
        copy events are too rare for two). Uses the newest leaderboard season's Save Data decks
        and Prydwen's guide decks and ratings. cards: each card the decks use, best first, with
        category, cost and color, and per version (base or Epiphany I-V) Prydwen's rating, the
        share of leaderboard decks taking it, how many Prydwen decks list it, and common Divine
        Epiphanies. removals: up to 5 starting cards the decks drop (a deck can remove at most
        5), with the share of decks that drop each; the Ego skill is never one. equipment: each
        item with its slot, the share of leaderboard decks wearing it, and how many Prydwen
        decks list it. prydwen_decks are Prydwen's decks, each with its deck code (to paste
        into the game's deck import) when Prydwen gives one. Names are the game's; use
        get_card for card text."""
        found = await _find_character(character)
        if found is None:
            return None
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
            "SELECT name, code, farming_zone, partner, equipment, fragments, cards, notes, url"
            " FROM deck WHERE character_slug = %(slug)s",
            slug,
        )
        plan = {
            "character": found["name"],
            "season": season,
            "leaderboard_decks": season_rows[0].get("decks", 0) if season_rows else 0,
            "prydwen_decks": decks,
            "cards": cards,
            "removals": removals,
            "equipment": equipment,
        }
        if not cards:
            plan["note"] = (
                "No leaderboard or Prydwen deck uses this character yet, so there is nothing"
                " to plan from."
            )
        return plan

    @mcp.tool()
    async def get_encounter(name: str, chaos: str | None = None) -> dict | None:
        """The best-matching Chaos event (question-mark node) by name, with every stage it can
        reach. stages: each stage's answers with requirements, what each gives (rewards:
        outcome success or failure for dice answers, class copy, removal, epiphany,
        divine_epiphany, equipment, card or other, and text), and leads_to, the stage that
        answer opens. The event starts at stage 1; some events start at a random one of
        several stages that no answer leads to. chaos (typos tolerated) narrows to events that
        can appear in that Chaos."""
        params = {"name": name, "chaos": await _resolve_chaos(chaos)}
        found = await _query(ENCOUNTER_MATCH_SQL, params)
        if not found:
            return None
        options = await _query(
            "SELECT stage, text, requirements, rewards, reward_classes, leads_to"
            " FROM encounter_option WHERE encounter_id = %(id)s ORDER BY stage, position",
            {"id": found[0]["id"]},
        )
        stages: dict[int, list[dict]] = {}
        for option in options:
            stages.setdefault(option.pop("stage"), []).append(option)
        event = found[0]
        del event["id"]
        return event | {
            "stages": [{"stage": n, "answers": answers} for n, answers in stages.items()]
        }

    @mcp.tool()
    async def list_encounters(
        chaos: str | None = None,
        reward: Literal["copy", "removal", "epiphany", "divine_epiphany", "equipment", "card"]
        | None = None,
        offset: int = 0,
    ) -> dict:
        """Chaos events, optionally only those that can appear in one Chaos (by name, typos
        tolerated; lookup kind chaos lists the names) and those with an answer giving one
        reward class. Events offering card copies come first, then removals, since those
        decide a one-character deck. Each row names the answers (with their stage) that give
        the requested class (or copies and removals). total counts every match; results holds
        30 from offset, and note says how to read on. An unknown Chaos name is an error that
        lists the known ones."""
        offset = max(offset, 0)
        params = {"chaos": await _resolve_chaos(chaos), "reward": reward, "offset": offset}
        return await _listing(LIST_ENCOUNTERS_SQL, params, offset=offset)

    @mcp.tool()
    async def list_divine_epiphanies(
        card_type: str | None = None,
        cost: int | None = None,
        god: str | None = None,
        offset: int = 0,
    ) -> dict:
        """Divine Epiphanies that can be added to a card: optionally only those that apply to a
        card type (Attack, Skill, Upgrade) and a card cost, or that a god offers (Vitor,
        Caligo, Circen, Diallos, Nihilum, Secred). Each has its effect text, the card types and
        cost range it applies to (applies_to), and the gods that offer it; url and
        data_fetched_at, shared by all, are given once. total counts every match; results
        holds 30 from offset, and note says how to read on."""
        offset = max(offset, 0)
        reply = await _listing(
            f"""
            SELECT text, {DIVINE_APPLIES} AS applies_to, gods, rarities, url,
                   data_fetched_at, count(*) OVER () AS total
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
        for row in reply["results"]:
            for shared in ("url", "data_fetched_at"):
                if shared in row:
                    reply[shared] = row.pop(shared)
        return reply

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
