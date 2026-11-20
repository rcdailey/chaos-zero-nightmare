"""Post-processing: derive data from what the scrape stored, without contacting Prydwen.

Runs as its own job (``czn-data enrich``), so a slow or failing step never affects the scrape,
and can run again at any time. Each step processes only rows it has not processed yet; a row
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
from .gender import TaggerUnavailable

log = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    failures: Counter = field(default_factory=Counter)  # step -> count

    @property
    def ok(self) -> bool:
        return not self.failures


def tag_genders(conn: psycopg.Connection, gender: Callable[[bytes], str], result: EnrichResult):
    """Assign a gender to every stored portrait that has none yet (see ``czn_data.gender``)."""
    portraits = conn.execute(
        """
        SELECT image_url, image FROM prydwen.portrait
        WHERE image_url NOT IN (SELECT image_url FROM prydwen.portrait_gender)
        ORDER BY image_url
        """
    ).fetchall()
    if portraits:
        log.info("tagging portraits", extra={"count": len(portraits)})
    for url, image in portraits:
        try:
            assigned = gender(image)
        except TaggerUnavailable as error:
            log.error("portrait tagger unavailable", extra={"error": str(error)})
            result.failures["gender"] += 1
            return
        except Exception as error:  # noqa: BLE001 - one bad portrait must not stop the rest
            log.error("portrait tagging failed", extra={"url": url, "error": repr(error)})
            result.failures["gender"] += 1
            continue
        conn.execute(
            "INSERT INTO prydwen.portrait_gender (image_url, gender, tagged_at)"
            " VALUES (%s, %s, %s)",
            (url, assigned, datetime.now(UTC)),
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
        for step in ("gender",):
            failures.set(result.failures[step], {"step": step})

    return metrics.export("czn_data.enrich", record)
