"""Private browser + multi-backend search.

Two responsibilities:

1. `search(query)` — query a search engine and return result URLs.
   Uses lightweight HTTP (httpx) against SearXNG public instances with a
   DuckDuckGo-Lite fallback. No Playwright needed for search — it's faster,
   less detectable, and avoids the 403 dance with major engines.
2. `fetch(url)` — load an arbitrary page and return raw HTML, using
   Playwright headless Chromium in a fresh BrowserContext per source so
   cookies and storage don't leak between sources.

Both layers respect the configured SOCKS5 proxy (Tor) and the active
Fingerprint when applicable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from penumbra.exceptions import BrowserError
from penumbra.privacy.fingerprint import Fingerprint, FingerprintEngine

if TYPE_CHECKING:
    from playwright.async_api import Browser

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchedPage:
    """Raw page returned by the browser before content extraction."""

    url: str
    final_url: str
    html: str
    status: int
    via_tor: bool


@dataclass(slots=True)
class SearchResult:
    """Lightweight search-result reference."""

    url: str
    title: str
    snippet: str


# Search backends are tried in order. SearXNG instances first (JSON output,
# privacy-respecting, federated), with DuckDuckGo Lite as the HTML fallback.
# Instances rotate naturally because we shuffle the SearXNG list per session.
_SEARXNG_INSTANCES: tuple[str, ...] = (
    "https://priv.au",
    "https://search.inetol.net",
    "https://baresearch.org",
    "https://searx.be",
    "https://search.disroot.org",
    "https://opnxng.com",
)
_DDG_LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"


class PrivateBrowser:
    """Headless browser with privacy features baked in."""

    def __init__(
        self,
        *,
        socks_proxy: str | None = None,
        fingerprint_engine: FingerprintEngine | None = None,
        nav_timeout_ms: int = 30_000,
        max_concurrent_fetches: int = 3,
    ) -> None:
        self._socks_proxy = socks_proxy
        self._fp_engine = fingerprint_engine or FingerprintEngine()
        self._timeout = nav_timeout_ms
        self._semaphore = asyncio.Semaphore(max_concurrent_fetches)
        self._playwright_cm: object | None = None
        self._browser: Browser | None = None

    @property
    def via_tor(self) -> bool:
        return self._socks_proxy is not None

    async def __aenter__(self) -> PrivateBrowser:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserError(
                "playwright not installed. Install with: pip install playwright"
            ) from e

        try:
            self._playwright_cm = await async_playwright().start()
            launch_kwargs: dict[str, object] = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
            if self._socks_proxy:
                launch_kwargs["proxy"] = {"server": self._socks_proxy}
            self._browser = await self._playwright_cm.chromium.launch(**launch_kwargs)  # type: ignore[attr-defined]
        except Exception as e:
            await self.stop()
            raise BrowserError(f"Failed to launch browser: {e}") from e

    async def stop(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001 — cleanup best-effort
                logger.debug("Browser close failed", exc_info=True)
            self._browser = None
        if self._playwright_cm is not None:
            try:
                await self._playwright_cm.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright_cm = None

    async def _new_context(self) -> tuple[object, Fingerprint]:
        if self._browser is None:
            raise BrowserError("Browser not started.")
        fingerprint = self._fp_engine.generate()
        ctx = await self._browser.new_context(
            **FingerprintEngine.context_options(fingerprint),  # type: ignore[arg-type]
        )
        await self._fp_engine.apply(ctx, fingerprint)  # type: ignore[arg-type]
        ctx.set_default_navigation_timeout(self._timeout)
        return ctx, fingerprint

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Run a search across backends and return result URLs (deduped, clean).

        Strategy:
        1. Try each SearXNG instance with format=json. If one returns results,
           use them.
        2. Fall back to DuckDuckGo Lite HTML parsing.
        """
        fp = self._fp_engine.generate()
        headers = {
            "User-Agent": fp.user_agent,
            "Accept-Language": f"{fp.locale},en;q=0.5",
            "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        }
        # httpx accepts proxy=None to mean "direct"
        client_kwargs: dict[str, object] = {
            "timeout": 20.0,
            "follow_redirects": True,
            "headers": headers,
        }
        if self._socks_proxy:
            client_kwargs["proxy"] = self._socks_proxy

        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            for instance in _SEARXNG_INSTANCES:
                results = await self._try_searxng(client, instance, query, limit)
                if results:
                    logger.info(
                        "Search '%s' via SearXNG-http (%s) -> %d results",
                        query[:60],
                        instance,
                        len(results),
                    )
                    return results
            results = await self._try_ddg_lite(client, query, limit)
            if results:
                logger.info(
                    "Search '%s' via DDG-Lite -> %d results",
                    query[:60],
                    len(results),
                )
                return results

        # Last resort: load SearXNG via the actual browser so Cloudflare's
        # JS challenge can resolve. Slower (~5-15s per challenge) but reliable.
        for instance in _SEARXNG_INSTANCES:
            results = await self._try_searxng_playwright(instance, query, limit)
            if results:
                logger.info(
                    "Search '%s' via SearXNG-browser (%s) -> %d results",
                    query[:60],
                    instance,
                    len(results),
                )
                return results

        raise BrowserError(
            f"All search backends failed for query: {query[:80]!r}. "
            "Try again or check connectivity."
        )

    async def _try_searxng(
        self,
        client: httpx.AsyncClient,
        instance: str,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        url = f"{instance.rstrip('/')}/search"
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": "0",
        }
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            logger.debug("SearXNG %s network error: %s", instance, e)
            return []
        if resp.status_code != 200:
            logger.debug("SearXNG %s returned %d", instance, resp.status_code)
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.debug("SearXNG %s returned non-JSON (probably HTML)", instance)
            return []
        return self._parse_searxng(data, limit)

    def _parse_searxng(self, data: dict[str, object], limit: int) -> list[SearchResult]:
        raw = data.get("results")
        if not isinstance(raw, list):
            return []
        cleaned: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url or url in seen:
                continue
            if not url.startswith(("http://", "https://")):
                continue
            seen.add(url)
            cleaned.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title", ""))[:200],
                    snippet=str(item.get("content", ""))[:500],
                )
            )
            if len(cleaned) >= limit:
                break
        return cleaned

    async def _try_ddg_lite(
        self,
        client: httpx.AsyncClient,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        try:
            resp = await client.get(
                _DDG_LITE_ENDPOINT,
                params={"q": query, "kl": "us-en"},
            )
        except httpx.HTTPError as e:
            logger.debug("DDG-Lite network error: %s", e)
            return []
        if resp.status_code != 200:
            logger.debug("DDG-Lite returned %d", resp.status_code)
            return []
        return self._parse_ddg_lite(resp.text, limit)

    async def _try_searxng_playwright(
        self,
        instance: str,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        """Use Playwright to hit a SearXNG JSON endpoint behind Cloudflare/rate limits.

        The browser solves the JS challenge transparently; we then read the
        raw JSON body that SearXNG renders inside the page.
        """
        if self._browser is None:
            return []
        url = (
            f"{instance.rstrip('/')}/search?"
            f"{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
        )
        async with self._semaphore:
            try:
                ctx, _ = await self._new_context()
            except BrowserError:
                return []
            try:
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded")
                except Exception as e:  # noqa: BLE001 — best-effort fallback
                    logger.debug("Playwright nav to %s failed: %s", instance, e)
                    return []
                if resp is None or resp.status != 200:
                    logger.debug(
                        "Playwright SearXNG %s returned %s",
                        instance,
                        resp.status if resp else "none",
                    )
                    return []
                body = await page.evaluate("() => document.body.innerText")
                if not body or not body.strip().startswith(("{", "[")):
                    logger.debug("Playwright SearXNG %s returned non-JSON body", instance)
                    return []
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    logger.debug("Playwright SearXNG %s body not valid JSON", instance)
                    return []
            finally:
                await ctx.close()
        if not isinstance(data, dict):
            return []
        return self._parse_searxng(data, limit)

    def _parse_ddg_lite(self, html: str, limit: int) -> list[SearchResult]:
        with contextlib.suppress(Exception):
            from selectolax.parser import HTMLParser

            tree = HTMLParser(html)
            cleaned: list[SearchResult] = []
            seen: set[str] = set()
            for a in tree.css("a[href]"):
                href = _unwrap_duckduckgo_redirect(a.attributes.get("href", "") or "")
                if not href or href in seen:
                    continue
                if not href.startswith(("http://", "https://")):
                    continue
                if "duckduckgo.com" in href or "google.com/search" in href:
                    continue
                seen.add(href)
                cleaned.append(
                    SearchResult(
                        url=href,
                        title=a.text(strip=True)[:200],
                        snippet="",
                    )
                )
                if len(cleaned) >= limit:
                    break
            return cleaned
        return []

    async def fetch(self, url: str) -> FetchedPage:
        """Load a page in an isolated context and return raw HTML."""
        async with self._semaphore:
            ctx, _ = await self._new_context()
            try:
                page = await ctx.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded")
                status = resp.status if resp else 0
                html = await page.content()
                final_url = page.url
            except Exception as e:
                logger.debug("Fetch failed for %s: %s", url, e)
                raise BrowserError(f"Failed to fetch {url}: {e}") from e
            finally:
                await ctx.close()

        return FetchedPage(
            url=url,
            final_url=final_url,
            html=html,
            status=status,
            via_tor=self.via_tor,
        )


def _unwrap_duckduckgo_redirect(href: str) -> str:
    """DuckDuckGo wraps result links in `/l/?uddg=...`. Unwrap it."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        parsed = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(parsed.query)
        target = params.get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return href
