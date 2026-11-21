"""Write scraped Prydwen pages to the ``prydwen`` schema without replacing good data with broken.

Each page's rows are replaced only when its newest HTML parses cleanly and does not lose a large
share of its previous rows. Otherwise the HTML is still saved (so a parser fix can reprocess it
with ``czn-data reparse``) and the previous rows stay in place. The MCP server does not read
these tables directly; ``czn_data.build`` merges them with e7bot's data.
"""

from dataclasses import astuple
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from .fetch import Listing
from .parse import ParsedPage, ParseError

DERIVED_TABLES = (
    "character",
    "card",
    "potential",
    "manifest_ego",
    "equipment",
    "fragment_set",
    "partner",
    "effect",
    "team",
    "deck",
    "character_equipment",
    "seasonal_power",
    "fragment_rank",
    "partner_rank",
    "synergy",
    "banner",
)
JSON_COLUMNS = {"sections", "stats", "bonuses", "cards", "slots"}

# A page that suddenly yields this much fewer rows of a kind than before is treated as broken.
SHRINK_LIMIT = 0.2


def check_shrink(conn: psycopg.Connection, url: str, parsed: ParsedPage) -> None:
    for table in DERIVED_TABLES:
        (old,) = conn.execute(
            f"SELECT count(*) FROM prydwen.{table} WHERE url = %s", (url,)
        ).fetchone()
        new = len(parsed.records.get(table, []))
        if old and new < old * (1 - SHRINK_LIMIT):
            raise ParseError(f"{table}.count", f"dropped from {old} to {new} rows")


def save_page(
    conn: psycopg.Connection,
    url: str,
    kind: str,
    html: str,
    fetched_at: datetime,
    parsed: ParsedPage | None,
    error: str | None,
) -> None:
    """Store the HTML; replace the page's rows only when ``parsed`` is given."""
    with conn.transaction():
        save_html(conn, url, kind, html, fetched_at)
        save_parse(conn, url, kind, parsed, error)


def save_html(
    conn: psycopg.Connection,
    url: str,
    kind: str,
    html: str,
    fetched_at: datetime,
    listing: Listing | None = None,
) -> None:
    """Store a downloaded page with the sitemap listing it was downloaded for, leaving its
    rows to ``save_parse``."""
    conn.execute(
        """
        INSERT INTO prydwen.page (url, kind, html, fetched_at, lastmod, sitemap)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (url) DO UPDATE SET kind = excluded.kind, html = excluded.html,
            fetched_at = excluded.fetched_at, lastmod = excluded.lastmod,
            sitemap = excluded.sitemap
        """,
        (url, kind, html, fetched_at, *(astuple(listing) if listing else (None, None))),
    )


def save_parse(
    conn: psycopg.Connection, url: str, kind: str, parsed: ParsedPage | None, error: str | None
) -> None:
    """Record how the stored HTML parsed; replace the page's rows only when ``parsed`` is given."""
    with conn.transaction():
        conn.execute(
            "UPDATE prydwen.page SET kind = %s, parse_error = %s WHERE url = %s",
            (kind, error, url),
        )
        if parsed is None:
            return
        for table in (*DERIVED_TABLES, "document"):
            conn.execute(f"DELETE FROM prydwen.{table} WHERE url = %s", (url,))
        conn.execute(
            "INSERT INTO prydwen.document (url, title, body) VALUES (%s, %s, %s)",
            (url, parsed.title, parsed.body),
        )
        for table, rows in parsed.records.items():
            for row in rows:
                _insert(conn, table, {"url": url} | row)
        conn.execute("UPDATE prydwen.page SET data_fetched_at = fetched_at WHERE url = %s", (url,))


def _insert(conn: psycopg.Connection, table: str, row: dict) -> None:
    if table not in DERIVED_TABLES:
        raise ValueError(f"unknown table {table!r}")
    columns = list(row)
    values = [Jsonb(row[c]) if c in JSON_COLUMNS else row[c] for c in columns]
    names = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    conn.execute(f"INSERT INTO prydwen.{table} ({names}) VALUES ({placeholders})", values)


def row_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM prydwen.{table}").fetchone()[0]
        for table in (*DERIVED_TABLES, "document")
    }
