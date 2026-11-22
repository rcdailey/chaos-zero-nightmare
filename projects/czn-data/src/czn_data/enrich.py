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
    code: str,
    identity_print: Callable[[bytes], list[float]],
    result: EnrichResult,
):
    """Store the identity print of every portrait that has none from the print code
    fingerprinted as ``code`` yet (see ``czn_data.identity``), and drop prints of other code."""
    conn.execute("DELETE FROM prydwen.portrait_identity WHERE digest <> %s", (code,))
    _tag(conn, "identity", "portrait_identity", "print", identity_print, result, code)


def compute_gear_prints(
    conn: psycopg.Connection,
    code: str,
    gear_prints: Callable[[bytes, list[bytes]], list[list[float]]],
    result: EnrichResult,
):
    """Store the prints of every equipment icon that has none from the print code fingerprinted
    as ``code`` yet, one per rarity background (see ``czn_data.gear``), and drop prints of
    other code; without stored backgrounds nothing can be printed."""
    conn.execute("DELETE FROM e7bot.gear_print WHERE digest <> %s", (code,))
    from .e7bot import GEAR_ICON, RARITY_BACKGROUNDS

    backgrounds = dict(
        conn.execute(
            "SELECT path, image FROM e7bot.asset WHERE path = ANY(%s)", (list(RARITY_BACKGROUNDS),)
        ).fetchall()
    )
    prefix, suffix = GEAR_ICON.split("{}")
    printed = {row[0] for row in conn.execute("SELECT icon_name FROM e7bot.gear_print")}
    icons = [
        (icon_name, image)
        for path, image in conn.execute(
            "SELECT path, image FROM e7bot.asset WHERE starts_with(path, %s) ORDER BY path",
            (prefix,),
        )
        if (icon_name := path.removeprefix(prefix).removesuffix(suffix)) not in printed
    ]
    if not icons:
        return
    if len(backgrounds) < len(RARITY_BACKGROUNDS):
        log.error("rarity backgrounds missing", extra={"step": "gear"})
        result.failures["gear"] += 1
        return
    log.info("printing gear icons", extra={"count": len(icons)})
    drawn_on = [backgrounds[path] for path in RARITY_BACKGROUNDS]
    for icon_name, image in icons:
        try:
            prints = gear_prints(image, drawn_on)
        except ModelUnavailable as error:
            log.error("gear model unavailable", extra={"error": str(error)})
            result.failures["gear"] += 1
            return
        except Exception as error:  # noqa: BLE001 - one bad icon must not stop the rest
            log.error("gear printing failed", extra={"icon": icon_name, "error": repr(error)})
            result.failures["gear"] += 1
            continue
        conn.execute(
            "INSERT INTO e7bot.gear_print (icon_name, digest, prints, tagged_at)"
            " VALUES (%s, %s, %s, %s)",
            (icon_name, code, prints, datetime.now(UTC)),
        )


def _tag(
    conn: psycopg.Connection,
    step: str,
    table: str,
    column: str,
    tag: Callable[[bytes], object],
    result: EnrichResult,
    code: str | None = None,
):
    """Store ``tag`` of each portrait missing from ``table`` (for the print code ``code``, when
    the table keeps one row per code); a failure counts against ``step``."""
    done = f"SELECT image_url FROM prydwen.{table}" + (" WHERE digest = %(code)s" if code else "")
    portraits = conn.execute(
        f"""
        SELECT image_url, image FROM prydwen.portrait
        WHERE image_url NOT IN ({done})
        ORDER BY image_url
        """,
        {"code": code},
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
        if code:
            row["digest"] = code
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
        for step in ("gender", "identity", "gear"):
            failures.set(result.failures[step], {"step": step})

    return metrics.export("czn_data.enrich", record)
