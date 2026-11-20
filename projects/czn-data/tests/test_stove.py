"""Contract: a Stove run stores every post of the chosen boards, refetches only edited posts, and
never replaces or drops stored posts because of a failure.

Stove's API is replaced by an in-memory one (URL -> JSON text), shaped like its real responses;
the database is real Postgres.
"""

import json
from urllib.parse import parse_qs

import pytest

from czn_data.cli import main
from czn_data.fetch import FetchFailed
from czn_data.stove import API, BOARDS, Client, scrape_stove

UPDATES, NEWS, EVENTS = BOARDS


def api(boards: dict[int, list[dict]], contents: dict[str, str | Exception]):
    """Stove's API serving ``boards`` (board -> posts, paged by the request's size) and each
    post's ``contents``. Records every article fetched in ``get.articles``."""
    articles = []

    def get(url: str) -> str:
        path, query = url.removeprefix(f"{API}/").split("?", 1)
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if path == "article":
            articles.append(params["article_id"])
            content = contents[params["article_id"]]
            if isinstance(content, Exception):
                raise content
            value = {"article_id": params["article_id"], "content": content}
        else:
            posts = boards.get(int(path.split("/")[2]), [])
            page, size = int(params["page"]), int(params["size"])
            value = {"total": len(posts), "list": posts[(page - 1) * size : page * size]}
        return json.dumps({"code": 0, "message": "Success", "value": value})

    get.articles = articles
    return get


def post(article_id: str, updated: int = 1_795_000_000_000) -> dict:
    return {
        "article_id": article_id,
        "title": f" Post {article_id} ",
        "create_datetime": 1_794_000_000_000,
        "update_datetime": updated,
    }


def stored(conn) -> dict[str, tuple[int, str, str]]:
    rows = conn.execute("SELECT article_id, board, title, content FROM stove.article").fetchall()
    return {article_id: (board, title, content) for article_id, board, title, content in rows}


def run(conn, get, allow_shrink=False):
    return scrape_stove(conn, Client(get), allow_shrink)


def test_clean_run_stores_every_post_of_each_board(conn, monkeypatch):
    monkeypatch.setattr("czn_data.stove.PAGE_SIZE", 2)  # three posts span two list pages
    get = api({UPDATES: [post("1"), post("2"), post("3")], EVENTS: [post("4")]}, {
        "1": "<p>a</p>", "2": "<p>b</p>", "3": "<p>c</p>", "4": "<p>d</p>"
    })  # fmt: skip

    result = run(conn, get)

    assert result.ok
    assert stored(conn) == {
        "1": (UPDATES, "Post 1", "<p>a</p>"),
        "2": (UPDATES, "Post 2", "<p>b</p>"),
        "3": (UPDATES, "Post 3", "<p>c</p>"),
        "4": (EVENTS, "Post 4", "<p>d</p>"),
    }
    (ok,) = conn.execute("SELECT ok FROM stove.run").fetchone()
    assert ok


def test_only_edited_posts_are_fetched_again(conn):
    run(conn, api({NEWS: [post("1"), post("2")]}, {"1": "old", "2": "same"}))
    get = api({NEWS: [post("1", updated=1_796_000_000_000), post("2")]}, {"1": "new"})

    result = run(conn, get)

    assert result.ok
    assert get.articles == ["1"]
    assert {k: v[2] for k, v in stored(conn).items()} == {"1": "new", "2": "same"}


def test_failed_post_keeps_its_previous_version_and_fails_the_run(conn):
    run(conn, api({NEWS: [post("1")]}, {"1": "old"}))
    edited = [post("1", updated=1_796_000_000_000)]

    result = run(conn, api({NEWS: edited}, {"1": FetchFailed("HTTP 500")}))

    assert not result.ok
    assert stored(conn)["1"][2] == "old"
    assert conn.execute("SELECT ok FROM stove.run ORDER BY started_at DESC").fetchone() == (False,)


def test_post_without_content_is_invalid_and_not_stored(conn):
    result = run(conn, api({NEWS: [post("1")]}, {"1": " "}))

    assert not result.ok
    assert result.validation_failures == {"article.content": 1}
    assert stored(conn) == {}


def test_board_that_shrinks_sharply_keeps_its_posts(conn):
    posts = [post(str(n)) for n in range(10)]
    run(conn, api({NEWS: posts}, {str(n): "x" for n in range(10)}))

    shrunk = run(conn, api({NEWS: posts[:2]}, {}))
    assert not shrunk.ok
    assert len(stored(conn)) == 10

    accepted = run(conn, api({NEWS: posts[:2]}, {}), allow_shrink=True)
    assert accepted.ok
    assert set(stored(conn)) == {"0", "1"}


def test_post_removed_from_its_board_is_deleted(conn):
    posts = [post(str(n)) for n in range(6)]
    run(conn, api({NEWS: posts}, {str(n): "x" for n in range(6)}))

    result = run(conn, api({NEWS: posts[1:]}, {}))

    assert result.ok
    assert set(stored(conn)) == {"1", "2", "3", "4", "5"}


def test_list_that_changed_shape_fails_and_keeps_posts(conn):
    run(conn, api({NEWS: [post("1")]}, {"1": "x"}))
    broken = [{k: v for k, v in post("1").items() if k != "update_datetime"}]

    result = run(conn, api({NEWS: broken}, {}))

    assert not result.ok
    assert result.validation_failures == {"list.update_datetime": 1}
    assert set(stored(conn)) == {"1"}


def test_scrape_stove_command_exits_nonzero_when_stove_fails(conn, database_url, monkeypatch):
    class Unreachable:
        def __init__(self, **_):
            pass

        def get(self, url: str) -> str:
            raise FetchFailed(f"{url}: HTTP 503")

    monkeypatch.setattr("czn_data.fetch.Fetcher", Unreachable)
    monkeypatch.setenv("DATABASE_URL", database_url)
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr("sys.argv", ["czn-data", "scrape-stove"])

    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 1
