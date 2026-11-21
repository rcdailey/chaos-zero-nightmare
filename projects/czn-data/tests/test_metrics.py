"""Contract: a scrape or enrich run's metrics reach VictoriaMetrics over OTLP under the names the
alert rules query, and each alert fires on a bad run and resolves after the next good one.

Runs against real VictoriaMetrics started with -opentelemetry.usePrometheusNaming, the flag
the cluster uses, so the stored names come from VictoriaMetrics' own OTLP translation.
"""

import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

import pytest
import yaml

from czn_data import enrich, store, stove
from czn_data.cli import main
from czn_data.fetch import FetchFailed
from czn_data.scrape import RunResult, export_metrics

ALERTS = yaml.safe_load((Path(__file__).parents[1] / "deploy" / "alerts.yaml").read_text())
ALERT_EXPRS = {rule["alert"]: rule["expr"] for rule in ALERTS["groups"][0]["rules"]}


@pytest.fixture
def vm(monkeypatch):
    url = os.environ.get("TEST_VM_URL")
    if not url:
        pytest.skip("TEST_VM_URL not set; run `mise run test` to start VictoriaMetrics")
    match = urllib.parse.urlencode({"match[]": '{__name__=~"czn_.*"}'})
    request = urllib.request.Request(
        f"{url}/api/v1/admin/tsdb/delete_series?{match}", method="POST"
    )
    urllib.request.urlopen(request, timeout=10).close()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"{url}/opentelemetry")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "czn-data")
    return url


def query(vm: str, expr: str, expect_result: bool = True) -> list[dict]:
    """Instant query, polled briefly: series re-created after the fixture's delete take a
    moment to become searchable."""
    params = urllib.parse.urlencode({"query": expr, "nocache": "1"})
    deadline = time.monotonic() + 10
    while True:
        urllib.request.urlopen(f"{vm}/internal/force_flush", timeout=10).close()
        with urllib.request.urlopen(f"{vm}/api/v1/query?{params}", timeout=10) as response:
            result = json.load(response)["data"]["result"]
        if bool(result) == expect_result or time.monotonic() > deadline:
            return result
        time.sleep(0.2)


def export_run(conn, monkeypatch, pages: dict | None = None, **flags) -> None:
    """One CronJob run. Each run is a new process, so its series carry a new
    service.instance.id; alerts must read the newest run, not any run in the window."""
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"service.instance.id={uuid.uuid4()}")
    result = RunResult(pages=Counter({"ok": 3} | (pages or {})), **flags)
    assert export_metrics(conn, result, started=0)


LABELS = {"result", "kind", "field", "table"}


def test_every_metric_arrives_under_the_documented_names(conn, vm):
    result = RunResult(pages=Counter({"ok": 1, "parse_error": 1}))
    result.validation_failures[("character", "card.count")] = 1
    assert export_metrics(conn, result, started=0)

    # Querying a series that arrives in the same export waits until the export is searchable.
    query(vm, "czn_data_rows")
    stored = {
        (s["metric"]["__name__"], tuple(sorted(k for k in s["metric"] if k in LABELS)))
        for s in query(vm, '{__name__=~"czn_.*"}')
    }
    assert stored == {
        ("czn_scrape_success", ()),
        ("czn_scrape_blocked", ()),
        ("czn_scrape_discovery_failed", ()),
        ("czn_scrape_last_run_timestamp_seconds", ()),
        ("czn_scrape_duration_seconds", ()),
        ("czn_scrape_pages", ("result",)),
        ("czn_scrape_fetches", ("result",)),
        ("czn_scrape_validation_failures", ("field", "kind")),
        ("czn_data_rows", ("table",)),
    }
    (failure,) = query(vm, "czn_scrape_validation_failures")
    assert (failure["metric"]["kind"], failure["metric"]["field"]) == ("character", "card.count")
    assert query(vm, 'czn_scrape_pages{result="parse_error"}')[0]["value"][1] == "1"
    tables = {s["metric"]["table"] for s in query(vm, "czn_data_rows")}
    assert tables == {*store.DERIVED_TABLES, "document"}


@pytest.mark.parametrize(
    ("alert", "bad_run"),
    [
        ("CznScrapeStale", {"blocked": True}),
        ("CznScrapeBlocked", {"blocked": True}),
        ("CznScrapeDiscoveryFailed", {"discovery_failed": True}),
        ("CznScrapeLayoutChanged", {"pages": {"parse_error": 2}}),
        ("CznScrapeFetchErrors", {"pages": {"fetch_error": 1}}),
    ],
)
def test_alert_fires_on_bad_run_and_resolves_after_good_run(conn, vm, monkeypatch, alert, bad_run):
    export_run(conn, monkeypatch, **bad_run)
    assert query(vm, ALERT_EXPRS[alert]), f"{alert} did not fire"

    export_run(conn, monkeypatch)
    assert not query(vm, ALERT_EXPRS[alert], expect_result=False), f"{alert} did not resolve"


def test_stale_alert_waits_for_the_first_run_on_a_fresh_install(conn, vm, monkeypatch):
    assert not query(vm, ALERT_EXPRS["CznScrapeStale"], expect_result=False)

    export_run(conn, monkeypatch, blocked=True)
    assert query(vm, ALERT_EXPRS["CznScrapeStale"]), "CznScrapeStale did not fire"


def export_enrich_run(monkeypatch, **failures) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"service.instance.id={uuid.uuid4()}")
    assert enrich.export_metrics(enrich.EnrichResult(Counter(failures)), started=0)


def test_enrich_metrics_arrive_under_the_documented_names(vm, monkeypatch):
    export_enrich_run(monkeypatch, gender=2)

    query(vm, "czn_enrich_failures")
    stored = {
        (s["metric"]["__name__"], tuple(sorted(k for k in s["metric"] if k == "step")))
        for s in query(vm, '{__name__=~"czn_.*"}')
    }
    assert stored == {
        ("czn_enrich_success", ()),
        ("czn_enrich_last_run_timestamp_seconds", ()),
        ("czn_enrich_duration_seconds", ()),
        ("czn_enrich_failures", ("step",)),
    }
    assert query(vm, 'czn_enrich_failures{step="gender"}')[0]["value"][1] == "2"


@pytest.mark.parametrize("alert", ["CznEnrichStale", "CznEnrichFailed"])
def test_enrich_alert_fires_on_bad_run_and_resolves_after_good_run(vm, monkeypatch, alert):
    assert not query(vm, ALERT_EXPRS[alert], expect_result=False)

    export_enrich_run(monkeypatch, gender=1)
    assert query(vm, ALERT_EXPRS[alert]), f"{alert} did not fire"

    export_enrich_run(monkeypatch)
    assert not query(vm, ALERT_EXPRS[alert], expect_result=False), f"{alert} did not resolve"


def export_stove_run(monkeypatch, **failures) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"service.instance.id={uuid.uuid4()}")
    result = stove.StoveResult(
        requests=Counter({"ok": 3}), articles=Counter({"fetched": 2, "unchanged": 1})
    )
    result.validation_failures.update(failures)
    assert stove.export_metrics(result, started=0)


def test_stove_metrics_arrive_under_the_documented_names(vm, monkeypatch):
    export_stove_run(monkeypatch, **{"list.count": 1})

    query(vm, "czn_stove_validation_failures")
    stored = {
        (s["metric"]["__name__"], tuple(sorted(k for k in s["metric"] if k in LABELS)))
        for s in query(vm, '{__name__=~"czn_.*"}')
    }
    assert stored == {
        ("czn_stove_success", ()),
        ("czn_stove_blocked", ()),
        ("czn_stove_last_run_timestamp_seconds", ()),
        ("czn_stove_duration_seconds", ()),
        ("czn_stove_requests", ("result",)),
        ("czn_stove_articles", ("result",)),
        ("czn_stove_validation_failures", ("field",)),
    }


@pytest.mark.parametrize("alert", ["CznStoveStale", "CznStoveFailed"])
def test_stove_alert_fires_on_bad_run_and_resolves_after_good_run(vm, monkeypatch, alert):
    assert not query(vm, ALERT_EXPRS[alert], expect_result=False)

    export_stove_run(monkeypatch, **{"list.count": 1})
    assert query(vm, ALERT_EXPRS[alert]), f"{alert} did not fire"

    export_stove_run(monkeypatch)
    assert not query(vm, ALERT_EXPRS[alert], expect_result=False), f"{alert} did not resolve"


def test_daily_run_enriches_and_builds_after_failed_scrapes(conn, database_url, vm, monkeypatch):
    class Unreachable:
        def __init__(self, **_):
            pass

        def get(self, url: str) -> str:
            raise FetchFailed(f"{url}: HTTP 503")

    monkeypatch.setattr("czn_data.fetch.Fetcher", Unreachable)
    monkeypatch.setattr("czn_data.e7bot.Fetcher", Unreachable)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr("sys.argv", ["czn-data", "daily"])

    with pytest.raises(SystemExit) as exit_:
        main()

    assert exit_.value.code == 1
    for source in ("scrape", "e7bot", "stove"):
        assert query(vm, f"czn_{source}_success")[0]["value"][1] == "0", source
    assert query(vm, "czn_enrich_success")[0]["value"][1] == "1"
    assert query(vm, "czn_build_success")[0]["value"][1] == "1"


def test_no_endpoint_configured_exports_nothing_without_error(conn, monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    assert export_metrics(conn, RunResult(), started=0)


def test_unreachable_endpoint_reports_failure(conn, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2")
    assert not export_metrics(conn, RunResult(), started=0)
