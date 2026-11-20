"""Command line entry point:
``czn-data scrape | scrape-e7bot | scrape-stove | reparse | build | enrich | migrate | serve``.

Configuration comes from environment variables, documented in the project README.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

import psycopg

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

    migrate(os.environ["DATABASE_URL"])
    if args.command == "migrate":
        return
    if args.command == "enrich":
        sys.exit(_run_enrich())
    if args.command == "build":
        sys.exit(_run_build())
    if args.command == "scrape-e7bot":
        sys.exit(_run_e7bot(args.allow_shrink))
    if args.command == "scrape-stove":
        sys.exit(_run_stove(args.allow_shrink))
    sys.exit(_run_job(args.command, args.allow_shrink))


def _build(conn: psycopg.Connection):
    """Run the build; a failure is logged and returned as None so the job still reports."""
    from .build import build

    try:
        return build(conn)
    except Exception:
        logging.getLogger("czn_data").exception("build failed, keeping previous merged data")
        return None


def _run_build() -> int:
    from .build import export_metrics

    started = time.time()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        result = _build(conn)
    return 0 if result is not None and _export(lambda: export_metrics(result, started)) else 1


def _run_e7bot(allow_shrink: bool) -> int:
    from . import build, e7bot

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "scrape-e7bot"})
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        client = e7bot.Client(e7bot.threaded_get())
        result = e7bot.scrape_e7bot(conn, client, allow_shrink)
        built = _build(conn)
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
        exported = _export(lambda: e7bot.export_metrics(result, started))
    exported = _export(lambda: build.export_metrics(built, started)) and exported
    return 0 if result.ok and built is not None and exported else 1


def _run_stove(allow_shrink: bool) -> int:
    from . import build, stove
    from .fetch import Fetcher

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "scrape-stove"})
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        client = stove.Client(Fetcher(delay=stove.DELAY_SECONDS).get)
        result = stove.scrape_stove(conn, client, allow_shrink)
        built = _build(conn)
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
    exported = _export(lambda: stove.export_metrics(result, started))
    exported = _export(lambda: build.export_metrics(built, started)) and exported
    return 0 if result.ok and built is not None and exported else 1


def _export(send) -> bool:
    try:
        return send()
    except Exception:
        logging.getLogger("czn_data").exception("metrics export failed")
        return False


def _run_enrich() -> int:
    from .enrich import EnrichResult, export_metrics, tag_genders
    from .gender import Tagger

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "enrich"})
    result = EnrichResult()
    from .build import export_metrics as build_metrics

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        tag_genders(conn, Tagger().gender, result)
        # Merged characters that only Prydwen has take their gender from the tags.
        built = _build(conn)
    log.info(
        "run finished",
        extra={"command": "enrich", "ok": result.ok, "failures": dict(result.failures)},
    )
    exported = _export(lambda: export_metrics(result, started))
    exported = _export(lambda: build_metrics(built, started)) and exported
    return 0 if result.ok and built is not None and exported else 1


def _run_job(command: str, allow_shrink: bool) -> int:
    from .build import export_metrics as build_metrics
    from .fetch import Fetcher
    from .scrape import export_metrics, fetch_portraits, reparse, scrape

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": command})
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        if command == "scrape":
            fetcher = Fetcher()
            result = scrape(conn, fetcher.get, allow_shrink)
            if not (result.blocked or result.discovery_failed):
                fetch_portraits(conn, fetcher.get_bytes, result)
        else:
            result = reparse(conn, allow_shrink)
        built = _build(conn)
        log.info(
            "run finished",
            extra={
                "command": command,
                "ok": result.ok,
                "pages": dict(result.pages),
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
    exported = _export(lambda: build_metrics(built, started)) and exported
    return 0 if result.ok and built is not None and exported else 1
