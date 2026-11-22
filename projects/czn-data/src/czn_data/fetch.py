"""HTTP access to prydwen.gg.

Prydwen sits behind Cloudflare bot protection, which challenges ordinary HTTP clients.
``curl_cffi`` sends requests with Chrome's TLS and HTTP/2 fingerprint, which currently passes
without running JavaScript. If Cloudflare starts challenging it anyway, ``Blocked`` is raised;
retrying cannot fix that, so the run stops and alerts.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from bs4 import BeautifulSoup
from curl_cffi import requests

log = logging.getLogger(__name__)

SITE = "https://www.prydwen.gg"
# robots.txt asks every crawler for this delay between requests.
CRAWL_DELAY_SECONDS = 10.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class Blocked(Exception):
    """Cloudflare served a bot challenge instead of the page."""


class FetchFailed(Exception):
    """The page could not be fetched after retries, or does not exist."""


class Fetcher:
    """``headers`` go with every request, over the browser's own (a server that wants its
    client's User-Agent)."""

    def __init__(
        self, delay: float = CRAWL_DELAY_SECONDS, retries: int = 3, headers: dict | None = None
    ):
        self.delay = delay
        self.retries = retries
        self._session = requests.Session(impersonate="chrome", headers=headers)
        self._last_request = 0.0

    def get(self, url: str) -> str:
        return self._get(url).text

    def get_bytes(self, url: str, start: int | None = None, end: int | None = None) -> bytes:
        """The body, or only bytes ``start`` to ``end`` (exclusive) of it when given."""
        if start is None or end is None:
            return self._get(url).content
        return self._get(url, {"Range": f"bytes={start}-{end - 1}"}).content

    def _get(self, url: str, headers: dict | None = None) -> requests.Response:
        ok = 206 if headers else 200
        for attempt in range(self.retries + 1):
            backoff = self.delay * 2**attempt
            self._wait(self.delay)
            try:
                response = self._session.get(url, headers=headers, timeout=60)
            except requests.RequestsError as error:
                log.warning("request error", extra={"url": url, "error": str(error)})
                self._wait(backoff)
                continue
            if response.status_code == 403 or response.headers.get("cf-mitigated"):
                raise Blocked(f"{url}: HTTP {response.status_code}, Cloudflare challenge")
            if response.status_code in RETRYABLE_STATUS:
                retry_after = response.headers.get("retry-after", "")
                log.warning("retryable status", extra={"url": url, "status": response.status_code})
                self._wait(float(retry_after) if retry_after.isdigit() else backoff)
                continue
            if response.status_code != ok:
                raise FetchFailed(f"{url}: HTTP {response.status_code}")
            return response
        raise FetchFailed(f"{url}: gave up after {self.retries + 1} attempts")

    def _wait(self, seconds: float) -> None:
        remaining = self._last_request + seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()


@dataclass(frozen=True)
class Listing:
    """A game page as a sitemap lists it: its last-modified date (None when the sitemap gives
    none) and the sitemap URL that listed it."""

    lastmod: datetime | None
    sitemap: str


def sitemap_pages(get, remembered: set[str]) -> dict[str, Listing]:
    """Every Chaos Zero Nightmare page listed in the sitemaps named by robots.txt.

    Prydwen keeps each game in its own sitemap, so the sitemaps in ``remembered`` (those that
    listed game pages last run) are read first; all are read only when robots.txt no longer
    names them or they list no game pages. Ceiling: if the game spread over a second sitemap,
    pages there stay unseen until the remembered ones list none.
    """
    log.info("reading robots.txt")
    robots = get(f"{SITE}/robots.txt")
    sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots)
    if not sitemaps:
        raise FetchFailed("robots.txt lists no sitemaps")
    known = [sitemap for sitemap in sitemaps if sitemap in remembered]
    pages = _read_sitemaps(get, known) if known else {}
    if not pages:
        pages = _read_sitemaps(get, sitemaps)
    if not pages:
        raise FetchFailed("sitemaps list no Chaos Zero Nightmare pages")
    return pages


def _read_sitemaps(get, sitemaps: list[str]) -> dict[str, Listing]:
    pages = {}
    for number, sitemap in enumerate(sitemaps, 1):
        log.info(
            "reading sitemap", extra={"url": sitemap, "number": number, "total": len(sitemaps)}
        )
        for entry in BeautifulSoup(get(sitemap), "xml").find_all("url"):
            loc, lastmod = entry.find("loc"), entry.find("lastmod")
            url = loc.get_text(strip=True).rstrip("/") if loc else ""
            if url.startswith(f"{SITE}/chaos-zero-nightmare"):
                pages[url] = Listing(
                    sitemap_date(lastmod.get_text(strip=True)) if lastmod else None, sitemap
                )
    return pages


def sitemap_date(text: str) -> datetime | None:
    """A sitemap date, UTC when it names no zone; None (the page is downloaded every run) when
    it does not parse."""
    try:
        date = datetime.fromisoformat(text)
    except ValueError:
        return None
    return date if date.tzinfo else date.replace(tzinfo=UTC)
