"""Contract: a Game8 run stores only the guide pages the game page lists under the kept headings,
refetches only pages whose sitemap date moved, and never replaces or drops stored pages because
of a failure.

Game8 is replaced by an in-memory site (URL -> HTML or sitemap bytes); the database is real
Postgres.
"""

import gzip

import pytest

from czn_data.cli import main
from czn_data.fetch import FetchFailed
from czn_data.game8 import ARCHIVE_PREFIX, GAME_PAGE, SITEMAP, scrape_game8

KEPT, SKIPPED = "All Tips and Tricks", "5 Star Character Builds"


def game_page(kept: list[str], skipped: list[str] = (), season_guides: list[str] = ()) -> str:
    def links(ids):
        return "".join(
            f'<a href="/games/Chaos-Zero-Nightmare/archives/{i}">Page {i}</a>' for i in ids
        )

    seasons = "".join(
        f'<a href="{ARCHIVE_PREFIX}{i}">Season 5: A Promise Upon the Snow Guide</a>'
        for i in season_guides
    )
    return (
        f"<html><body><h3>Season 5 - Phase 1 Guides</h3>{seasons}"
        f"<h3>{KEPT}</h3>{links(kept)}<h3>{SKIPPED}</h3>{links(skipped)}</body></html>"
    )


def sitemap(dates: dict[str, str]) -> bytes:
    urls = "".join(
        f"<url><loc>{ARCHIVE_PREFIX}{i}</loc><lastmod>{d}</lastmod></url>" for i, d in dates.items()
    )
    xml = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}'
    return gzip.compress(f"{xml}</urlset>".encode())


def guide(text: str) -> str:
    return f'<html><body><div class="archive-style-wrapper"><p>{text}</p></div></body></html>'


def site(page: str, dates: dict[str, str], pages: dict[str, str | Exception]):
    """Game8 serving the game page, the sitemap, and ``pages`` (archive id -> HTML or a failure).
    Records every guide page fetched in ``get.fetched``."""
    fetched = []

    def get(url: str) -> str:
        if url == GAME_PAGE:
            return page
        number = url.removeprefix(ARCHIVE_PREFIX)
        fetched.append(number)
        content = pages[number]
        if isinstance(content, Exception):
            raise content
        return content

    def get_bytes(url: str) -> bytes:
        assert url == SITEMAP
        return sitemap(dates)

    get.fetched = fetched
    return get, get_bytes


def stored(conn) -> dict[str, str]:
    rows = conn.execute("SELECT url, html FROM game8.page").fetchall()
    return {url.removeprefix(ARCHIVE_PREFIX): html for url, html in rows}


def run(conn, get_pair, allow_shrink=False):
    get, get_bytes = get_pair
    return scrape_game8(conn, get, get_bytes, allow_shrink)


DAY1, DAY2 = "2026-11-01T00:00:00-05:00", "2026-11-02T00:00:00-05:00"


def test_clean_run_stores_only_pages_under_kept_headings_and_season_guides(conn):
    pair = site(
        game_page(["1", "2"], skipped=["3"], season_guides=["4"]),
        {"1": DAY1, "2": DAY1, "3": DAY1, "4": DAY1},
        {"1": guide("a"), "2": guide("b"), "4": guide("d")},
    )

    result = run(conn, pair)

    assert result.ok
    assert set(stored(conn)) == {"1", "2", "4"}
    assert conn.execute("SELECT ok FROM game8.run").fetchone() == (True,)


def test_page_missing_from_the_sitemap_is_not_stored(conn):
    pair = site(game_page(["1", "2"]), {"1": DAY1}, {"1": guide("a"), "2": guide("premium")})

    result = run(conn, pair)

    assert result.ok
    assert set(stored(conn)) == {"1"} and pair[0].fetched == ["1"]


def test_only_pages_whose_date_moved_are_fetched_again(conn):
    run(conn, site(game_page(["1", "2"]), {"1": DAY1, "2": DAY1}, {"1": guide("old"),
        "2": guide("same")}))  # fmt: skip
    pair = site(game_page(["1", "2"]), {"1": DAY2, "2": DAY1}, {"1": guide("new")})

    result = run(conn, pair)

    assert result.ok
    assert pair[0].fetched == ["1"]
    assert "new" in stored(conn)["1"] and "same" in stored(conn)["2"]


def test_failed_or_empty_page_keeps_its_previous_copy_and_fails_the_run(conn):
    run(conn, site(game_page(["1", "2"]), {"1": DAY1, "2": DAY1}, {"1": guide("old"),
        "2": guide("old")}))  # fmt: skip
    pair = site(
        game_page(["1", "2"]),
        {"1": DAY2, "2": DAY2},
        {"1": FetchFailed("HTTP 500"), "2": "<html><body>Access denied</body></html>"},
    )

    result = run(conn, pair)

    assert not result.ok
    assert all("old" in html for html in stored(conn).values())
    latest = "SELECT ok FROM game8.run ORDER BY started_at DESC"
    assert conn.execute(latest).fetchone() == (False,)


def test_selection_that_shrinks_sharply_keeps_every_page(conn):
    ids = [str(n) for n in range(10)]
    run(conn, site(game_page(ids), dict.fromkeys(ids, DAY1), {i: guide(i) for i in ids}))
    shrunk = site(game_page(ids[:2]), dict.fromkeys(ids, DAY1), {})

    assert not run(conn, shrunk).ok
    assert len(stored(conn)) == 10

    assert run(conn, shrunk, allow_shrink=True).ok
    assert set(stored(conn)) == {"0", "1"}


def test_game_page_without_kept_headings_fails_and_keeps_pages(conn):
    run(conn, site(game_page(["1"]), {"1": DAY1}, {"1": guide("a")}))

    result = run(conn, site(game_page([], skipped=["1"]), {"1": DAY1}, {}))

    assert not result.ok
    assert result.validation_failures == {"selection.pages": 1}
    assert set(stored(conn)) == {"1"}


def test_scrape_game8_command_exits_nonzero_when_game8_fails(conn, database_url, monkeypatch):
    class Unreachable:
        def __init__(self, **_):
            pass

        def get(self, url: str) -> str:
            raise FetchFailed(f"{url}: HTTP 503")

        get_bytes = get

    monkeypatch.setattr("czn_data.fetch.Fetcher", Unreachable)
    monkeypatch.setenv("DATABASE_URL", database_url)
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.argv", ["czn-data", "scrape-game8"])

    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 1
