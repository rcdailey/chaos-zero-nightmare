"""Contract: a scrape run never replaces good data with broken data, and reports every failure.

Prydwen is replaced by an in-memory site (URL -> HTML), which isolates the run logic from the
network; the database is real Postgres.
"""

from pathlib import Path

from czn_data.fetch import Blocked, FetchFailed
from czn_data.scrape import reparse, scrape

SITE = "https://www.prydwen.gg"
CHARACTER_URL = f"{SITE}/chaos-zero-nightmare/characters/mei-lin"
GUIDE_URL = f"{SITE}/chaos-zero-nightmare/guides/beginner-guide"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()
GUIDE_HTML = "<html><body><div id='S:1'><h1>Beginner Guide</h1><p>Pull Mei Lin.</p></div></body>"


def site(pages: dict[str, str | Exception]):
    """robots.txt -> one sitemap listing ``pages`` (plus a non-CZN page) -> page contents."""
    locs = "".join(f"<url><loc>{url}</loc></url>" for url in [*pages, f"{SITE}/nikke/tier-list"])
    content = {
        f"{SITE}/robots.txt": f"User-agent: *\nSitemap: {SITE}/sitemap/0.xml\n",
        f"{SITE}/sitemap/0.xml": f"<urlset>{locs}</urlset>",
        **pages,
    }
    requested = []

    def get(url: str) -> str:
        requested.append(url)
        value = content[url]
        if isinstance(value, Exception):
            raise value
        return value

    get.requested = requested
    return get


def card_names(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM card").fetchall()}


def test_clean_run_stores_every_game_page(conn):
    get = site({CHARACTER_URL: CHARACTER_HTML, GUIDE_URL: GUIDE_HTML})

    result = scrape(conn, get, allow_shrink=False)

    assert result.ok
    assert f"{SITE}/nikke/tier-list" not in get.requested
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}
    titles = {row[0] for row in conn.execute("SELECT title FROM document").fetchall()}
    assert titles == {"Mei Lin", "Beginner Guide"}


def test_layout_change_keeps_previous_rows_and_reports_the_field(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    broken = CHARACTER_HTML.replace("czn-hero-name", "hero-v2")

    result = scrape(conn, site({CHARACTER_URL: broken}), allow_shrink=False)

    assert not result.ok
    assert result.validation_failures == {("character", "character.name"): 1}
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}
    html, data_at, fetched_at, error = conn.execute(
        "SELECT html, data_fetched_at, fetched_at, parse_error FROM page"
    ).fetchone()
    assert html == broken  # kept so `reparse` can apply a parser fix later
    assert data_at < fetched_at
    assert error.startswith("character.name")


def test_reparse_recovers_stored_html_without_fetching(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    conn.execute("DELETE FROM card")

    result = reparse(conn, allow_shrink=False)

    assert result.ok
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}


def test_large_row_loss_is_rejected_unless_allowed(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    start = CHARACTER_HTML.index('<article class="build-table-card">')
    end = CHARACTER_HTML.index("</article>", start) + len("</article>")
    without_variants = CHARACTER_HTML[:start] + CHARACTER_HTML[end:]  # 3 cards -> 2

    rejected = scrape(conn, site({CHARACTER_URL: without_variants}), allow_shrink=False)
    assert rejected.validation_failures == {("character", "card.count"): 1}
    assert "Rising Dragon Spire V" in card_names(conn)

    accepted = scrape(conn, site({CHARACTER_URL: without_variants}), allow_shrink=True)
    assert accepted.ok
    assert card_names(conn) == {"Strike", "Rising Dragon Spire"}


def test_cloudflare_block_stops_the_run_and_keeps_data(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    get = site({CHARACTER_URL: Blocked("challenge"), GUIDE_URL: GUIDE_HTML})

    result = scrape(conn, get, allow_shrink=False)

    assert result.blocked
    assert not result.ok
    assert GUIDE_URL not in get.requested  # the guide sorts after the character page
    assert "Strike" in card_names(conn)


def test_fetch_failure_skips_the_page_and_fails_the_run(conn):
    get = site({CHARACTER_URL: FetchFailed("HTTP 404"), GUIDE_URL: GUIDE_HTML})

    result = scrape(conn, get, allow_shrink=False)

    assert not result.ok
    assert result.pages == {"fetch_error": 1, "ok": 1}


def test_discovery_failure_fails_the_run(conn):
    get = site({})
    result = scrape(conn, get, allow_shrink=False)
    assert result.discovery_failed
    assert not result.ok
