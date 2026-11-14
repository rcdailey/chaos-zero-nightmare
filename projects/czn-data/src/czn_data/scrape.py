"""One scrape run: discover pages, fetch, parse, store, and report metrics.

A run succeeds only when every page fetched and parsed cleanly. Anything else exits non-zero and
shows up in the pushed metrics, which back the alert rules in ``deploy/alerts.yaml``.
"""

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from prometheus_client import CollectorRegistry, Gauge, pushadd_to_gateway

from . import store
from .fetch import Blocked, FetchFailed, sitemap_urls
from .parse import ParseError, page_kind, parse_page

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    pages: Counter = field(default_factory=Counter)  # result -> count
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
    result = RunResult()
    store.apply_schema(conn)
    try:
        urls = sitemap_urls(get)
    except Blocked as error:
        log.error("blocked by Cloudflare", extra={"error": str(error)})
        result.blocked = True
        return result
    except FetchFailed as error:
        log.error("page discovery failed", extra={"error": str(error)})
        result.discovery_failed = True
        return result
    log.info("discovered pages", extra={"count": len(urls)})

    for url in urls:
        try:
            html = get(url)
        except Blocked as error:
            # Every later request would be blocked too; stop and keep existing data.
            log.error("blocked by Cloudflare", extra={"url": url, "error": str(error)})
            result.blocked = True
            return result
        except FetchFailed as error:
            log.error("fetch failed", extra={"url": url, "error": str(error)})
            result.pages["fetch_error"] += 1
            continue
        _process(conn, url, html, datetime.now(UTC), allow_shrink, result)
    return result


def reparse(conn: psycopg.Connection, allow_shrink: bool) -> RunResult:
    """Parse the stored HTML again, for example after fixing a parser. Fetches nothing."""
    result = RunResult()
    store.apply_schema(conn)
    pages = conn.execute("SELECT url, html, fetched_at FROM page ORDER BY url").fetchall()
    for url, html, fetched_at in pages:
        _process(conn, url, html, fetched_at, allow_shrink, result)
    return result


def _process(conn, url, html, fetched_at, allow_shrink, result: RunResult) -> None:
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
    store.save_page(conn, url, kind, html, fetched_at, parsed, error)


def push_metrics(conn: psycopg.Connection, result: RunResult, started: float) -> None:
    """Push run metrics in Pushgateway format, which VictoriaMetrics and vmagent accept.

    Short-lived jobs cannot be scraped, so they push. Without ``METRICS_PUSH_URL`` this logs only.
    """
    url = os.environ.get("METRICS_PUSH_URL")
    if not url:
        log.info("METRICS_PUSH_URL not set, skipping metrics push")
        return
    registry = CollectorRegistry()
    now = time.time()
    Gauge("czn_scrape_success", "1 when the last run had no failures", registry=registry).set(
        int(result.ok)
    )
    Gauge("czn_scrape_blocked", "1 when Cloudflare blocked the run", registry=registry).set(
        int(result.blocked)
    )
    Gauge("czn_scrape_discovery_failed", "1 when sitemap discovery failed", registry=registry).set(
        int(result.discovery_failed)
    )
    Gauge("czn_scrape_last_run_timestamp_seconds", "End of the last run", registry=registry).set(
        now
    )
    if result.ok:
        Gauge(
            "czn_scrape_last_success_timestamp_seconds",
            "End of the last fully successful run",
            registry=registry,
        ).set(now)
    Gauge("czn_scrape_duration_seconds", "Run duration", registry=registry).set(now - started)
    pages = Gauge("czn_scrape_pages", "Pages by result", ["result"], registry=registry)
    for outcome in ("ok", "fetch_error", "parse_error"):
        pages.labels(outcome).set(result.pages[outcome])
    failures = Gauge(
        "czn_scrape_validation_failures",
        "Pages whose layout no longer parses, by page kind and failed field",
        ["kind", "field"],
        registry=registry,
    )
    for (kind, field_name), count in result.validation_failures.items():
        failures.labels(kind, field_name).set(count)
    rows = Gauge("czn_data_rows", "Rows stored per table", ["table"], registry=registry)
    for table, count in store.row_counts(conn).items():
        rows.labels(table).set(count)
    pushadd_to_gateway(url, job="czn-scrape", registry=registry)
