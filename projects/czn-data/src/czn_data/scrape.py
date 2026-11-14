"""One scrape run: discover pages, fetch, parse, store, and report metrics.

A run succeeds only when every page fetched and parsed cleanly. Anything else exits non-zero and
shows up in the exported metrics, which back the alert rules in ``deploy/alerts.yaml``.
"""

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
from opentelemetry.sdk.resources import Resource

from . import store
from .fetch import Blocked, FetchFailed, sitemap_urls
from .parse import ParseError, page_kind, parse_page

log = logging.getLogger(__name__)

# With the 10 s crawl delay this is about one progress line per six pages, so a stalled run
# goes quiet for no more than a minute or two plus one page's retries.
PROGRESS_INTERVAL_SECONDS = 60


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

    last_progress = time.monotonic()
    for done, url in enumerate(urls):
        if time.monotonic() - last_progress >= PROGRESS_INTERVAL_SECONDS:
            last_progress = time.monotonic()
            log.info(
                "fetch progress",
                extra={
                    "done": done,
                    "total": len(urls),
                    "ok": result.pages["ok"],
                    "fetch_error": result.pages["fetch_error"],
                    "parse_error": result.pages["parse_error"],
                },
            )
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


def export_metrics(conn: psycopg.Connection, result: RunResult, started: float) -> bool:
    """Send the run's metrics once over OTLP/HTTP; return False if the export failed.

    The job exits right after, so a periodic reader would race the exit. Instead every value is
    recorded, collected once, and exported synchronously, and the export result is checked.
    Configuration comes only from the standard OTEL_* variables (endpoint, headers, timeout,
    OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES). With no OTLP endpoint set, nothing is sent.

    Names and units are chosen so VictoriaMetrics with -opentelemetry.usePrometheusNaming stores
    the Prometheus names documented in the README (``czn.scrape.pages`` -> ``czn_scrape_pages``;
    unit ``s`` adds ``_seconds``). Count gauges stay unitless: unit "1" would add ``_ratio``.
    """
    if not _otlp_metrics_configured():
        log.info("no OTLP metrics endpoint configured, skipping metrics export")
        return True

    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader], resource=Resource.create()).get_meter(
        "czn_data.scrape"
    )
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
        gauge("czn.scrape.last_success.timestamp", "End of the last successful run", "s").set(now)
    gauge("czn.scrape.duration", "Run duration", "s").set(now - started)
    pages = gauge("czn.scrape.pages", "Pages by result")
    for outcome in ("ok", "fetch_error", "parse_error"):
        pages.set(result.pages[outcome], {"result": outcome})
    failures = gauge(
        "czn.scrape.validation_failures", "Pages that no longer parse, by page kind and field"
    )
    for (kind, field_name), count in result.validation_failures.items():
        failures.set(count, {"kind": kind, "field": field_name})
    rows = gauge("czn.data.rows", "Rows stored per table")
    for table, count in store.row_counts(conn).items():
        rows.set(count, {"table": table})

    exporter = OTLPMetricExporter()
    try:
        outcome = exporter.export(reader.get_metrics_data())
    finally:
        exporter.shutdown()
    if outcome is not MetricExportResult.SUCCESS:
        log.error("OTLP metrics export failed")
        return False
    log.info("metrics exported")
    return True


def _otlp_metrics_configured() -> bool:
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    if os.environ.get("OTEL_METRICS_EXPORTER", "otlp").lower() != "otlp":
        return False
    return any(
        os.environ.get(name)
        for name in ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
    )
