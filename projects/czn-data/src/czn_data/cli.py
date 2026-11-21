"""Command line entry point:
``czn-data daily | scrape | scrape-e7bot | scrape-stove | reparse | build | enrich | migrate |
serve``.

Configuration comes from environment variables, documented in the project README.
"""

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg

# Arbitrary constant shared by every czn-data process; distinct from the migration and build locks.
RUN_LOCK = 7_418_023_357

# Attributes every LogRecord has; anything else was passed through ``extra=`` and is logged.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the field names vlagent maps to _msg and _time."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry |= {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(prog="czn-data")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("daily", "run every scrape in parallel, then enrich, then build the merged tables"),
        ("scrape", "fetch every Prydwen CZN page, then build the merged tables"),
        ("scrape-e7bot", "fetch e7bot's CZN game data, then build the merged tables"),
        ("scrape-stove", "fetch the official Stove posts, then build the merged tables"),
        ("reparse", "parse the stored Prydwen HTML again without fetching, then build"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--allow-shrink",
            action="store_true",
            help="accept pages or lists that lose many rows (the source really removed content)",
        )
    commands.add_parser("build", help="merge the stored sources into the tables the MCP reads")
    commands.add_parser(
        "enrich", help="derive data (character gender) from the stored scrape, without fetching"
    )
    commands.add_parser("migrate", help="upgrade the database schema, then exit")
    serve_command = commands.add_parser("serve", help="run the MCP server")
    serve_command.add_argument(
        "--stdio", action="store_true", help="serve a local client over stdin/stdout"
    )
    args = parser.parse_args()

    # stdout carries the MCP protocol in stdio mode, so logs go to stderr there.
    handler = logging.StreamHandler(sys.stderr if getattr(args, "stdio", False) else sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler])

    if args.command == "serve":
        from .server import serve, serve_stdio

        if args.stdio:
            serve_stdio()
        else:
            serve()
        return
    from .migrate import migrate

    if args.command in ("migrate", "build"):
        migrate(os.environ["DATABASE_URL"])
        if args.command == "build":
            sys.exit(0 if _guarded(_build_step) else 1)
        return
    # Scrapes and enrich insert rows without conflict handling, and two Prydwen crawls at once
    # double the load Cloudflare sees, so a run that starts during another one (a deploy's run
    # during the daily CronJob) skips. The lock comes before migrating because a migration can
    # reparse or build. Closing the connection on exit releases the lock.
    with _connect() as lock:
        if not lock.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK,)).fetchone()[0]:
            logging.getLogger("czn_data").warning(
                "another run is in progress, skipping", extra={"command": args.command}
            )
            return
        migrate(os.environ["DATABASE_URL"])
        _run_locked(args)


def _run_locked(args: argparse.Namespace) -> None:
    if args.command == "daily":
        sys.exit(_run_daily(args.allow_shrink))
    steps = {
        "enrich": _enrich,
        "scrape-e7bot": lambda: _scrape_e7bot(args.allow_shrink),
        "scrape-stove": lambda: _scrape_stove(args.allow_shrink),
        "scrape": lambda: _prydwen("scrape", args.allow_shrink),
        "reparse": lambda: _prydwen("reparse", args.allow_shrink),
    }
    sys.exit(_then_build(steps[args.command]))


def _then_build(step: Callable[[], bool]) -> int:
    """Exit code of ``step`` followed by a build, for the commands that run one source."""
    ok = _guarded(step)
    return 0 if _build_step() and ok else 1


def _run_daily(allow_shrink: bool) -> int:
    """The scheduled run: every scrape at once, then enrich, then one build.

    The scrapes write disjoint schemas and fetch different sites, so they share nothing. Enrich
    tags the card art the Prydwen scrape stores, and the build merges all of it, so both wait.
    A failed step fails the run but never stops a later one, which then uses the last good data.
    """
    scrapes = (
        lambda: _prydwen("scrape", allow_shrink),
        lambda: _scrape_e7bot(allow_shrink),
        lambda: _scrape_stove(allow_shrink),
    )
    with ThreadPoolExecutor(max_workers=len(scrapes)) as pool:
        ok = all(list(pool.map(_guarded, scrapes)))
    ok = _guarded(_enrich) and ok
    return 0 if _build_step() and ok else 1


def _guarded(step: Callable[[], bool]) -> bool:
    """Run one step; an unexpected error fails it without stopping the steps after it."""
    try:
        return step()
    except Exception:
        logging.getLogger("czn_data").exception("step crashed")
        return False


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def _build(conn: psycopg.Connection):
    """Run the build; a failure is logged and returned as None so the job still reports."""
    from .build import build

    try:
        return build(conn)
    except Exception:
        logging.getLogger("czn_data").exception("build failed, keeping previous merged data")
        return None


def _build_step() -> bool:
    from .build import export_metrics

    started = time.time()
    with _connect() as conn:
        result = _build(conn)
    exported = _export(lambda: export_metrics(result, started))
    return result is not None and exported


def _scrape_e7bot(allow_shrink: bool) -> bool:
    from . import e7bot

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "scrape-e7bot"})
    with _connect() as conn:
        client = e7bot.Client(e7bot.threaded_get())
        result = e7bot.scrape_e7bot(conn, client, allow_shrink)
    log.info(
        "run finished",
        extra={
            "command": "scrape-e7bot",
            "ok": result.ok,
            "requests": dict(result.requests),
            "blocked": result.blocked,
            "validation_failures": dict(result.validation_failures),
        },
    )
    return _export(lambda: e7bot.export_metrics(result, started)) and result.ok


def _scrape_stove(allow_shrink: bool) -> bool:
    from . import stove
    from .fetch import Fetcher

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "scrape-stove"})
    with _connect() as conn:
        client = stove.Client(Fetcher(delay=stove.DELAY_SECONDS).get)
        result = stove.scrape_stove(conn, client, allow_shrink)
    log.info(
        "run finished",
        extra={
            "command": "scrape-stove",
            "ok": result.ok,
            "requests": dict(result.requests),
            "articles": dict(result.articles),
            "blocked": result.blocked,
            "validation_failures": dict(result.validation_failures),
        },
    )
    return _export(lambda: stove.export_metrics(result, started)) and result.ok


def _export(send) -> bool:
    try:
        return send()
    except Exception:
        logging.getLogger("czn_data").exception("metrics export failed")
        return False


def _enrich() -> bool:
    from .enrich import EnrichResult, compute_identities, export_metrics, tag_genders
    from .identity import MODEL, Identifier
    from .tagger import Tagger

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "enrich"})
    result = EnrichResult()
    with _connect() as conn:
        tag_genders(conn, Tagger().gender, result)
        compute_identities(conn, MODEL, Identifier().print_of, result)
    log.info(
        "run finished",
        extra={"command": "enrich", "ok": result.ok, "failures": dict(result.failures)},
    )
    return _export(lambda: export_metrics(result, started)) and result.ok


def _prydwen(command: str, allow_shrink: bool) -> bool:
    from .fetch import Fetcher
    from .scrape import export_metrics, fetch_portraits, reparse, scrape

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": command})
    with _connect() as conn:
        if command == "scrape":
            fetcher = Fetcher()
            result = scrape(conn, fetcher.get, allow_shrink)
            if not (result.blocked or result.discovery_failed):
                fetch_portraits(conn, fetcher.get_bytes, result)
        else:
            result = reparse(conn, allow_shrink)
        log.info(
            "run finished",
            extra={
                "command": command,
                "ok": result.ok,
                "pages": dict(result.pages),
                "fetches": dict(result.fetches),
                "blocked": result.blocked,
                "validation_failures": [
                    {"kind": k, "field": f, "count": n}
                    for (k, f), n in result.validation_failures.items()
                ],
            },
        )
        exported = True
        if command == "scrape":
            exported = _export(lambda: export_metrics(conn, result, started))
    return exported and result.ok
