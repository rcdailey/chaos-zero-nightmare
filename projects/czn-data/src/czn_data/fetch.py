"""HTTP access to prydwen.gg.

Prydwen sits behind Cloudflare bot protection, which challenges ordinary HTTP clients.
``curl_cffi`` sends requests with Chrome's TLS and HTTP/2 fingerprint, which currently passes
without running JavaScript. If Cloudflare starts challenging it anyway, ``Blocked`` is raised;
retrying cannot fix that, so the run stops and alerts.
"""

import logging
import re
import time

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
    def __init__(self, delay: float = CRAWL_DELAY_SECONDS, retries: int = 3):
        self.delay = delay
        self.retries = retries
        self._session = requests.Session(impersonate="chrome")
        self._last_request = 0.0

    def get(self, url: str) -> str:
        return self._get(url).text

    def get_bytes(self, url: str) -> bytes:
        return self._get(url).content

    def _get(self, url: str) -> requests.Response:
        for attempt in range(self.retries + 1):
            backoff = self.delay * 2**attempt
            self._wait(self.delay)
            try:
                response = self._session.get(url, timeout=30)
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
            if response.status_code != 200:
                raise FetchFailed(f"{url}: HTTP {response.status_code}")
            return response
        raise FetchFailed(f"{url}: gave up after {self.retries + 1} attempts")

    def _wait(self, seconds: float) -> None:
        remaining = self._last_request + seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()


def sitemap_urls(get) -> list[str]:
    """Every Chaos Zero Nightmare page URL listed in the sitemaps named by robots.txt."""
    log.info("reading robots.txt")
    robots = get(f"{SITE}/robots.txt")
    sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots)
    if not sitemaps:
        raise FetchFailed("robots.txt lists no sitemaps")
    urls = set()
    for number, sitemap in enumerate(sitemaps, 1):
        log.info(
            "reading sitemap", extra={"url": sitemap, "number": number, "total": len(sitemaps)}
        )
        for url in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", get(sitemap)):
            if url.rstrip("/").startswith(f"{SITE}/chaos-zero-nightmare"):
                urls.add(url.rstrip("/"))
    if not urls:
        raise FetchFailed("sitemaps list no Chaos Zero Nightmare pages")
    return sorted(urls)
