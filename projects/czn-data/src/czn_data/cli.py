"""Command line entry point: ``czn-data scrape | reparse | enrich | migrate | serve``.

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
        ("scrape", "fetch every Prydwen CZN page and update the database"),
        ("reparse", "parse the stored HTML again without fetching"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--allow-shrink",
            action="store_true",
            help="accept pages that lose many rows (Prydwen really removed content)",
        )
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
    sys.exit(_run_job(args.command, args.allow_shrink))


def _run_enrich() -> int:
    from .enrich import EnrichResult, export_metrics, tag_genders
    from .gender import Tagger

    log = logging.getLogger("czn_data")
    started = time.time()
    log.info("run started", extra={"command": "enrich"})
    result = EnrichResult()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        tag_genders(conn, Tagger().gender, result)
    log.info(
        "run finished",
        extra={"command": "enrich", "ok": result.ok, "failures": dict(result.failures)},
    )
    try:
        exported = export_metrics(result, started)
    except Exception:
        log.exception("metrics export failed")
        exported = False
    return 0 if result.ok and exported else 1


def _run_job(command: str, allow_shrink: bool) -> int:
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
        if command == "scrape":
            try:
                exported = export_metrics(conn, result, started)
            except Exception:
                log.exception("metrics export failed")
                exported = False
            if not exported:
                return 1
    return 0 if result.ok else 1
