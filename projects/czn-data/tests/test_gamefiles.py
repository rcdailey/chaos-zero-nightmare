"""Contract: a game files run stores the rows of every file in ``FILES``, downloads again only
files whose manifest record changed, and never replaces stored files because of a failure.

The game's CDN is replaced by an in-memory one serving a manifest and chunk files encoded the
way the game encodes them; the database is real Postgres.
"""

import struct
from compression import zstd

import pytest

from czn_data import gamefiles
from czn_data.cli import main
from czn_data.fetch import Blocked, FetchFailed
from czn_data.gamefiles import BASE, FILES, KEY, MAGIC, scrape_gamefiles

CHUNK_SIZE = 64  # small, so a file spans several chunks


def table(rows: list[dict[str, str]]) -> bytes:
    """A data table as the game stores it: a hash-table container of named entries, XORed
    with the key at rotation 7, then zstd-compressed with the old magic byte."""
    columns = list(rows[0]) if rows else ["id"]
    named = {"\trows": struct.pack("<I", len(rows)), "\tcols": struct.pack("<I", len(columns))}
    named |= {f"\t{n}": c.encode() + b"\0" for n, c in enumerate(columns)}
    for n, row in enumerate(rows):
        key = f"row{n}"
        named[f"\t\t{n}"] = key.encode() + b"\0"
        named[key] = b"".join(row[c].encode() + b"\0" for c in columns)
    header = bytearray(38)
    header[:5], header[5] = MAGIC, 0x26
    table_at = len(header)
    struct.pack_into("<I", header, 21, 1)  # one bucket holds every entry
    header[25], header[26:30] = 0, struct.pack("<I", table_at)
    body = bytearray(header + bytes(5 + 5))  # root record, then the bucket's pointer
    entries = list(named.items())
    first = len(body)
    for i, (name, data) in enumerate(entries):
        at = len(body)
        next_at = at + 15 + len(name.encode()) + len(data) if i + 1 < len(entries) else 0
        body += struct.pack("<IBBI", 0, 0, len(name.encode()), len(data))
        body += bytes([next_at >> 32]) + struct.pack("<I", next_at & 0xFFFFFFFF)
        body += name.encode() + data
    body[table_at + 5] = 0
    body[table_at + 6 : table_at + 10] = struct.pack("<I", first)
    key = (KEY[7:] + KEY[:7]) * (len(body) // len(KEY) + 1)
    packed = zstd.compress(bytes(b ^ k for b, k in zip(body, key, strict=False)))
    return b"\x25" + packed[1:]


def cdn(files: dict[str, bytes]):
    """The manifest and chunks of one group holding ``files`` back to back. ``get.ranges``
    records each chunk request; ``get.failing`` names files whose requests fail."""
    names, blob, records = b"", b"", []
    for name, data in files.items():
        name_at = len(names)
        names += f"/{name}".encode() + b"\0"
        records.append((len(blob), len(data), name_at))
        blob += data
    group_names = b"main\0"
    strings = 64
    grps = strings + len(names)
    chunk_table = grps + 16 + 24 + len(group_names)
    chunk_count = -(-len(blob) // CHUNK_SIZE)
    file_table = chunk_table + 32 * chunk_count
    manifest = bytearray(struct.pack("<4sIIIIIQQQQQ", b"SSRA", 1, 0, chunk_count, len(files), 0,
        strings, len(names), chunk_table, file_table, 0))  # fmt: skip
    manifest += names + b"GRPS" + struct.pack("<III", 1, len(group_names), 0)
    manifest += struct.pack("<HxxI16x", 0, 0) + group_names
    for n in range(chunk_count):
        size = min(CHUNK_SIZE, len(blob) - n * CHUNK_SIZE)
        manifest += struct.pack("<IHHQQQ", n, 0, 0, size, size, 0)
    for offset, size, name_at in records:
        manifest += struct.pack("<IIQIIIIBBHI", 0, 0, offset, size, size, 0, name_at, 1, 0, 0, 0)
    served = {f"{BASE}manifest.ssra.zst": zstd.compress(bytes(manifest))}
    for n in range(chunk_count):
        served[f"{BASE}chunks/main_b{n:02d}_0.ssrc"] = blob[n * CHUNK_SIZE : (n + 1) * CHUNK_SIZE]
    spans = {
        name: (offset, offset + size)
        for name, (offset, size, _) in zip(files, records, strict=True)
    }

    def get(url: str, start: int | None = None, end: int | None = None) -> bytes:
        if url not in served:
            raise Blocked(f"{url}: HTTP 403")
        if start is None:
            return served[url]
        chunk = int(url.rsplit("_b", 1)[1].split("_")[0])
        at = chunk * CHUNK_SIZE + start
        for name in get.failing:
            if spans[name][0] <= at < spans[name][1]:
                raise FetchFailed(f"{url}: HTTP 500")
        get.ranges.append(at)
        return served[url][start:end]

    get.ranges, get.failing = [], set()
    get.spans = spans
    return get


def game(text: str = "Hello", pages: int = 1) -> dict[str, bytes]:
    """Every file in FILES; the text table's one string is ``text``, and one pages table has
    ``pages`` rows."""
    files = {name: table([{"id": "x", "text": "y"}]) for name in FILES if name != gamefiles.TEXT}
    files[gamefiles.HELP_PAGES[0]] = table([{"id": f"p{n}", "group": "g"} for n in range(pages)])
    # Last, so a change to it moves no other file.
    files[gamefiles.TEXT] = table([{"id": "greeting", "text": text}])
    return files


def stored(conn) -> dict[str, list]:
    return dict(conn.execute("SELECT name, rows FROM gamefiles.file").fetchall())


def downloaded(get, name: str) -> bool:
    start, end = get.spans[name]
    return any(start <= at < end for at in get.ranges)


def last_run_ok(conn) -> bool:
    return conn.execute("SELECT ok FROM gamefiles.run ORDER BY started_at DESC").fetchone()[0]


def test_clean_run_stores_every_file_as_rows(conn):
    result = scrape_gamefiles(conn, cdn(game()), allow_shrink=False)

    assert result.ok
    files = stored(conn)
    assert set(files) == set(FILES)
    assert files[gamefiles.TEXT] == [{"id": "greeting", "text": "Hello"}]
    assert last_run_ok(conn)


def test_only_files_whose_record_changed_are_downloaded_again(conn):
    scrape_gamefiles(conn, cdn(game("Hello")), allow_shrink=False)
    unchanged = cdn(game("Hello"))
    assert scrape_gamefiles(conn, unchanged, allow_shrink=False).ok
    assert unchanged.ranges == []

    changed = cdn(game("Hello there"))
    assert scrape_gamefiles(conn, changed, allow_shrink=False).ok

    assert stored(conn)[gamefiles.TEXT] == [{"id": "greeting", "text": "Hello there"}]
    assert [name for name in FILES if downloaded(changed, name)] == [gamefiles.TEXT]


def test_a_new_build_downloads_every_file_again(conn, monkeypatch):
    scrape_gamefiles(conn, cdn(game()), allow_shrink=False)
    monkeypatch.setattr(gamefiles, "BUILD", "next-build")
    get = cdn(game())

    assert scrape_gamefiles(conn, get, allow_shrink=False).ok
    assert all(downloaded(get, name) for name in FILES)


def test_a_failed_file_stores_nothing_and_fails_the_run(conn, monkeypatch):
    scrape_gamefiles(conn, cdn(game("old")), allow_shrink=False)
    monkeypatch.setattr(gamefiles, "BUILD", "next-build")  # every file is due
    get = cdn(game("new text"))
    get.failing = {gamefiles.HELP_MENUS[1]}

    result = scrape_gamefiles(conn, get, allow_shrink=False)

    assert not result.ok
    assert stored(conn)[gamefiles.TEXT] == [{"id": "greeting", "text": "old"}]
    assert not last_run_ok(conn)


def test_a_dropped_build_or_missing_file_keeps_stored_files(conn):
    scrape_gamefiles(conn, cdn(game()), allow_shrink=False)

    def retired(url: str, **_) -> bytes:
        raise Blocked(f"{url}: HTTP 403")

    refused = scrape_gamefiles(conn, retired, allow_shrink=False)
    assert refused.blocked and not refused.ok

    partial = game()
    del partial[gamefiles.HELP_MENUS[0]]
    missing = scrape_gamefiles(conn, cdn(partial), allow_shrink=False)
    assert missing.validation_failures == {"manifest.files": 1}

    assert set(stored(conn)) == set(FILES)


def test_a_table_that_shrinks_sharply_is_kept_unless_allowed(conn):
    scrape_gamefiles(conn, cdn(game(pages=10)), allow_shrink=False)
    shrunk = cdn(game(pages=2))

    assert not scrape_gamefiles(conn, shrunk, allow_shrink=False).ok
    assert len(stored(conn)[gamefiles.HELP_PAGES[0]]) == 10

    assert scrape_gamefiles(conn, cdn(game(pages=2)), allow_shrink=True).ok
    assert len(stored(conn)[gamefiles.HELP_PAGES[0]]) == 2


def test_scrape_gamefiles_command_exits_nonzero_when_the_cdn_fails(conn, database_url, monkeypatch):
    class Unreachable:
        def __init__(self, **_):
            pass

        def get_bytes(self, url: str, start=None, end=None) -> bytes:
            raise FetchFailed(f"{url}: HTTP 503")

    monkeypatch.setattr("czn_data.fetch.Fetcher", Unreachable)
    monkeypatch.setenv("DATABASE_URL", database_url)
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.argv", ["czn-data", "scrape-gamefiles"])

    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 1
