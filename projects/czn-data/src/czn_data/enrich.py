"""Post-processing: derive data from what the scrape stored, without contacting Prydwen.

Runs after the scrapes in ``czn-data daily``, or alone as ``czn-data enrich``; it can run again
at any time. Each step processes only rows it has not processed yet; a row
that fails counts as a failure of that step and is tried again next run.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from opentelemetry.metrics import Meter

from . import metrics
from .hub import ModelUnavailable

log = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    failures: Counter = field(default_factory=Counter)  # step -> count

    @property
    def ok(self) -> bool:
        return not self.failures


def tag_genders(conn: psycopg.Connection, gender: Callable[[bytes], str], result: EnrichResult):
    """Assign a gender to every stored portrait that has none yet (see ``czn_data.tagger``)."""
    _tag(conn, "gender", "portrait_gender", "gender", gender, result)


def compute_identities(
    conn: psycopg.Connection,
    model: str,
    identity_print: Callable[[bytes], list[float]],
    result: EnrichResult,
):
    """Store the identity print of every portrait that has none from ``model`` yet (see
    ``czn_data.identity``)."""
    _tag(conn, "identity", "portrait_identity", "print", identity_print, result, model)


def _tag(
    conn: psycopg.Connection,
    step: str,
    table: str,
    column: str,
    tag: Callable[[bytes], object],
    result: EnrichResult,
    model: str | None = None,
):
    """Store ``tag`` of each portrait missing from ``table`` (for ``model``, when the table keeps
    one row per model); a failure counts against ``step``."""
    done = f"SELECT image_url FROM prydwen.{table}" + (" WHERE model = %(model)s" if model else "")
    portraits = conn.execute(
        f"""
        SELECT image_url, image FROM prydwen.portrait
        WHERE image_url NOT IN ({done})
        ORDER BY image_url
        """,
        {"model": model},
    ).fetchall()
    if portraits:
        log.info("tagging portraits", extra={"step": step, "count": len(portraits)})
    for url, image in portraits:
        try:
            value = tag(image)
        except ModelUnavailable as error:
            log.error("portrait model unavailable", extra={"step": step, "error": str(error)})
            result.failures[step] += 1
            return
        except Exception as error:  # noqa: BLE001 - one bad portrait must not stop the rest
            log.error(
                "portrait tagging failed", extra={"step": step, "url": url, "error": repr(error)}
            )
            result.failures[step] += 1
            continue
        row = {"image_url": url, column: value, "tagged_at": datetime.now(UTC)}
        if model:
            row["model"] = model
        conn.execute(
            f"INSERT INTO prydwen.{table} ({', '.join(row)})"
            f" VALUES ({', '.join(f'%({name})s' for name in row)})",
            row,
        )


def export_metrics(result: EnrichResult, started: float) -> bool:
    """Send the run's metrics once (see ``czn_data.metrics``); False if the export failed."""

    def record(meter: Meter) -> None:
        now = time.time()

        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.enrich.success", "1 when the run had no failures").set(int(result.ok))
        gauge("czn.enrich.last_run.timestamp", "End of the run", "s").set(now)
        if result.ok:
            gauge("czn.enrich.last_success.timestamp", "End of the last successful run", "s").set(
                now
            )
        gauge("czn.enrich.duration", "Run duration", "s").set(now - started)
        failures = gauge("czn.enrich.failures", "Rows that failed, by step")
        for step in ("gender", "identity"):
            failures.set(result.failures[step], {"step": step})

    return metrics.export("czn_data.enrich", record)
