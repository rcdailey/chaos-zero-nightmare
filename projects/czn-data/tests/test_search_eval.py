"""Contract: the daily run measures search again only when the passages or search code changed
since the last measurement; the eval-search command always measures.

Real Postgres; the search models are the word-overlap fakes from conftest.
"""

import logging
import sys

import pytest

from czn_data import cli, digest


@pytest.fixture
def evaluations(database_url, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", database_url)
    caplog.set_level(logging.INFO, logger="czn_data")
    return lambda: sum(r.getMessage() == "search evaluated" for r in caplog.records)


@pytest.fixture
def run(monkeypatch):
    """Runs one czn-data command; the daily run's scrapes and enrich are replaced by steps
    that fetch nothing and succeed."""
    for step in ("_scrape_e7bot", "_scrape_stove", "_scrape_game8", "_scrape_gamefiles"):
        monkeypatch.setattr(cli, step, lambda allow_shrink: True)
    monkeypatch.setattr(cli, "_prydwen", lambda command, allow_shrink: True)
    monkeypatch.setattr(cli, "_enrich", lambda: True)

    def run(command: str) -> None:
        monkeypatch.setattr(sys, "argv", ["czn-data", command])
        with pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code == 0

    return run


def add_passage(conn, text: str) -> None:
    conn.execute(
        "INSERT INTO passage (source, key, position, title, text, embedding)"
        " VALUES ('post', '1', 0, 'Notice', %s, '{1}')",
        (text,),
    )


def post(conn, content: str) -> None:
    """One Stove post, which the daily run's build turns into passages."""
    conn.execute(
        "INSERT INTO stove.article"
        " (article_id, board, title, content, created_at, updated_at, fetched_at)"
        " VALUES (1, 1, 'Notice', %s, now(), now(), now())"
        " ON CONFLICT (article_id) DO UPDATE SET content = excluded.content",
        (content,),
    )


def test_daily_run_measures_search_again_only_after_passages_change(conn, evaluations, run):
    post(conn, "<p>Maintenance on Monday.</p>")
    run("daily")
    run("daily")
    assert evaluations() == 1

    post(conn, "<p>Maintenance on Tuesday.</p>")
    run("daily")
    assert evaluations() == 2


def test_daily_run_measures_search_after_a_search_code_change(conn, evaluations, run, monkeypatch):
    post(conn, "<p>Maintenance on Monday.</p>")
    run("daily")
    code = digest.code
    monkeypatch.setattr(
        digest,
        "code",
        lambda *modules: code(*modules) + ("edited" * ("czn_data.search_eval" in modules)),
    )

    run("daily")

    assert evaluations() == 2


def test_eval_search_command_always_measures(conn, evaluations, run):
    add_passage(conn, "Maintenance on Monday.")
    run("eval-search")
    run("eval-search")
    assert evaluations() == 2
