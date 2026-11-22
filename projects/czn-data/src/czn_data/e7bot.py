"""Fetch wiki.e7bot.top's Chaos Zero Nightmare data into the ``e7bot`` schema, as received.

e7bot serves the game's own tables through an undocumented JSON API. Responses are AES-256-CBC
encrypted with a key embedded in the site's public JavaScript (the first 16 bytes of the
payload are the IV). If the key or response shapes change, the run fails and the previous data
stays: list documents are replaced only when every path fetched and validated. Leaderboard Save
Data never changes once created, so each save is fetched once and stored as it arrives.
"""

import base64
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from opentelemetry.metrics import Meter
from psycopg.types.json import Jsonb

from . import metrics
from .fetch import Blocked, Fetcher, FetchFailed
from .parse import ParseError
from .store import SHRINK_LIMIT

log = logging.getLogger(__name__)

API = "https://wiki.e7bot.top/api/"
SITE = "https://wiki.e7bot.top/#/"
# e7bot's robots.txt sets no crawl delay. WORKERS requests run at once, each worker waiting
# DELAY_SECONDS between its own requests: about 4 requests per second at most.
DELAY_SECONDS = 1.0
WORKERS = 4
# Detail documents are refetched when e7bot imports new game data for their kind (its snapshot
# diff timestamp changes), and at least this often.
FULL_REFRESH = timedelta(days=7)
DIFFS = ("czn/snapshot/characters/diff", "czn/snapshot/encounters/diff")
KEY = b"2egkWEkLvR9idARLvjqfJdWKfR24HJgY"

# List paths and the keys every item must have. Detail paths are fetched per listed id.
LISTS = {
    "czn/characters/list": {"id", "char_type", "names", "ego", "job", "rarity", "gender_type"},
    "czn/cards/list": {
        "id",
        "name",
        "cost",
        "card_category",
        "rarity",
        "desc_display",
        "r_spark_cards",
        "base_combatant",
        "sort",
        "talent",
        "tooltips",
    },
    "czn/characters/partner/list/detail": {
        "id",
        "name",
        "rarity",
        "job",
        "partner_card",
        "partner_passives",
    },
    "czn/relics/list": {"id", "name", "rarity", "relic_category", "relic_type", "skills"},
    "czn/r-sparks/list": {"id", "description", "god_type", "rarity"},
    "czn/piece-set-options/list": {"id", "name", "set2", "set4", "set6"},
    "czn/encounters/list": {"id", "name", "link_encounter_pack_id", "chaos_collection_info"},
    "czn/encounters/packs/list": {"id", "name", "pack_type"},
    "czn/rankings/list": {"season_id", "score"},
}
# Between seasons the leaderboard can be empty.
MAY_BE_EMPTY = {"czn/rankings/list"}
# Image files, by their path under ASSETS (the paths e7bot's own pages request).
ASSETS = "https://wiki.e7bot.top/api/assets/file/czn/data/"
GEAR_ICON = "item/{}.webp"
# The backgrounds the game draws behind an equipment icon, one per rarity.
RARITY_BACKGROUNDS = tuple(
    f"rarity/bg_equipment_rarity_{rarity}.webp" for rarity in ("common", "rare", "unique", "legend")
)
GEAR_SLOTS = {"EQUIP_WEAPON": "Weapon", "EQUIP_ARMOR": "Armor", "EQUIP_ACC": "Trinket"}
CHARACTER_DETAIL = "czn/characters/{}"
ENCOUNTER_DETAIL = "czn/encounters/{}"
SAVE = "czn/rankings/savedata/{}"
TEAM_SLOTS = [f"t{team}_c{slot}" for team in (1, 2) for slot in (1, 2, 3)]


def decode(body: Any, path: str) -> Any:
    """Decrypt and unwrap one API response body (parsed JSON) to its data."""
    if isinstance(body, dict) and body.get("encrypted"):
        try:
            raw = base64.b64decode(body["data"])
            decryptor = Cipher(algorithms.AES(KEY), modes.CBC(raw[:16])).decryptor()
            unpadder = padding.PKCS7(128).unpadder()
            plain = decryptor.update(raw[16:]) + decryptor.finalize()
            body = json.loads(unpadder.update(plain) + unpadder.finalize())
        except (KeyError, ValueError) as error:
            raise ParseError(f"{path}.decrypt", repr(error)) from error
    if isinstance(body, dict) and "code" in body:
        if body["code"] != 0 or "data" not in body:
            raise ParseError(f"{path}.code", f"code {body.get('code')!r}")
        return body["data"]
    return body


def threaded_get() -> Callable[[str], str]:
    """URL -> text for the workers: each thread gets its own paced HTTP session."""
    local = threading.local()

    def get(url: str) -> str:
        if not hasattr(local, "fetcher"):
            local.fetcher = Fetcher(delay=DELAY_SECONDS)
        return local.fetcher.get(url)

    return get


class Client:
    """Fetches and decodes e7bot API paths through ``get`` (URL -> response text), which must
    be safe to call from several threads."""

    def __init__(self, get: Callable[[str], str]):
        self._get = get

    def get(self, path: str) -> Any:
        text = self._get(f"{API}{path}?lang=en")
        try:
            body = json.loads(text)
        except ValueError as error:
            raise ParseError(f"{path}.json", repr(error)) from error
        return decode(body, path)


@dataclass
class E7botResult:
    requests: Counter = field(default_factory=Counter)  # ok, fetch_error, invalid -> count
    validation_failures: Counter = field(default_factory=Counter)  # field -> count
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.blocked
            or self.validation_failures
            or self.requests["fetch_error"]
            or self.requests["invalid"]
        )


class _Abort(Exception):
    """A required path failed; the run keeps the previous documents."""


def scrape_e7bot(conn: psycopg.Connection, client: Client, allow_shrink: bool) -> E7botResult:
    result = E7botResult()
    started = datetime.now(UTC)
    error = None
    try:
        documents = _fetch_documents(conn, client, allow_shrink, result)
        _replace_documents(conn, documents)
        _fetch_saves(conn, client, documents["czn/rankings/list"][0], result)
    except _Abort as failure:
        error = str(failure)
        log.error("e7bot run failed, keeping previous data", extra={"error": error})
    conn.execute(
        "INSERT INTO e7bot.run (started_at, finished_at, ok, error) VALUES (%s, %s, %s, %s)",
        (started, datetime.now(UTC), result.ok, error),
    )
    return result


def _fetch_documents(conn, client, allow_shrink, result) -> dict[str, tuple[Any, datetime]]:
    documents = _fetch_all(client, [*LISTS, *DIFFS], result)
    for path, required in LISTS.items():
        _validate_list(conn, path, documents[path][0], required, allow_shrink, result)
    for path in DIFFS:
        if not isinstance(documents[path][0], dict) or "timestamp" not in documents[path][0]:
            _invalid(result, f"{path}.timestamp", f"{path}: no timestamp")

    combatants = [
        c["id"] for c in documents["czn/characters/list"][0] if c["char_type"] == "CHAR_COMBATANT"
    ]
    encounters = [e["id"] for e in documents["czn/encounters/list"][0]]
    details = {
        CHARACTER_DETAIL: (combatants, "potential_nodes", "czn/snapshot/characters/diff"),
        ENCOUNTER_DETAIL: (encounters, "options", "czn/snapshot/encounters/diff"),
    }
    wanted = []
    for template, (ids, _, diff) in details.items():
        reusable = _reusable_details(conn, template, documents[diff][0]["timestamp"], diff)
        for item_id in ids:
            path = template.format(item_id)
            if path in reusable:
                documents[path] = reusable[path]
            else:
                wanted.append(path)
    log.info("fetching e7bot details", extra={"count": len(wanted)})
    documents |= _fetch_all(client, wanted, result)
    for template, (ids, key, _) in details.items():
        for item_id in ids:
            path = template.format(item_id)
            if not isinstance(documents[path][0], dict) or key not in documents[path][0]:
                _invalid(result, f"{template.format('{id}')}.{key}", path)
    return documents


def _reusable_details(conn, template: str, timestamp, diff: str) -> dict:
    """Stored detail documents still valid: e7bot has not imported new game data for that kind
    since they were fetched (its diff timestamp is unchanged), and they are younger than
    ``FULL_REFRESH``, which bounds how long a change the diff missed can go unnoticed."""
    stored = conn.execute(
        "SELECT body->'timestamp' FROM e7bot.document WHERE path = %s", (diff,)
    ).fetchone()
    if not stored or stored[0] != timestamp:
        return {}
    prefix = template.format("")
    return {
        path: (body, fetched_at)
        for path, body, fetched_at in conn.execute(
            "SELECT path, body, fetched_at FROM e7bot.document"
            " WHERE starts_with(path, %s) AND fetched_at > now() - %s",
            (prefix, FULL_REFRESH),
        )
    }


def _fetch_all(client: Client, paths: list[str], result: E7botResult) -> dict:
    """Fetch every path, ``WORKERS`` at a time; the first failure aborts the run."""
    documents = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(client.get, path): path for path in paths}
        try:
            for future in as_completed(futures):
                path = futures[future]
                documents[path] = (_required(future, path, result), datetime.now(UTC))
        except _Abort:
            pool.shutdown(cancel_futures=True)
            raise
    return documents


def _required(future, path: str, result: E7botResult) -> Any:
    try:
        data = future.result()
    except Blocked as failure:
        result.blocked = True
        raise _Abort(f"{path}: blocked: {failure}") from failure
    except FetchFailed as failure:
        result.requests["fetch_error"] += 1
        raise _Abort(f"{path}: {failure}") from failure
    except ParseError as failure:
        result.requests["invalid"] += 1
        result.validation_failures[failure.field.split(".")[-1]] += 1
        raise _Abort(str(failure)) from failure
    result.requests["ok"] += 1
    return data


def _validate_list(conn, path, items, required, allow_shrink, result) -> None:
    if not isinstance(items, list) or (not items and path not in MAY_BE_EMPTY):
        _invalid(result, f"{path}.items", f"{path}: not a non-empty list")
    for item in items:
        missing = required - set(item) if isinstance(item, dict) else required
        if missing:
            _invalid(result, f"{path}.{min(missing)}", f"{path}: item lacks {missing}")
    if allow_shrink:
        return
    row = conn.execute(
        "SELECT jsonb_array_length(body) FROM e7bot.document WHERE path = %s", (path,)
    ).fetchone()
    if row and items and len(items) < row[0] * (1 - SHRINK_LIMIT):
        _invalid(result, f"{path}.count", f"{path}: dropped from {row[0]} to {len(items)} items")


def _invalid(result: E7botResult, field_name: str, message: str):
    result.validation_failures[field_name] += 1
    raise _Abort(message)


def _replace_documents(conn: psycopg.Connection, documents: dict) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM e7bot.document")
        with conn.cursor().copy("COPY e7bot.document (path, body, fetched_at) FROM STDIN") as copy:
            for path, (body, fetched_at) in documents.items():
                copy.write_row((path, Jsonb(body), fetched_at))


def export_metrics(result: E7botResult, started: float) -> bool:
    """Send the run's metrics once (see ``czn_data.metrics``); False if the export failed."""

    def record(meter: Meter) -> None:
        now = time.time()

        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.e7bot.success", "1 when the run had no failures").set(int(result.ok))
        gauge("czn.e7bot.blocked", "1 when the site blocked the run").set(int(result.blocked))
        gauge("czn.e7bot.last_run.timestamp", "End of the run", "s").set(now)
        if result.ok:
            gauge("czn.e7bot.last_success.timestamp", "End of the last successful run", "s").set(
                now
            )
        gauge("czn.e7bot.duration", "Run duration", "s").set(now - started)
        requests = gauge("czn.e7bot.requests", "API requests by result")
        for outcome in ("ok", "fetch_error", "invalid"):
            requests.set(result.requests[outcome], {"result": outcome})
        failures = gauge("czn.e7bot.validation_failures", "Responses that no longer validate")
        for field_name, count in result.validation_failures.items():
            failures.set(count, {"field": field_name})

    return metrics.export("czn_data.e7bot", record)


def _fetch_saves(conn, client: Client, rankings: list[dict], result: E7botResult) -> None:
    """Fetch every leaderboard save not stored yet. A failed save is retried next run."""
    stored = {row[0] for row in conn.execute("SELECT save_id FROM e7bot.save")}
    wanted = {
        str(entry[f"{slot}_save_id"])
        for entry in rankings
        for slot in TEAM_SLOTS
        if entry.get(f"{slot}_save_id") and entry.get(f"{slot}_data_exists", True)
    }
    missing = sorted(wanted - stored)
    if missing:
        log.info("fetching leaderboard saves", extra={"count": len(missing)})
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(client.get, SAVE.format(save_id)): save_id for save_id in missing}
        for future in as_completed(futures):
            save_id = futures[future]
            path = SAVE.format(save_id)
            try:
                body = future.result()
            except Blocked as failure:
                result.blocked = True
                pool.shutdown(cancel_futures=True)
                raise _Abort(f"{path}: blocked: {failure}") from failure
            except (FetchFailed, ParseError) as failure:
                log.error("save fetch failed", extra={"path": path, "error": str(failure)})
                result.requests["fetch_error"] += 1
                continue
            if not isinstance(body, dict) or not isinstance(body.get("card_datas"), list):
                log.error("save has no card list", extra={"path": path})
                result.requests["invalid"] += 1
                continue
            result.requests["ok"] += 1
            conn.execute(
                "INSERT INTO e7bot.save (save_id, body, fetched_at) VALUES (%s, %s, %s)",
                (save_id, Jsonb(body), datetime.now(UTC)),
            )


def wearable(relic: dict) -> bool:
    """Whether a relic list item is equipment a player can wear. EQUIPMENT_TRANSFORM items
    (such as Eye of Gluttony) are worn like the rest; ``_test_`` items are developer benchmarks
    and ``_tut_`` items tutorial copies, which players never obtain."""
    return (
        relic["relic_category"] in ("EQUIPMENT", "EQUIPMENT_TRANSFORM")
        and relic["relic_type"] in GEAR_SLOTS
        and "_test_" not in relic["id"]
        and "_tut_" not in relic["id"]
    )


def fetch_gear_icons(
    conn: psycopg.Connection, get_bytes: Callable[[str], bytes], result: E7botResult
) -> None:
    """Store the icon of every wearable item in the stored relic list, and the rarity
    backgrounds, that is not stored yet, for gear prints (``czn_data.enrich``). A failed file
    counts as a ``fetch_error`` and is tried again next run.

    Stored files are never fetched again: e7bot names them after the game's own asset files, so
    new art arrives under a new name. If the game ever redraws an icon under its old name, the
    stored copy and its prints stay until the row is deleted.
    """
    row = conn.execute("SELECT body FROM e7bot.document WHERE path = 'czn/relics/list'").fetchone()
    relics = row[0] if row else []
    wanted = [
        *RARITY_BACKGROUNDS,
        *(GEAR_ICON.format(r["icon_name"]) for r in relics if r.get("icon_name") and wearable(r)),
    ]
    stored = {row[0] for row in conn.execute("SELECT path FROM e7bot.asset")}
    missing = sorted(set(wanted) - stored)
    if missing:
        log.info("fetching e7bot assets", extra={"count": len(missing)})
    for path in missing:
        try:
            image = get_bytes(ASSETS + path)
        except Blocked as failure:
            log.error("blocked", extra={"path": path, "error": str(failure)})
            result.blocked = True
            return
        except FetchFailed as failure:
            log.error("asset fetch failed", extra={"path": path, "error": str(failure)})
            result.requests["fetch_error"] += 1
            continue
        result.requests["ok"] += 1
        conn.execute(
            "INSERT INTO e7bot.asset (path, image, fetched_at) VALUES (%s, %s, %s)",
            (path, image, datetime.now(UTC)),
        )
