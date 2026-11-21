"""Contract: a failed step fails the daily run without stopping the steps after it, and a run
reports whether its metrics export succeeded.

Metric names as VictoriaMetrics stores them and the alert rules are cluster configuration, not
tested here.
"""

from datetime import UTC, datetime

import pytest
from test_server import CHARACTER_HTML, CHARACTER_URL

from czn_data import store
from czn_data.cli import main
from czn_data.fetch import FetchFailed
from czn_data.parse import page_kind, parse_page
from czn_data.scrape import RunResult, export_metrics


def test_daily_run_still_builds_after_every_scrape_fails(conn, database_url, monkeypatch):
    class Unreachable:
        def __init__(self, **_):
            pass

        def get(self, url: str) -> str:
            raise FetchFailed(f"{url}: HTTP 503")

    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    now = datetime.now(UTC)
    store.save_page(
        conn, CHARACTER_URL, page_kind(CHARACTER_URL), CHARACTER_HTML, now, parsed, None
    )
    monkeypatch.setattr("czn_data.fetch.Fetcher", Unreachable)
    monkeypatch.setattr("czn_data.e7bot.Fetcher", Unreachable)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr("sys.argv", ["czn-data", "daily"])

    with pytest.raises(SystemExit) as exit_:
        main()

    assert exit_.value.code == 1
    assert conn.execute("SELECT name FROM character").fetchall() == [("Mei Lin",)]


def test_no_endpoint_configured_exports_nothing_without_error(conn, monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    assert export_metrics(conn, RunResult(), started=0)


def test_unreachable_endpoint_reports_failure(conn, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "0.5")
    assert not export_metrics(conn, RunResult(), started=0)
