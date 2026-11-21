"""Contract: a scrape run never replaces good data with broken data, and reports every failure.

Prydwen is replaced by an in-memory site (URL -> HTML), which isolates the run logic from the
network; the database is real Postgres.
"""

from pathlib import Path

from test_parse import banner_html

from czn_data.fetch import Blocked, FetchFailed
from czn_data.scrape import MAX_AGE, RunResult, fetch_portraits, reparse, scrape

SITE = "https://www.prydwen.gg"
BANNERS_URL = f"{SITE}/chaos-zero-nightmare/banners"
CHARACTER_URL = f"{SITE}/chaos-zero-nightmare/characters/mei-lin"
GUIDE_URL = f"{SITE}/chaos-zero-nightmare/guides/beginner-guide"
CHARACTER_HTML = (Path(__file__).parent / "fixtures" / "character.html").read_text()
GUIDE_HTML = "<html><body><div id='S:1'><h1>Beginner Guide</h1><p>Pull Mei Lin.</p></div></body>"


def site(
    pages: dict[str, str | Exception],
    lastmod: dict[str, str] | None = None,
    sitemap: int = 1,
    sitemaps: int = 2,
):
    """robots.txt -> ``sitemaps`` sitemaps, of which number ``sitemap`` lists ``pages`` (with
    their ``lastmod`` dates, if any) and sitemap 0 a non-CZN page -> page contents. The banners
    page, which the sitemaps leave out, is served too."""
    lastmod = lastmod or {}

    def entry(url: str) -> str:
        date = f"<lastmod>{lastmod[url]}</lastmod>" if url in lastmod else ""
        return f"<url><loc>{url}</loc>{date}</url>"

    listed = dict.fromkeys(range(sitemaps), "")
    listed[0] += entry(f"{SITE}/nikke/tier-list")
    listed[sitemap] += "".join(entry(url) for url in pages)
    robots = "".join(f"Sitemap: {SITE}/sitemap/{number}.xml\n" for number in listed)
    content = {
        f"{SITE}/robots.txt": f"User-agent: *\n{robots}",
        **{f"{SITE}/sitemap/{n}.xml": f"<urlset>{urls}</urlset>" for n, urls in listed.items()},
        BANNERS_URL: banner_html(),
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
    return {row[0] for row in conn.execute("SELECT name FROM prydwen.card").fetchall()}


def test_clean_run_stores_every_game_page(conn):
    get = site({CHARACTER_URL: CHARACTER_HTML, GUIDE_URL: GUIDE_HTML})

    result = scrape(conn, get, allow_shrink=False)

    assert result.ok
    assert f"{SITE}/nikke/tier-list" not in get.requested
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}
    titles = {row[0] for row in conn.execute("SELECT title FROM prydwen.document").fetchall()}
    assert titles == {"Mei Lin", "Beginner Guide", "Banners"}
    banners = conn.execute("SELECT name FROM prydwen.banner ORDER BY name").fetchall()
    assert banners == [("Blazebell",), ("Tiana",)]


def test_layout_change_keeps_previous_rows_and_reports_the_field(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    broken = CHARACTER_HTML.replace("czn-hero-name", "hero-v2")

    result = scrape(conn, site({CHARACTER_URL: broken}), allow_shrink=False)

    assert not result.ok
    assert result.validation_failures == {("character", "character.name"): 1}
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}
    html, data_at, fetched_at, error = conn.execute(
        "SELECT html, data_fetched_at, fetched_at, parse_error FROM prydwen.page WHERE url = %s",
        (CHARACTER_URL,),
    ).fetchone()
    assert html == broken  # kept so `reparse` can apply a parser fix later
    assert data_at < fetched_at
    assert error.startswith("character.name")


def test_reparse_recovers_stored_html_without_fetching(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    conn.execute("DELETE FROM prydwen.card")

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
    assert result.pages == {"fetch_error": 1, "ok": 2}  # the guide and the banners page


PORTRAIT = "https://cdn.prydwen.gg/images/chaos/characters/mei-lin_card.webp"


def test_new_portraits_are_stored_once(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)
    fetched = []

    def get_bytes(url: str) -> bytes:
        fetched.append(url)
        return b"art"

    result = RunResult()
    fetch_portraits(conn, get_bytes, result)
    fetch_portraits(conn, get_bytes, result)

    assert result.ok
    assert fetched == [PORTRAIT]
    stored = conn.execute("SELECT image_url, image FROM prydwen.portrait").fetchall()
    assert stored == [(PORTRAIT, b"art")]


def test_portrait_that_fails_is_reported_and_retried_next_run(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)

    def unreachable(url: str) -> bytes:
        raise FetchFailed("HTTP 404")

    failed = RunResult()
    fetch_portraits(conn, unreachable, failed)
    assert failed.pages == {"fetch_error": 1}

    retried = RunResult()
    fetch_portraits(conn, {PORTRAIT: b"art"}.__getitem__, retried)
    assert retried.ok
    assert conn.execute("SELECT count(*) FROM prydwen.portrait").fetchone() == (1,)


def test_blocked_portrait_fetch_stops_the_run(conn):
    scrape(conn, site({CHARACTER_URL: CHARACTER_HTML}), allow_shrink=False)

    def blocked(url: str) -> bytes:
        raise Blocked("challenge")

    result = RunResult()
    fetch_portraits(conn, blocked, result)
    assert result.blocked


def test_discovery_failure_fails_the_run(conn):
    get = site({})
    result = scrape(conn, get, allow_shrink=False)
    assert result.discovery_failed
    assert not result.ok


DATED = {CHARACTER_URL: "2026-11-20T08:31:19.144Z", GUIDE_URL: "2026-08-09T08:04:58.511Z"}
BOTH_PAGES = {CHARACTER_URL: CHARACTER_HTML, GUIDE_URL: GUIDE_HTML}


def test_unchanged_pages_are_not_downloaded_and_keep_their_rows(conn):
    scrape(conn, site(BOTH_PAGES, DATED), allow_shrink=False)
    get = site(BOTH_PAGES, DATED)

    result = scrape(conn, get, allow_shrink=False)

    assert result.ok
    assert CHARACTER_URL not in get.requested
    assert GUIDE_URL not in get.requested
    assert BANNERS_URL in get.requested  # the sitemaps give it no date
    assert result.fetches == {"fetched": 1, "unchanged": 2}
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}


def test_page_with_a_new_date_or_no_date_is_downloaded(conn):
    scrape(conn, site(BOTH_PAGES, DATED), allow_shrink=False)
    get = site(BOTH_PAGES, {CHARACTER_URL: "2026-11-21T12:30:02.426Z"})

    scrape(conn, get, allow_shrink=False)

    assert CHARACTER_URL in get.requested
    assert GUIDE_URL in get.requested


def test_copy_older_than_max_age_is_downloaded_again(conn):
    scrape(conn, site(BOTH_PAGES, DATED), allow_shrink=False)
    conn.execute(
        "UPDATE prydwen.page SET fetched_at = now() - %s - interval '1 hour' WHERE url = %s",
        (MAX_AGE, GUIDE_URL),
    )
    get = site(BOTH_PAGES, DATED)

    scrape(conn, get, allow_shrink=False)

    assert GUIDE_URL in get.requested
    assert CHARACTER_URL not in get.requested


def test_failed_download_is_retried_next_run(conn):
    scrape(conn, site(BOTH_PAGES, DATED), allow_shrink=False)
    newer = {CHARACTER_URL: "2026-11-21T12:30:02.426Z", GUIDE_URL: DATED[GUIDE_URL]}
    failing = {CHARACTER_URL: FetchFailed("HTTP 503"), GUIDE_URL: GUIDE_HTML}

    failed = scrape(conn, site(failing, newer), allow_shrink=False)
    get = site(BOTH_PAGES, newer)
    retried = scrape(conn, get, allow_shrink=False)

    assert not failed.ok
    assert retried.ok
    assert CHARACTER_URL in get.requested


def test_stored_pages_are_parsed_again_without_downloading(conn):
    scrape(conn, site(BOTH_PAGES, DATED), allow_shrink=False)
    conn.execute("DELETE FROM prydwen.card")  # as if the parser now yields different rows
    get = site(BOTH_PAGES, DATED)

    result = scrape(conn, get, allow_shrink=False)

    assert result.ok
    assert CHARACTER_URL not in get.requested
    assert card_names(conn) == {"Strike", "Rising Dragon Spire", "Rising Dragon Spire V"}


def sitemaps_read(get) -> set[str]:
    return {url for url in get.requested if "/sitemap/" in url}


def test_only_the_sitemap_that_listed_the_game_is_read(conn):
    scrape(conn, site(BOTH_PAGES, DATED, sitemap=1, sitemaps=3), allow_shrink=False)
    get = site(BOTH_PAGES, DATED, sitemap=1, sitemaps=3)

    scrape(conn, get, allow_shrink=False)

    assert sitemaps_read(get) == {f"{SITE}/sitemap/1.xml"}


def test_every_sitemap_is_read_when_the_game_moved(conn):
    scrape(conn, site(BOTH_PAGES, DATED, sitemap=1, sitemaps=3), allow_shrink=False)
    get = site(BOTH_PAGES, DATED, sitemap=2, sitemaps=3)

    result = scrape(conn, get, allow_shrink=False)

    assert result.ok
    assert sitemaps_read(get) == {f"{SITE}/sitemap/{n}.xml" for n in range(3)}
    assert CHARACTER_URL not in get.requested  # same dates, so still unchanged


def test_every_sitemap_is_read_when_robots_no_longer_names_the_remembered_one(conn):
    scrape(conn, site(BOTH_PAGES, DATED, sitemap=2, sitemaps=3), allow_shrink=False)
    get = site(BOTH_PAGES, DATED, sitemap=1, sitemaps=2)

    result = scrape(conn, get, allow_shrink=False)

    assert result.ok
    assert sitemaps_read(get) == {f"{SITE}/sitemap/{n}.xml" for n in range(2)}
