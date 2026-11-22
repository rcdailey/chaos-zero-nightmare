"""One scrape run: discover pages, fetch, parse, store, and report metrics.

A run succeeds only when every page fetched and parsed cleanly. Anything else exits non-zero and
shows up in the exported metrics, which back the alert rules in ``deploy/alerts.yaml``.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
from opentelemetry.metrics import Meter

from . import digest, metrics, store
from .fetch import Blocked, FetchFailed, Listing, sitemap_pages
from .parse import ParseError, page_kind, parse_page

log = logging.getLogger(__name__)

PARSE_STEP = "prydwen.parse"

# With the 10 s crawl delay this is about one progress line per six pages, so a stalled run
# goes quiet for no more than a minute or two plus one page's retries.
PROGRESS_INTERVAL_SECONDS = 60

# Pages the sitemaps leave out; robots.txt allows them.
UNLISTED_PAGES = ("https://www.prydwen.gg/chaos-zero-nightmare/banners",)
# A stored page is downloaded again after this long even when its sitemap date has not moved.
MAX_AGE = timedelta(days=7)


@dataclass
class RunResult:
    # ok, unchanged (not parsed again), parse_error, fetch_error -> count
    pages: Counter = field(default_factory=Counter)
    fetches: Counter = field(default_factory=Counter)  # fetched, unchanged -> count
    validation_failures: Counter = field(default_factory=Counter)  # (kind, field) -> count
    blocked: bool = False
    discovery_failed: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.blocked
            or self.discovery_failed
            or self.validation_failures
            or self.pages["fetch_error"]
        )


def scrape(conn: psycopg.Connection, get: Callable[[str], str], allow_shrink: bool) -> RunResult:
    """Download the pages that may have changed (see ``_due``), then parse the stored pages
    whose HTML or parser changed (see ``_parse_stored``)."""
    result = RunResult()
    stored = {
        url: (lastmod, fetched_at, sitemap)
        for url, lastmod, fetched_at, sitemap in conn.execute(
            "SELECT url, lastmod, fetched_at, sitemap FROM prydwen.page"
        )
    }
    remembered = {sitemap for _, _, sitemap in stored.values() if sitemap}
    try:
        listed = sitemap_pages(get, remembered)
    except Blocked as error:
        log.error("blocked by Cloudflare", extra={"error": str(error)})
        result.blocked = True
        return result
    except FetchFailed as error:
        log.error("page discovery failed", extra={"error": str(error)})
        result.discovery_failed = True
        return result
    listed |= dict.fromkeys(UNLISTED_PAGES)
    now = datetime.now(UTC)
    due = sorted(url for url, listing in listed.items() if _due(stored.get(url), listing, now))
    result.fetches["unchanged"] = len(listed) - len(due)
    for url, listing in listed.items():
        if url in stored and listing and stored[url][2] != listing.sitemap:
            # The game moved to another sitemap; remember it even for pages not downloaded.
            conn.execute(
                "UPDATE prydwen.page SET sitemap = %s WHERE url = %s", (listing.sitemap, url)
            )
    log.info("discovered pages", extra={"count": len(listed), "due": len(due)})
    if _fetch(conn, get, due, listed, result):
        _parse_stored(conn, allow_shrink, result)
    return result


def _due(stored, listing: Listing | None, now: datetime) -> bool:
    """Whether a page may have changed since its stored copy: there is none, the sitemap gives
    no date, the date moved, or the copy is older than ``MAX_AGE`` (which bounds how long a
    change the date missed goes unnoticed)."""
    if stored is None or listing is None or listing.lastmod is None:
        return True
    lastmod, fetched_at, _ = stored
    return lastmod != listing.lastmod or fetched_at < now - MAX_AGE


def _fetch(conn, get, urls: list[str], listed: dict, result: RunResult) -> bool:
    """Download and store ``urls``; False when Cloudflare blocked the run."""
    last_progress = time.monotonic()
    for done, url in enumerate(urls):
        if time.monotonic() - last_progress >= PROGRESS_INTERVAL_SECONDS:
            last_progress = time.monotonic()
            log.info(
                "fetch progress",
                extra={
                    "done": done,
                    "total": len(urls),
                    "fetched": result.fetches["fetched"],
                    "fetch_error": result.pages["fetch_error"],
                },
            )
        try:
            html = get(url)
        except Blocked as error:
            # Every later request would be blocked too; stop and keep existing data.
            log.error("blocked by Cloudflare", extra={"url": url, "error": str(error)})
            result.blocked = True
            return False
        except FetchFailed as error:
            # The stored copy and its date stay, so the next run downloads the page again.
            log.error("fetch failed", extra={"url": url, "error": str(error)})
            result.pages["fetch_error"] += 1
            continue
        store.save_html(conn, url, page_kind(url), html, datetime.now(UTC), listed[url])
        result.fetches["fetched"] += 1
    return True


def fetch_portraits(
    conn: psycopg.Connection, get_bytes: Callable[[str], bytes], result: RunResult
) -> None:
    """Store every character image not stored yet, and download a stored one again when its
    character page was downloaded after it, for post-processing (``czn_data.enrich``).

    Images keep their URL when Prydwen replaces the art, so a downloaded image that differs from
    the stored one replaces it and loses its tags, which the next enrich computes again. An
    image that fails to download counts as a ``fetch_error`` and is tried again next run.
    """
    due = conn.execute(
        """
        SELECT c.image_url, p.image FROM prydwen.character c
        JOIN prydwen.page USING (url)
        LEFT JOIN prydwen.portrait p ON p.image_url = c.image_url
        WHERE c.image_url IS NOT NULL
        GROUP BY c.image_url, p.image, p.fetched_at
        HAVING p.fetched_at IS NULL OR p.fetched_at < max(page.fetched_at)
        ORDER BY c.image_url
        """
    ).fetchall()
    if due:
        log.info("fetching portraits", extra={"count": len(due)})
    for url, stored in due:
        try:
            image = get_bytes(url)
        except Blocked as error:
            log.error("blocked by Cloudflare", extra={"url": url, "error": str(error)})
            result.blocked = True
            return
        except FetchFailed as error:
            log.error("fetch failed", extra={"url": url, "error": str(error)})
            result.pages["fetch_error"] += 1
            continue
        with conn.transaction():
            if stored is not None and image != stored:
                for table in ("portrait_gender", "portrait_identity"):
                    conn.execute(f"DELETE FROM prydwen.{table} WHERE image_url = %s", (url,))
            conn.execute(
                "INSERT INTO prydwen.portrait (image_url, image, fetched_at) VALUES (%s, %s, %s)"
                " ON CONFLICT (image_url) DO UPDATE"
                " SET image = excluded.image, fetched_at = excluded.fetched_at",
                (url, image, datetime.now(UTC)),
            )


def reparse(conn: psycopg.Connection, allow_shrink: bool) -> RunResult:
    """Parse every stored page again, changed or not. Fetches nothing."""
    result = RunResult()
    _parse_stored(conn, allow_shrink, result, every_page=True)
    return result


def _parse_stored(conn, allow_shrink: bool, result: RunResult, every_page: bool = False) -> None:
    """Parse each stored page whose HTML or parser code changed since it last parsed cleanly
    (``czn_data.digest``); a page that failed is parsed again every run, so it keeps failing the
    run until a fix. A skipped page's rows came from identical HTML, so they take the time of
    its newest download."""
    code = digest.code("czn_data.parse", "czn_data.store")
    done = {} if every_page else digest.recorded(conn, PARSE_STEP)
    stored = conn.execute("SELECT url, md5(html) FROM prydwen.page ORDER BY url").fetchall()
    for url, html_md5 in stored:
        fingerprint = digest.item(code, url, html_md5)
        if done.get(url, (None,))[0] == fingerprint:
            conn.execute(
                "UPDATE prydwen.page SET data_fetched_at = fetched_at WHERE url = %s", (url,)
            )
            result.pages["unchanged"] += 1
            continue
        (html,) = conn.execute("SELECT html FROM prydwen.page WHERE url = %s", (url,)).fetchone()
        if _process(conn, url, html, allow_shrink, result):
            digest.record(conn, PARSE_STEP, url, fingerprint)


def _process(conn, url, html, allow_shrink, result: RunResult) -> bool:
    """Parse one page and store its rows; False when it failed and kept its previous rows."""
    kind = page_kind(url)
    parsed, error = None, None
    try:
        parsed = parse_page(url, html)
        if not allow_shrink:
            store.check_shrink(conn, url, parsed)
    except ParseError as failure:
        parsed, error = None, str(failure)
        result.validation_failures[(kind, failure.field)] += 1
    except Exception as failure:  # noqa: BLE001 - layout changes break parsers unpredictably
        parsed, error = None, f"internal: {failure!r}"
        result.validation_failures[(kind, "internal")] += 1
    if error:
        log.error("parse failed, keeping previous data", extra={"url": url, "error": error})
        result.pages["parse_error"] += 1
    else:
        result.pages["ok"] += 1
    store.save_parse(conn, url, kind, parsed, error)
    return error is None


def export_metrics(conn: psycopg.Connection, result: RunResult, started: float) -> bool:
    """Send the run's metrics once (see ``czn_data.metrics``); False if the export failed."""

    def record(meter: Meter) -> None:
        now = time.time()

        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.scrape.success", "1 when the run had no failures").set(int(result.ok))
        gauge("czn.scrape.blocked", "1 when Cloudflare blocked the run").set(int(result.blocked))
        gauge("czn.scrape.discovery_failed", "1 when page discovery failed").set(
            int(result.discovery_failed)
        )
        gauge("czn.scrape.last_run.timestamp", "End of the run", "s").set(now)
        if result.ok:
            gauge("czn.scrape.last_success.timestamp", "End of the last successful run", "s").set(
                now
            )
        gauge("czn.scrape.duration", "Run duration", "s").set(now - started)
        pages = gauge("czn.scrape.pages", "Pages by result")
        for outcome in ("ok", "unchanged", "fetch_error", "parse_error"):
            pages.set(result.pages[outcome], {"result": outcome})
        fetches = gauge("czn.scrape.fetches", "Pages downloaded or skipped as unchanged")
        for outcome in ("fetched", "unchanged"):
            fetches.set(result.fetches[outcome], {"result": outcome})
        failures = gauge(
            "czn.scrape.validation_failures", "Pages that no longer parse, by page kind and field"
        )
        for (kind, field_name), count in result.validation_failures.items():
            failures.set(count, {"kind": kind, "field": field_name})
        rows = gauge("czn.data.rows", "Rows stored per table")
        for table, count in store.row_counts(conn).items():
            rows.set(count, {"table": table})

    return metrics.export("czn_data.scrape", record)
