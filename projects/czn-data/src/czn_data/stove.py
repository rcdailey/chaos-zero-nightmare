"""Fetch the game's official Stove community posts into the ``stove`` schema, as received.

Stove's community site loads its boards from an undocumented JSON API that needs no login
(the pattern comes from the open-source EmberLauncher). Board lists carry titles and edit times
but never the post body, so each post is fetched on its own, and only when its edit time
changed. A post that fails keeps its previous version; a board whose list fails or shrinks
sharply keeps every stored post. Either makes the run fail.
"""

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from opentelemetry.metrics import Meter

from . import metrics
from .fetch import Blocked, FetchFailed
from .parse import ParseError
from .store import SHRINK_LIMIT

log = logging.getLogger(__name__)

API = "https://api.onstove.com/cwms/v3.0"
POST_URL = "https://page.onstove.com/chaoszeronightmare/en/view/{}"
# Board id -> name. Announcements (139238) and Known Issues (142488) are left out: they hold
# maintenance, ban, and bug notices rather than game content.
BOARDS = {142421: "Update Notice", 139239: "News", 139240: "Current Events"}
PAGE_SIZE = 100
# Stove publishes no crawl delay; one request per second keeps the load small.
DELAY_SECONDS = 1.0
LIST_KEYS = {"article_id", "title", "create_datetime", "update_datetime"}


@dataclass
class StoveResult:
    requests: Counter = field(default_factory=Counter)  # ok, fetch_error, invalid -> count
    validation_failures: Counter = field(default_factory=Counter)  # field -> count
    articles: Counter = field(default_factory=Counter)  # fetched, unchanged, removed -> count
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.blocked
            or self.validation_failures
            or self.requests["fetch_error"]
            or self.requests["invalid"]
        )


class Client:
    """Fetches Stove API paths through ``get`` (URL -> response text) and unwraps the
    ``{"code": 0, "value": ...}`` envelope."""

    def __init__(self, get: Callable[[str], str]):
        self._get = get

    def get(self, path: str) -> Any:
        text = self._get(f"{API}/{path}")
        try:
            body = json.loads(text)
        except ValueError as error:
            raise ParseError("response.json", repr(error)) from error
        if not isinstance(body, dict) or body.get("code") != 0 or "value" not in body:
            code = body.get("code") if isinstance(body, dict) else None
            raise ParseError("response.code", f"{path}: code {code!r}")
        return body["value"]

    def board(self, board: int) -> list[dict]:
        """Every post listed on a board, newest first."""
        posts: list[dict] = []
        page = 1
        while True:
            value = self.get(
                f"article_group/BOARD/{board}/article/list?content_yn=N&summary_yn=N"
                f"&sort_type_code=LATEST&translation_yn=N&page={page}&size={PAGE_SIZE}"
            )
            listed = value.get("list") if isinstance(value, dict) else None
            if not isinstance(listed, list) or not isinstance(value.get("total"), int):
                raise ParseError("list.shape", f"board {board}: no list or total")
            for post in listed:
                missing = LIST_KEYS - set(post) if isinstance(post, dict) else LIST_KEYS
                if missing:
                    raise ParseError(f"list.{min(missing)}", f"board {board}: post lacks {missing}")
            posts.extend(listed)
            if not listed or len(posts) >= value["total"]:
                return posts
            page += 1

    def content(self, article_id: str) -> str:
        value = self.get(f"article?article_id={article_id}&translation_yn=N&request_id=CM")
        content = value.get("content") if isinstance(value, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ParseError("article.content", f"article {article_id}: no content")
        return content


def scrape_stove(conn: psycopg.Connection, client: Client, allow_shrink: bool) -> StoveResult:
    result = StoveResult()
    started = datetime.now(UTC)
    errors = []
    for board, name in BOARDS.items():
        try:
            _scrape_board(conn, client, board, allow_shrink, result)
        except Blocked as failure:
            result.blocked = True
            errors.append(f"board {board}: blocked: {failure}")
            log.error("blocked, stopping", extra={"board": name, "error": str(failure)})
            break
        except (FetchFailed, ParseError) as failure:
            if isinstance(failure, ParseError):
                result.requests["invalid"] += 1
                result.validation_failures[failure.field] += 1
            else:
                result.requests["fetch_error"] += 1
            errors.append(f"board {board}: {failure}")
            log.error(
                "board failed, keeping its stored posts",
                extra={"board": name, "error": str(failure)},
            )
    conn.execute(
        "INSERT INTO stove.run (started_at, finished_at, ok, error) VALUES (%s, %s, %s, %s)",
        (started, datetime.now(UTC), result.ok, "; ".join(errors) or None),
    )
    return result


def _scrape_board(conn, client: Client, board: int, allow_shrink: bool, result) -> None:
    posts = client.board(board)
    result.requests["ok"] += 1
    stored = dict(
        conn.execute(
            "SELECT article_id, updated_at FROM stove.article WHERE board = %s", (board,)
        ).fetchall()
    )
    if not allow_shrink and stored and len(posts) < len(stored) * (1 - SHRINK_LIMIT):
        raise ParseError("list.count", f"board {board}: dropped from {len(stored)} to {len(posts)}")
    listed = set()
    for post in posts:
        article_id = str(post["article_id"])
        listed.add(article_id)
        updated_at = _time(post["update_datetime"])
        if stored.get(article_id) == updated_at:
            result.articles["unchanged"] += 1
            continue
        try:
            content = client.content(article_id)
        except FetchFailed as failure:
            result.requests["fetch_error"] += 1
            log.error("post fetch failed", extra={"article": article_id, "error": str(failure)})
            continue
        except ParseError as failure:
            result.requests["invalid"] += 1
            result.validation_failures[failure.field] += 1
            log.error("post invalid", extra={"article": article_id, "error": str(failure)})
            continue
        result.requests["ok"] += 1
        result.articles["fetched"] += 1
        conn.execute(
            """
            INSERT INTO stove.article
                (article_id, board, title, content, created_at, updated_at, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (article_id) DO UPDATE SET board = excluded.board,
                title = excluded.title, content = excluded.content,
                created_at = excluded.created_at, updated_at = excluded.updated_at,
                fetched_at = excluded.fetched_at
            """,
            (
                article_id,
                board,
                post["title"].strip(),
                content,
                _time(post["create_datetime"]),
                updated_at,
            ),
        )
    removed = set(stored) - listed
    if removed:
        conn.execute("DELETE FROM stove.article WHERE article_id = ANY (%s)", (list(removed),))
        result.articles["removed"] += len(removed)


def _time(epoch_ms: Any) -> datetime:
    if not isinstance(epoch_ms, int):
        raise ParseError("list.update_datetime", f"not epoch milliseconds: {epoch_ms!r}")
    return datetime.fromtimestamp(epoch_ms / 1000, UTC)


def export_metrics(result: StoveResult, started: float) -> bool:
    """Send the run's metrics once (see ``czn_data.metrics``); False if the export failed."""

    def record(meter: Meter) -> None:
        now = time.time()

        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.stove.success", "1 when the run had no failures").set(int(result.ok))
        gauge("czn.stove.blocked", "1 when the site blocked the run").set(int(result.blocked))
        gauge("czn.stove.last_run.timestamp", "End of the run", "s").set(now)
        if result.ok:
            gauge("czn.stove.last_success.timestamp", "End of the last successful run", "s").set(
                now
            )
        gauge("czn.stove.duration", "Run duration", "s").set(now - started)
        requests = gauge("czn.stove.requests", "API requests by result")
        for outcome in ("ok", "fetch_error", "invalid"):
            requests.set(result.requests[outcome], {"result": outcome})
        articles = gauge("czn.stove.articles", "Posts by what the run did with them")
        for outcome in ("fetched", "unchanged", "removed"):
            articles.set(result.articles[outcome], {"result": outcome})
        failures = gauge("czn.stove.validation_failures", "Responses that no longer validate")
        for field_name, count in result.validation_failures.items():
            failures.set(count, {"field": field_name})

    return metrics.export("czn_data.stove", record)
