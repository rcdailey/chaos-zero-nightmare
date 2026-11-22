"""Ranking of the ``passage`` table, shared by every search tool.

A search runs in two stages. First, three cheap searches each nominate candidates: BM25 over
the passages' words (pg_textsearch, which weighs rare words above common ones), the passages
closest in meaning (embedding dot product), and titles or headings that match the query
loosely, which catches typos ("spiral towr"). Then a cross-encoder (``czn_data.rerank``) reads
the query with each candidate and scores how well the candidate answers it; that score orders
the results. Newer sources win close calls: a candidate gains up to FRESHNESS_WEIGHT by how
recent its source is among the candidates, and the game's own text counts as current.

``czn-data eval-search`` measures this ranking against ``search_eval.toml``; change the
constants here only with its numbers before and after.
"""

import anyio
import psycopg
from psycopg.rows import dict_row

from . import embed, rerank

# The passage sources each search tool ranks.
TOOL_SOURCES = {
    "search_posts": ("post",),
    "search_game_systems": ("help", "game8", "game_text"),
    "search_guides": ("guide",),
}
# Candidates each first-stage search nominates; the cross-encoder scores at most three times
# this many passages per query.
CANDIDATES = 30
# Most the freshest candidate gains on the cross-encoder's scale (logits; one relevant passage
# usually scores several points above an unrelated one).
FRESHNESS_WEIGHT = 1.0
# How closely the query spells a passage's title or heading (pg_trgm similarity, 0 to 1), so
# a query naming a topic, even misspelled ("Teatme Evnt"), finds it: passages at NAMED_MIN or
# more are candidates and gain NAMED_WEIGHT times it. The cross-encoder reads typos poorly.
NAMED_MIN = 0.3
NAMED_WEIGHT = 2.0
_NAMED = "greatest(similarity(%(query)s, title), coalesce(similarity(%(query)s, heading), 0))"

# Sources from the game's own files: they describe the build the game runs now, so they count as
# newer than every other source whatever their published_at (the time the files were fetched).
GAME_SOURCES = ("help", "game_text")


def _age(row: dict) -> tuple | None:
    """Sorts newest first among candidates; None for a passage without a date."""
    if row["source"] in GAME_SOURCES:
        return (0,)
    if row["published_at"] is None:
        return None
    return (1, -row["published_at"].timestamp())


_CANDIDATES_SQL = f"""
WITH pool AS (
    SELECT * FROM passage
    WHERE source = ANY(%(sources)s)
      AND (%(keys)s::text[] IS NULL OR (key, position) IN (
          SELECT * FROM unnest(%(keys)s::text[], %(positions)s::int[])))
),
words AS (
    SELECT source, key, position FROM pool
    WHERE document <@> to_bm25query(%(query)s, 'passage_bm25') < 0
    ORDER BY document <@> to_bm25query(%(query)s, 'passage_bm25')
    LIMIT {CANDIDATES}
),
meaning AS (
    SELECT source, key, position FROM pool, LATERAL (
        SELECT sum(a * b) AS similarity
        FROM unnest(embedding, %(vector)s::real[]) AS pair(a, b)
    ) s
    ORDER BY similarity DESC
    LIMIT {CANDIDATES}
),
named AS (
    SELECT source, key, position FROM pool
    WHERE {_NAMED} >= {NAMED_MIN}
    ORDER BY {_NAMED} DESC
    LIMIT {CANDIDATES}
)
SELECT source, key, position, title, heading, text, published_at, {_NAMED} AS named FROM pool
WHERE (source, key, position) IN (
    SELECT * FROM words UNION SELECT * FROM meaning UNION SELECT * FROM named
)
"""


async def search(
    conn: psycopg.AsyncConnection,
    sources: tuple[str, ...],
    query: str,
    within: list[tuple[str, int]] | None = None,
) -> list[dict]:
    """Every candidate among the passages of sources (``passage.source`` values), best first,
    with each Help topic once, as its best page:
    each a passage row (source, key, position, title, heading, text, published_at) with its
    score. within, when given, limits the search to those (key, position) passages before
    candidates are picked, so a filter drops none of its matches."""
    vector = await anyio.to_thread.run_sync(embed.query, query)
    cursor = conn.cursor(row_factory=dict_row)
    params = {
        "sources": list(sources),
        "query": query,
        "vector": vector,
        "keys": None if within is None else [key for key, _ in within],
        "positions": None if within is None else [position for _, position in within],
    }
    rows = await (await cursor.execute(_CANDIDATES_SQL, params)).fetchall()
    texts = [f"{r['title']}\n{r['heading'] or ''}\n{r['text']}" for r in rows]
    relevance = await anyio.to_thread.run_sync(rerank.scores, query, texts)
    ages = sorted({age for row in rows if (age := _age(row)) is not None})
    for row, score in zip(rows, relevance, strict=True):
        age = _age(row)
        freshness = 0.0
        if age is not None:
            freshness = 1 - ages.index(age) / (len(ages) - 1) if len(ages) > 1 else 1.0
        named = row.pop("named")
        row["score"] = (
            score
            + FRESHNESS_WEIGHT * freshness
            + (NAMED_WEIGHT * named if named >= NAMED_MIN else 0.0)
        )
    rows.sort(key=lambda r: (-r["score"], r["source"], r["key"], r["position"]))
    # A Help topic is shown whole, so it ranks once, by its best page.
    topics = set()
    best = []
    for row in rows:
        if row["source"] == "help":
            if row["key"] in topics:
                continue
            topics.add(row["key"])
        best.append(row)
    return best
