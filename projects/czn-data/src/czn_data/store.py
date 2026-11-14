"""Write scraped pages to Postgres without ever replacing good data with broken data.

Each page's rows are replaced only when its newest HTML parses cleanly and does not lose a large
share of its previous rows. Otherwise the HTML is still saved (so a parser fix can reprocess it
with ``czn-data reparse``) and the previous rows stay in place.
"""

from datetime import datetime
from importlib.resources import files

import psycopg
from psycopg.types.json import Jsonb

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
)
JSON_COLUMNS = {"sections", "stats", "bonuses"}

# A page that suddenly yields this much fewer rows of a kind than before is treated as broken.
SHRINK_LIMIT = 0.2


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(files(__package__).joinpath("schema.sql").read_text())


def check_shrink(conn: psycopg.Connection, url: str, parsed: ParsedPage) -> None:
    for table in DERIVED_TABLES:
        (old,) = conn.execute(f"SELECT count(*) FROM {table} WHERE url = %s", (url,)).fetchone()
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
        conn.execute(
            """
            INSERT INTO page (url, kind, html, fetched_at, parse_error)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE SET kind = excluded.kind, html = excluded.html,
                fetched_at = excluded.fetched_at, parse_error = excluded.parse_error
            """,
            (url, kind, html, fetched_at, error),
        )
        if parsed is None:
            return
        for table in (*DERIVED_TABLES, "document"):
            conn.execute(f"DELETE FROM {table} WHERE url = %s", (url,))
        conn.execute(
            "INSERT INTO document (url, title, body) VALUES (%s, %s, %s)",
            (url, parsed.title, parsed.body),
        )
        for table, rows in parsed.records.items():
            for row in rows:
                _insert(conn, table, {"url": url} | row)
        conn.execute("UPDATE page SET data_fetched_at = fetched_at WHERE url = %s", (url,))


def _insert(conn: psycopg.Connection, table: str, row: dict) -> None:
    if table not in DERIVED_TABLES:
        raise ValueError(f"unknown table {table!r}")
    columns = list(row)
    values = [Jsonb(row[c]) if c in JSON_COLUMNS else row[c] for c in columns]
    names = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", values)


def row_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (*DERIVED_TABLES, "document")
    }
