"""MCP server that answers Chaos Zero Nightmare questions from the scraped database.

It only reads; give it a database role with SELECT rights. Every result carries the Prydwen
source URL and the time that data was fetched, so answers can be cited and checked for staleness.

Auth: the server is an OAuth 2.1 resource server. It validates JWT access tokens issued by an
OIDC provider (``OIDC_ISSUER``) and advertises that provider to MCP clients such as claude.ai,
which run the login flow themselves.
"""

import json
import logging
import os
import urllib.request

import anyio
import jwt
import psycopg
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Chaos Zero Nightmare (CZN) game data from prydwen.gg: characters, their cards and Epiphany
variants with Prydwen's ratings, potentials, Manifest Ego levels, neutral cards, equipment, memory
fragment sets, partners, keyword/effect definitions, and full-text search over every guide page.
Start with `lookup` for any name seen on screen, then use the specific tools. Cite `url` and
`data_fetched_at` when the answer depends on the data. Player state (owned characters, current
run) is not stored here; read it from the user's screenshots.
"""

LOOKUP_SQL = """
SELECT entity.*, page.data_fetched_at FROM (
    SELECT 'character' AS kind, name, concat_ws(' ', rarity || '*', element, class) AS summary,
           url FROM character
    UNION ALL
    SELECT 'card', name, concat_ws(' | ', type, 'cost ' || cost, text), url FROM card
    UNION ALL
    SELECT 'equipment', name, concat_ws(' | ', rarity, slot, text), url FROM equipment
    UNION ALL
    SELECT 'fragment_set', name, bonuses::text, url FROM fragment_set
    UNION ALL
    SELECT 'partner', name, concat_ws(' | ', rarity || '*', class, passive_text), url FROM partner
    UNION ALL
    SELECT 'effect', name, concat_ws(' | ', type, description), url FROM effect
) entity
JOIN page USING (url)
WHERE (%(kind)s::text IS NULL OR entity.kind = %(kind)s) AND {match}
ORDER BY word_similarity(%(name)s, name) DESC, similarity(name, %(name)s) DESC, name
LIMIT %(limit)s
"""

# Fuzzy name match: substring, or pg_trgm word similarity ("flicker blad" finds
# "Flicker Blade I"). Shared by every tool that resolves a name typed from a screenshot.
NAME_MATCH = "(name ILIKE '%%' || %(name)s || '%%' OR %(name)s <%% name)"
LOOKUP_SQL = LOOKUP_SQL.format(match=NAME_MATCH)


def _database_url() -> str:
    return os.environ["DATABASE_URL"]


async def _query(sql: str, params: dict | None = None) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(_database_url(), row_factory=dict_row) as conn:
        rows = await (await conn.execute(sql, params or {})).fetchall()
    return [_jsonable(row) for row in rows]


def _jsonable(row: dict) -> dict:
    """Drop null and empty fields to save model tokens; keep 0 and False, which carry meaning."""
    return {
        k: v.isoformat() if hasattr(v, "isoformat") else v
        for k, v in row.items()
        if v is not None and v != "" and v != [] and v != {}
    }


def build_server(auth: AuthSettings | None, verifier: TokenVerifier | None) -> MCPServer:
    mcp = MCPServer(
        "czn-data",
        instructions=INSTRUCTIONS,
        auth=auth,
        token_verifier=verifier,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    async def lookup(name: str, kind: str | None = None, limit: int = 10) -> list[dict]:
        """Fuzzy-find any named game entity, tolerant of typos and partial names.

        kind narrows the search: character, card, equipment, fragment_set, partner, effect.
        Effects are keywords and statuses (for example Exhaust, Retain, Vulnerable).
        """
        return await _query(LOOKUP_SQL, {"name": name, "kind": kind, "limit": min(limit, 50)})

    @mcp.tool()
    async def get_card(name: str) -> list[dict]:
        """Every version of the best-matching card: the base card and its Epiphany variants
        (I-V), each with full text, cost, tags, Prydwen's star rating and Epiphany review."""
        return await _query(
            """
            WITH best AS (
                SELECT base_name, character_slug FROM card
                WHERE """
            + NAME_MATCH
            + """
                ORDER BY word_similarity(%(name)s, name) DESC, similarity(name, %(name)s) DESC
                LIMIT 1
            )
            SELECT card.*, page.data_fetched_at FROM card JOIN best USING (base_name)
            JOIN page USING (url)
            WHERE card.character_slug IS NOT DISTINCT FROM best.character_slug
            ORDER BY variant
            """,
            {"name": name},
        )

    @mcp.tool()
    async def get_character(name: str) -> dict | None:
        """Full profile of the best-matching character: rarity, element, class, every card and
        Epiphany variant, potentials, Manifest Ego levels with reviews, and Prydwen's written
        guide sections (pros, cons, decks, teams, partners, fragments, equipment)."""
        found = await _query(
            """
            SELECT character.*, page.data_fetched_at FROM character JOIN page USING (url)
            WHERE slug = %(name)s OR """
            + NAME_MATCH
            + """
            ORDER BY slug = %(name)s DESC, word_similarity(%(name)s, name) DESC,
                similarity(name, %(name)s) DESC
            LIMIT 1
            """,
            {"name": name},
        )
        if not found:
            return None
        character = found[0]
        slug = {"slug": character["slug"]}
        character["cards"] = await _query(
            "SELECT * FROM card WHERE character_slug = %(slug)s ORDER BY category, base_name,"
            " variant",
            slug,
        )
        character["potentials"] = await _query(
            "SELECT position, name, text FROM potential WHERE character_slug = %(slug)s", slug
        )
        character["manifest_egos"] = await _query(
            "SELECT level, name, text, performance, review FROM manifest_ego"
            " WHERE character_slug = %(slug)s ORDER BY level",
            slug,
        )
        return character

    @mcp.tool()
    async def list_cards(
        character: str | None = None,
        category: str | None = None,
        type: str | None = None,
        tag: str | None = None,
        base_only: bool = True,
    ) -> list[dict]:
        """List cards with optional filters. character is a name or slug; category is starting,
        unique or neutral; type is Attack, Skill, Upgrade and so on; tag is a card tag such as
        Exhaust. base_only skips Epiphany variants."""
        return await _query(
            """
            SELECT card.name, card.category, card.cost, card.type, card.tags, card.text,
                   card.variant, card.rating, character.name AS character, card.url
            FROM card LEFT JOIN character ON character.slug = card.character_slug
            WHERE (%(character)s::text IS NULL OR character.slug = %(character)s
                   OR character.name ILIKE %(character)s)
              AND (%(category)s::text IS NULL OR card.category = %(category)s)
              AND (%(type)s::text IS NULL OR card.type ILIKE %(type)s)
              AND (%(tag)s::text IS NULL OR %(tag)s ILIKE ANY (card.tags))
              AND (NOT %(base_only)s OR card.variant = '')
            ORDER BY character.name NULLS LAST, card.name
            LIMIT 300
            """,
            {
                "character": character,
                "category": category,
                "type": type,
                "tag": tag,
                "base_only": base_only,
            },
        )

    @mcp.tool()
    async def list_equipment(slot: str | None = None, rarity: str | None = None) -> list[dict]:
        """List equipment, optionally by slot (Weapon, Armor, Trinket) or rarity (Rare,
        Legendary, ...), with effect text, stats per level and where it drops."""
        return await _query(
            """
            SELECT name, rarity, slot, text, stats, source, url FROM equipment
            WHERE (%(slot)s::text IS NULL OR slot ILIKE %(slot)s)
              AND (%(rarity)s::text IS NULL OR rarity ILIKE %(rarity)s)
            ORDER BY name LIMIT 300
            """,
            {"slot": slot, "rarity": rarity},
        )

    @mcp.tool()
    async def search_guides(query: str, limit: int = 5) -> list[dict]:
        """Full-text search over every scraped Prydwen page (guides, tier list, team database,
        character reviews). Returns the best pages with highlighted excerpts."""
        return await _query(
            """
            SELECT url, title, page.data_fetched_at,
                   ts_headline('english', body, q,
                       'MaxFragments=3, MaxWords=40, MinWords=10') AS excerpt
            FROM document JOIN page USING (url), websearch_to_tsquery('english', %(q)s) q
            WHERE search @@ q
            ORDER BY ts_rank(search, q) DESC LIMIT %(limit)s
            """,
            {"q": query, "limit": min(limit, 20)},
        )

    @mcp.tool()
    async def get_page(url: str) -> dict | None:
        """Full text of one scraped Prydwen page, by URL from search_guides or lookup."""
        rows = await _query(
            "SELECT url, title, body, page.data_fetched_at FROM document JOIN page USING (url)"
            " WHERE url = %(url)s",
            {"url": url},
        )
        return rows[0] if rows else None

    @mcp.tool()
    async def data_status() -> dict:
        """Freshness of the data: page counts, oldest and newest fetch times, and pages whose
        latest version failed to parse (their previous data is still served)."""
        summary = await _query(
            """
            SELECT count(*) AS pages, min(data_fetched_at) AS oldest_data,
                   max(fetched_at) AS last_fetch,
                   count(*) FILTER (WHERE parse_error IS NOT NULL) AS pages_with_errors
            FROM page
            """
        )
        errors = await _query(
            "SELECT url, parse_error FROM page WHERE parse_error IS NOT NULL ORDER BY url"
        )
        return summary[0] | {"errors": errors}

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
