"""Fetch Game8's guides on the game's systems and modes into the ``game8`` schema, and split them
into sections for search.

Game8 covers characters, cards, and equipment too, but e7bot, Prydwen, and the official posts
already serve those; only the pages its game page lists under ``INCLUDED_HEADINGS`` (how
systems, modes, Chaos areas, events, and seasons work) are kept. Game8's sitemap dates each page,
so a page is fetched again only when that date moves (or the copy is older than
``scrape.MAX_AGE``). A page that fails keeps its stored copy; a game page or sitemap that fails,
or a selection that shrinks sharply, keeps every stored page. Either makes the run fail.
"""

import gzip
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

import psycopg
from bs4 import BeautifulSoup
from opentelemetry.metrics import Meter

from . import metrics
from .fetch import Blocked, FetchFailed, sitemap_date
from .parse import ParseError
from .scrape import MAX_AGE
from .sections import GuidePage, split_at_headings
from .store import SHRINK_LIMIT
from .text import text_of

log = logging.getLogger(__name__)

GAME_PAGE = "https://game8.co/games/Chaos-Zero-Nightmare"
ARCHIVE_PREFIX = f"{GAME_PAGE}/archives/"
# Game8 keeps one sitemap per game; 1462 is this game's id on its game page.
SITEMAP = "https://game8.co/sitemaps/game_1462.xml.gz"
# Game8 publishes no crawl delay for ordinary crawlers; one request per second keeps load small.
DELAY_SECONDS = 1.0
# Headings on the game page whose links are kept, as Game8 writes them (2026-11).
INCLUDED_HEADINGS = frozenset(
    {
        "Chaos Zero Nightmare Chaos",
        "Chaos Game Modes",
        "All Chaos Areas",
        "All Seasonal Chaos Areas",
        "Chaos Zero Nightmare Simulation",
        "Simulation Guides",
        "Challenges Guides",
        "Chaos Zero Nightmare Ark City",
        "All Ark City Guides",
        "Chaos Zero Nightmare Tips and Tricks",
        "All Tips and Tricks",
        "Chaos Zero Nightmare Glossary",
        "All Galactic Disaster Seasons",
        "List of All Seasons",
        "Events and Game Info",
        "Chaos Zero Nightmare Missions",
        "Promotion Missions",
    }
)
# System and mode guides also sit among other links (recommended guides, the footer), so link
# text keeps them anywhere on the game page ("Corruption Mode Explained", "Season 5: ... Guide",
# "Hand Size: How Many Cards Can You Hold?"), except guides to characters, builds, and the like.
SYSTEM_GUIDE = re.compile(r"(Explained|Guide|Mechanics)$|^How to |^What is |^Hand Size")
NOT_SYSTEM_GUIDE = re.compile(
    r"Combatant Guide|Reroll|Tier List|Team Composition|Build|Banner|Character Cards|Partner"
    r"|Sharing|Codes|Download|Patch Error|Pre-Register",
    re.IGNORECASE,
)
CONTENT = ".archive-style-wrapper"
_HEADING_LEVELS = {"h2": 0, "h3": 1, "h4": 2}


@dataclass
class Game8Result:
    requests: Counter = field(default_factory=Counter)  # ok, fetch_error, invalid -> count
    validation_failures: Counter = field(default_factory=Counter)  # field -> count
    pages: Counter = field(default_factory=Counter)  # fetched, unchanged, removed -> count
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.blocked
            or self.validation_failures
            or self.requests["fetch_error"]
            or self.requests["invalid"]
        )


def scrape_game8(
    conn: psycopg.Connection,
    get: Callable[[str], str],
    get_bytes: Callable[[str], bytes],
    allow_shrink: bool,
) -> Game8Result:
    result = Game8Result()
    started = datetime.now(UTC)
    error = None
    try:
        selected = select_pages(get(GAME_PAGE))
        result.requests["ok"] += 1
        listed = sitemap_dates(get_bytes(SITEMAP))
        result.requests["ok"] += 1
        # Game8 leaves its premium (opinion) articles out of the sitemap; keeping only listed
        # pages drops them and gives every kept page a date. A new page waits for its listing.
        selected &= listed.keys()
        _store_pages(conn, get, selected, listed, allow_shrink, result)
    except Blocked as failure:
        result.blocked = True
        error = f"blocked: {failure}"
        log.error("blocked, stopping", extra={"error": str(failure)})
    except (FetchFailed, ParseError) as failure:
        if isinstance(failure, ParseError):
            result.requests["invalid"] += 1
            result.validation_failures[failure.field] += 1
        else:
            result.requests["fetch_error"] += 1
        error = str(failure)
        log.error("listing failed, keeping stored pages", extra={"error": error})
    if error is None and not result.ok:
        error = "some pages failed; see the logs"
    conn.execute(
        "INSERT INTO game8.run (started_at, finished_at, ok, error) VALUES (%s, %s, %s, %s)",
        (started, datetime.now(UTC), result.ok, error),
    )
    return result


def select_pages(html: str) -> set[str]:
    """Pages the game page links under ``INCLUDED_HEADINGS``, plus system and mode guides
    anywhere on it (``SYSTEM_GUIDE``)."""
    soup = BeautifulSoup(html, "lxml")
    heading = None
    selected = set()
    for el in soup.find_all(["h2", "h3", "a"]):
        if el.name != "a":
            heading = text_of(el)
            continue
        url = urljoin(GAME_PAGE, el.get("href", "")).split("#")[0].split("?")[0]
        if not url.startswith(ARCHIVE_PREFIX):
            continue
        text = text_of(el)
        guide = SYSTEM_GUIDE.search(text) and not NOT_SYSTEM_GUIDE.search(text)
        if heading in INCLUDED_HEADINGS or guide:
            selected.add(url)
    if not selected:
        raise ParseError("selection.pages", "game page lists no pages under the kept headings")
    return selected


def sitemap_dates(body: bytes) -> dict[str, datetime | None]:
    """Each game page in the sitemap with its date (None when it gives none or none parses)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    pages = {}
    for entry in BeautifulSoup(body, "xml").find_all("url"):
        loc, lastmod = entry.find("loc"), entry.find("lastmod")
        url = loc.get_text(strip=True) if loc else ""
        if url.startswith(ARCHIVE_PREFIX):
            pages[url] = sitemap_date(lastmod.get_text(strip=True)) if lastmod else None
    if not pages:
        raise ParseError("sitemap.pages", "sitemap lists no game pages")
    return pages


def _store_pages(conn, get, selected: set[str], listed: dict, allow_shrink: bool, result) -> None:
    stored = {
        url: (lastmod, fetched_at)
        for url, lastmod, fetched_at in conn.execute(
            "SELECT url, lastmod, fetched_at FROM game8.page"
        )
    }
    if not allow_shrink and stored and len(selected) < len(stored) * (1 - SHRINK_LIMIT):
        raise ParseError(
            "selection.count", f"selection dropped from {len(stored)} to {len(selected)} pages"
        )
    now = datetime.now(UTC)
    for url in sorted(selected):
        lastmod = listed.get(url)
        if url in stored and lastmod is not None:
            stored_lastmod, fetched_at = stored[url]
            if stored_lastmod == lastmod and fetched_at >= now - MAX_AGE:
                result.pages["unchanged"] += 1
                continue
        try:
            html = get(url)
        except FetchFailed as failure:
            # The stored copy and its date stay, so the next run fetches the page again.
            result.requests["fetch_error"] += 1
            log.error("page fetch failed", extra={"url": url, "error": str(failure)})
            continue
        if BeautifulSoup(html, "lxml").select_one(CONTENT) is None:
            result.requests["invalid"] += 1
            result.validation_failures["page.content"] += 1
            log.error("page has no guide content", extra={"url": url})
            continue
        result.requests["ok"] += 1
        result.pages["fetched"] += 1
        conn.execute(
            """
            INSERT INTO game8.page (url, html, lastmod, fetched_at) VALUES (%s, %s, %s, now())
            ON CONFLICT (url) DO UPDATE SET html = excluded.html, lastmod = excluded.lastmod,
                fetched_at = excluded.fetched_at
            """,
            (url, html, lastmod),
        )
    removed = set(stored) - selected
    if removed:
        conn.execute("DELETE FROM game8.page WHERE url = ANY (%s)", (list(removed),))
        result.pages["removed"] += len(removed)


def split_page(html: str) -> GuidePage:
    """Split a page at its headings (h2 to h4; see ``czn_data.sections``). Tables become one
    line per row, and the table of contents is dropped."""
    soup = BeautifulSoup(html, "lxml")
    titled = soup.select_one("h1.p-archiveHeader__title") or soup.find("h1")
    title = text_of(titled) if titled else ""
    root = soup.select_one(CONTENT)
    if root is None:
        raise ParseError("page.content", "page has no guide content")
    for gone in root(["script", "style", "noscript"]) + root.select(".a-outline"):
        gone.decompose()
    return split_at_headings(root, title, _HEADING_LEVELS)


def export_metrics(result: Game8Result, started: float) -> bool:
    """Send the run's metrics once (see ``czn_data.metrics``); False if the export failed."""

    def record(meter: Meter) -> None:
        now = time.time()

        def gauge(name: str, description: str, unit: str = ""):
            return meter.create_gauge(name, unit=unit, description=description)

        gauge("czn.game8.success", "1 when the run had no failures").set(int(result.ok))
        gauge("czn.game8.blocked", "1 when the site blocked the run").set(int(result.blocked))
        gauge("czn.game8.last_run.timestamp", "End of the run", "s").set(now)
        if result.ok:
            gauge("czn.game8.last_success.timestamp", "End of the last successful run", "s").set(
                now
            )
        gauge("czn.game8.duration", "Run duration", "s").set(now - started)
        requests = gauge("czn.game8.requests", "Requests by result")
        for outcome in ("ok", "fetch_error", "invalid"):
            requests.set(result.requests[outcome], {"result": outcome})
        pages = gauge("czn.game8.pages", "Pages by what the run did with them")
        for outcome in ("fetched", "unchanged", "removed"):
            pages.set(result.pages[outcome], {"result": outcome})
        failures = gauge("czn.game8.validation_failures", "Responses that no longer validate")
        for field_name, count in result.validation_failures.items():
            failures.set(count, {"field": field_name})

    return metrics.export("czn_data.game8", record)
