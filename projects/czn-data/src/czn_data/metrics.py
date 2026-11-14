"""One-shot OTLP/HTTP metrics export for the batch jobs (``scrape`` and ``enrich``).

A job exits right after its run, so a periodic reader would race the exit. Instead every value
is recorded, collected once, and exported synchronously, and the export result is checked.
Configuration comes only from the standard OTEL_* variables (endpoint, headers, timeout,
OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES). With no OTLP endpoint set, nothing is sent.

Names and units are chosen so VictoriaMetrics with -opentelemetry.usePrometheusNaming stores
the Prometheus names documented in the README (``czn.scrape.pages`` -> ``czn_scrape_pages``;
unit ``s`` adds ``_seconds``). Count gauges stay unitless: unit "1" would add ``_ratio``.
"""

import logging
import os
from collections.abc import Callable

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
from opentelemetry.sdk.resources import Resource

log = logging.getLogger(__name__)


def export(scope: str, record: Callable[[Meter], None]) -> bool:
    """Export what ``record`` sets on the meter; return False if the export failed."""
    if not _configured():
        log.info("no OTLP metrics endpoint configured, skipping metrics export")
        return True
    reader = InMemoryMetricReader()
    record(MeterProvider(metric_readers=[reader], resource=Resource.create()).get_meter(scope))
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


def _configured() -> bool:
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    if os.environ.get("OTEL_METRICS_EXPORTER", "otlp").lower() != "otlp":
        return False
    return any(
        os.environ.get(name)
        for name in ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
    )
