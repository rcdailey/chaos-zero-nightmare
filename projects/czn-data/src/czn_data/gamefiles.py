"""Download tables from the game's own data files into the ``gamefiles`` schema.

The game downloads its data from a public CDN as one manifest (``manifest.ssra``) and large
chunk files that hold every file back to back. This module reads the manifest, then
range-requests only the bytes of the files in ``FILES``, decompresses and decrypts each, and
stores its rows. Formats are described in ``docs/game-data-investigation.md`` (repository
root); ``cznrip/ChaosZeroNightmare-AssetRipper`` is the reference implementation.

``BUILD`` names one build folder on the CDN. The game learns the current folder from a version
service whose address is unknown, so ``BUILD`` is updated by hand after a patch; until then the
old build keeps being served while the CDN keeps it. A 403 for the manifest means the CDN
dropped the folder. A file is downloaded again only when its manifest record (location, sizes,
and checksums) or ``BUILD`` changed; the stored rows of every other file stay. A run stores
nothing unless every changed file downloads and decodes, so the stored files always come from
one manifest.
"""

import logging
import struct
import time
from collections import Counter
from collections.abc import Callable
from compression import zstd
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from opentelemetry.metrics import Meter
from psycopg.types.json import Jsonb

from . import metrics
from .fetch import Blocked, FetchFailed
from .parse import ParseError
from .store import SHRINK_LIMIT

log = logging.getLogger(__name__)

# The build folder the game was sent on 2026-11-11 (app 1.0.853, data build 166).
BUILD = "1.0.85302/5P340UKEBKQV/pack-166-a248701680af"
BASE = f"https://czn-live-down.game.playstove.com/patch/{BUILD}/main/"
# The CDN answers 403 to any other User-Agent.
HEADERS = {"User-Agent": "Yuna WebSocket/0.1 Supercreative inc."}
DELAY_SECONDS = 1.0
# Files stored, by manifest name: every English string, and the in-game Help menus (the general
# menu and the one opened in Chaos) with their pages.
TEXT = "text/en/text.db"
HELP_MENUS = ("db/tutorial_popup@help_menu.db", "db/tutorial_popup(battle)@help_menu.db")
# Each Help menu as players see it: the lobby's Help, and the one opened during a Chaos run.
MENU_NAMES = dict(zip(HELP_MENUS, ("Help", "Chaos Help"), strict=True))
HELP_PAGES = (
    "db/tutorial_popup@tutorial_popup_page.db",
    "db/tutorial_popup(battle)@tutorial_popup_page.db",
)
FILES = (TEXT, *HELP_MENUS, *HELP_PAGES)

# Data tables are XORed with this 256-byte key, starting at an unknown rotation; the rotation
# is the one that turns the first bytes into the container magic.
KEY = bytes.fromhex(
    "91AE4ED4644F585162EC1BD5EF24ADDBAF838242AEF51E97804B134FFD8CE5BB"
    "4F6E3E6451147CDF56C318E5E964C999C0D95CC860822E6B418BE465D79A036D"
    "BF67AB3DA72AB1023A4561F444E5CE858D23EA10FEB4899151AD7E43FF3E2419"
    "A97B4DD3AF4EF5C829E5AF4ACE9436F6B6B6382E9DFD26642099011A4899089C"
    "9D4B9F80BBB00A4CC73255CE1F78646E91C9C12313F5D840DC51457010D37D19"
    "615BB69888B42B19E749F993C00337E9332F89B320C173A5653848788798A771"
    "739E72DBC84C7946597149BDDAE4E3BD1A17856C85A555CFA24F6352D005933B"
    "50042BE0BA4C708DE8EBB52059B2059C9BFE90D8923DF74B43911BBC00BB6BFA"
)
MAGIC = b"PLPcK"
_HEADER = struct.Struct("<4sIIIIIQQQQQ")
_CHUNK = struct.Struct("<IHHQQQ")
_FILE = struct.Struct("<IIQIIIIBBHI")
_TOMBSTONE = 1
# Chunk group of hotfixes, files replaced after the build shipped.
_HOTFIX = 0xFFFF


@dataclass(frozen=True)
class Entry:
    """Where one file sits: ``offset`` within its chunk group, its stored and real sizes, and
    ``record``, its raw manifest record (which changes with the file)."""

    group: str
    offset: int
    stored_size: int
    compressed: bool
    record: bytes


@dataclass
class Manifest:
    files: dict[str, Entry]
    # Group name -> its chunks as (chunk file name, start in the group, size), in order.
    chunks: dict[str, list[tuple[str, int, int]]]


@dataclass
class GameFilesResult:
    requests: Counter = field(default_factory=Counter)  # ok, fetch_error, invalid -> count
    validation_failures: Counter = field(default_factory=Counter)  # field -> count
    files: Counter = field(default_factory=Counter)  # fetched, unchanged -> count
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.blocked
            or self.validation_failures
            or self.requests["fetch_error"]
            or self.requests["invalid"]
        )


def scrape_gamefiles(
    conn: psycopg.Connection,
    get_bytes: Callable[..., bytes],
    allow_shrink: bool,
) -> GameFilesResult:
    """``get_bytes(url, start=None, end=None)`` returns a body, or its bytes start to end."""
    result = GameFilesResult()
    started = datetime.now(UTC)
    error = None
    try:
        manifest = read_manifest(get_bytes(f"{BASE}manifest.ssra.zst"))
        result.requests["ok"] += 1
        _store_files(conn, get_bytes, manifest, allow_shrink, result)
    except Blocked as failure:
        result.blocked = True
        error = f"refused, the CDN may have dropped build {BUILD}: {failure}"
        log.error("download refused", extra={"build": BUILD, "error": str(failure)})
    except (FetchFailed, ParseError) as failure:
        if isinstance(failure, ParseError):
            result.requests["invalid"] += 1
            result.validation_failures[failure.field] += 1
        else:
            result.requests["fetch_error"] += 1
        error = str(failure)
        log.error("download failed, keeping stored files", extra={"error": error})
    conn.execute(
        "INSERT INTO gamefiles.run (started_at, finished_at, build, ok, error)"
        " VALUES (%s, %s, %s, %s, %s)",
        (started, datetime.now(UTC), BUILD, result.ok, error),
    )
    return result


def _store_files(conn, get_bytes, manifest: Manifest, allow_shrink: bool, result) -> None:
    stored = {
        name: (build, record, count)
        for name, build, record, count in conn.execute(
            "SELECT name, build, record, jsonb_array_length(rows) FROM gamefiles.file"
        )
    }
    downloads = {}
    for name in FILES:
        entry = manifest.files.get(name)
        if entry is None:
            raise ParseError("manifest.files", f"manifest lists no {name}")
        if stored.get(name, (None, None))[:2] == (BUILD, entry.record):
            result.files["unchanged"] += 1
            continue
        rows = read_table(_read_file(get_bytes, manifest, entry, result))
        count = stored.get(name, (None, None, 0))[2]
        if not allow_shrink and len(rows) < count * (1 - SHRINK_LIMIT):
            raise ParseError("table.count", f"{name} dropped from {count} to {len(rows)} rows")
        downloads[name] = (entry.record, rows)
    with conn.transaction():
        for name, (record, rows) in downloads.items():
            conn.execute(
                """
                INSERT INTO gamefiles.file (name, build, record, rows, fetched_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (name) DO UPDATE SET build = excluded.build,
                    record = excluded.record, rows = excluded.rows, fetched_at = now()
                """,
                (name, BUILD, record, Jsonb(rows)),
            )
    result.files["fetched"] += len(downloads)


def _read_file(get_bytes, manifest: Manifest, entry: Entry, result) -> bytes:
    """The file's bytes, from each chunk it overlaps."""
    data = bytearray()
    end = entry.offset + entry.stored_size
    for chunk, start, size in manifest.chunks.get(entry.group, []):
        lo, hi = max(entry.offset, start), min(end, start + size)
        if lo < hi:
            data += get_bytes(f"{BASE}chunks/{chunk}", start=lo - start, end=hi - start)
            result.requests["ok"] += 1
    if len(data) != entry.stored_size:
        raise ParseError("file.size", f"read {len(data)} of {entry.stored_size} bytes")
    return _unzstd(bytes(data)) if entry.compressed else bytes(data)


def _unzstd(data: bytes) -> bytes:
    # Some frames start with an older magic byte (0x25 to 0x27) in place of zstd's 0x28.
    if len(data) > 4 and 0x25 <= data[0] <= 0x27 and data[1:4] == b"\xb5\x2f\xfd":
        data = b"\x28" + data[1:]
    try:
        return zstd.decompress(data)
    except zstd.ZstdError as error:
        raise ParseError("file.zstd", str(error)) from error


def read_manifest(body: bytes) -> Manifest:
    """The manifest's live files and each chunk group's chunks."""
    data = _unzstd(body)
    try:
        return _parse_manifest(data)
    except (struct.error, ValueError, IndexError) as error:
        raise ParseError("manifest.format", f"manifest does not parse: {error}") from error


def _parse_manifest(data: bytes) -> Manifest:
    _, _, _, chunk_count, file_count, _, strings, strings_size, chunk_table, file_table, _ = (
        _HEADER.unpack_from(data)
    )
    # Group names follow the string table: count at +4, 24-byte records from +16 (index at +0,
    # name offset at +4), then their names.
    groups = {}
    at = strings + strings_size
    if data[at : at + 4] == b"GRPS":
        (count,) = struct.unpack_from("<I", data, at + 4)
        names = at + 16 + 24 * count
        for i in range(count):
            index, name = struct.unpack_from("<HxxI", data, at + 16 + 24 * i)
            groups[index] = _string(data, names + name)
    by_group: dict[int, list[tuple[int, int]]] = {}
    for i in range(chunk_count):
        chunk_id, group, _, size, _, _ = _CHUNK.unpack_from(data, chunk_table + _CHUNK.size * i)
        by_group.setdefault(group, []).append((chunk_id, size))
    groups[_HOTFIX] = "hotfix"
    chunks = {}
    for group, members in by_group.items():
        if group not in groups:
            continue
        start, placed = 0, []
        for chunk_id, size in sorted(members):
            name = (
                f"hotfix_{chunk_id:04d}.ssrc"
                if group == _HOTFIX
                else f"{groups[group]}_b{chunk_id:02d}_0.ssrc"
            )
            placed.append((name, start, size))
            start += size
        chunks[groups[group]] = placed
    files = {}
    for i in range(file_count):
        at = file_table + _FILE.size * i
        _, _, offset, stored, _, _, name, compressed, _, group, flags = _FILE.unpack_from(data, at)
        if flags & _TOMBSTONE:
            continue
        files[_string(data, strings + name).lstrip("/")] = Entry(
            groups.get(group, ""), offset, stored, bool(compressed), data[at : at + _FILE.size]
        )
    return Manifest(files, chunks)


def _string(data: bytes, at: int) -> str:
    return data[at : data.index(b"\0", at)].decode()


def read_table(data: bytes) -> list[dict[str, str]]:
    """A data table's rows, each as column -> value (every value is text)."""
    rotation = next((r for r in range(len(KEY)) if _xor(data[: len(MAGIC)], r) == MAGIC), None)
    if rotation is None:
        raise ParseError("table.key", "no key rotation decrypts the table")
    try:
        return _rows(_entries(_xor(data, rotation)))
    except (struct.error, KeyError, ValueError, IndexError) as error:
        raise ParseError("table.format", f"table does not parse: {error!r}") from error


def _xor(data: bytes, rotation: int) -> bytes:
    key = (KEY[rotation:] + KEY[:rotation]) * (len(data) // len(KEY) + 1)
    return (int.from_bytes(data) ^ int.from_bytes(key[: len(data)])).to_bytes(len(data))


def _pointer(data: bytes, at: int) -> int:
    """A 40-bit pointer: its high byte, then the low 32 bits."""
    return (data[at] << 32) | struct.unpack_from("<I", data, at + 1)[0]


def _entries(data: bytes) -> dict[str, bytes]:
    """A table container's named entries: a hash table of chains, each entry holding its name
    length at +5, data size at +6, the next entry at +10, then its name and data from +15."""
    (buckets,) = struct.unpack_from("<I", data, 21)
    table = _pointer(data, 25) + 5
    entries = {}
    for bucket in range(buckets):
        at = _pointer(data, table + 5 * bucket)
        while at:
            name_size = data[at + 5]
            (size,) = struct.unpack_from("<I", data, at + 6)
            name = at + 15
            entries[data[name : name + name_size].decode()] = data[
                name + name_size : name + name_size + size
            ]
            at = _pointer(data, at + 10)
    return entries


def _rows(entries: dict[str, bytes]) -> list[dict[str, str]]:
    """``\\trows`` and ``\\tcols`` count rows and columns, ``\\t<n>`` names column n, and
    ``\\t\\t<n>`` names the entry holding row n's values, each ended by a NUL."""
    (count,) = struct.unpack_from("<I", entries["\trows"])
    (width,) = struct.unpack_from("<I", entries["\tcols"])
    columns = [entries[f"\t{c}"].rstrip(b"\0").decode() for c in range(width)]
    rows = []
    for n in range(count):
        values = entries[entries[f"\t\t{n}"].rstrip(b"\0").decode()].split(b"\0")
        rows.append(dict(zip(columns, (v.decode() for v in values), strict=False)))
    return rows


@dataclass
class HelpTopic:
    """One topic of the in-game Help: ``menu`` is where the game shows it ("Help", "Chaos
    Help", or "Tutorial"); ``text`` is its pages in order, each under its own title when the
    topic has several; text keeps the game's markup."""

    id: str
    menu: str
    title: str
    text: str


def help_topics(tables: dict[str, list[dict]]) -> list[HelpTopic]:
    """Help topics from the stored tables (file name -> rows). Each menu entry links a group of
    pages; pages no menu links (shown only during the tutorial) become one topic each."""
    strings = {row["id"]: row.get("text", "") for row in tables.get(TEXT, [])}
    groups: dict[str, list[dict]] = {}
    for name in HELP_PAGES:
        for page in tables.get(name, []):
            groups.setdefault(page["group"], []).append(page)

    def text_of(pages: list[dict]) -> str:
        pages = sorted(pages, key=lambda p: int(p.get("sort") or 0))
        bodies = [
            strings.get(p.get("out_game_desc") or p.get("in_game_desc", ""), "") for p in pages
        ]
        if len(pages) == 1:
            return bodies[0]
        return "\n\n".join(
            f"{strings.get(p['title'], '')}\n{b}" for p, b in zip(pages, bodies, strict=True)
        )

    topics, linked = [], set()
    for name in HELP_MENUS:
        for entry in tables.get(name, []):
            group = entry["link_tutorial_popup_page_group"]
            linked.add(group)
            if groups.get(group):
                title = strings.get(entry["name"], "")
                topics.append(
                    HelpTopic(entry["id"], MENU_NAMES[name], title, text_of(groups[group]))
                )
    for group, pages in groups.items():
        if group not in linked:
            topics.extend(
                HelpTopic(p["id"], "Tutorial", strings.get(p["title"], ""), text_of([p]))
                for p in pages
            )
    return [t for t in topics if t.title and t.text]


def export_metrics(result: GameFilesResult, started: float) -> bool:
    """Send the run's metrics once (see ``czn_data.metrics``); False if the export failed."""

    def record(meter: Meter) -> None:
        now = time.time()

        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.gamefiles.success", "1 when the run had no failures").set(int(result.ok))
        gauge("czn.gamefiles.blocked", "1 when the CDN refused the build").set(int(result.blocked))
        gauge("czn.gamefiles.last_run.timestamp", "End of the run", "s").set(now)
        if result.ok:
            gauge(
                "czn.gamefiles.last_success.timestamp", "End of the last successful run", "s"
            ).set(now)
        gauge("czn.gamefiles.duration", "Run duration", "s").set(now - started)
        requests = gauge("czn.gamefiles.requests", "Requests by result")
        for outcome in ("ok", "fetch_error", "invalid"):
            requests.set(result.requests[outcome], {"result": outcome})
        files = gauge("czn.gamefiles.files", "Files by what the run did with them")
        for outcome in ("fetched", "unchanged"):
            files.set(result.files[outcome], {"result": outcome})
        failures = gauge("czn.gamefiles.validation_failures", "Responses that no longer validate")
        for field_name, count in result.validation_failures.items():
            failures.set(count, {"field": field_name})

    return metrics.export("czn_data.gamefiles", record)
