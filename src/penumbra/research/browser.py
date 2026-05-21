"""Playwright-based private browser with optional Tor routing.

This is the workhorse for retrieval. It does two things:

1. `search(query)` — query a search engine and return result URLs
2. `fetch(url)`    — load a page and return raw HTML

The browser is configured with the active `Fingerprint` and (if Tor is enabled)
routes through a SOCKS5 proxy. Each `fetch()` runs in a fresh BrowserContext so
cookies and storage don't leak between sources.

We use DuckDuckGo's HTML-only endpoint for search because it works through Tor
without CAPTCHAs, and we strip its tracking redirect to get the raw target URL.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

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


_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"


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
        """Run a search and return result URLs (deduped, tracking-stripped)."""
        url = f"{_SEARCH_ENDPOINT}?{urllib.parse.urlencode({'q': query})}"
        async with self._semaphore:
            ctx, _ = await self._new_context()
            try:
                page = await ctx.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded")
                if resp is None or resp.status >= 400:
                    raise BrowserError(
                        f"Search returned status {resp.status if resp else 'none'}"
                    )
                results = await page.evaluate(
                    """() => {
                        const out = [];
                        const items = document.querySelectorAll('.result');
                        for (const el of items) {
                            const a = el.querySelector('a.result__a');
                            const s = el.querySelector('.result__snippet');
                            if (!a) continue;
                            out.push({
                                href: a.getAttribute('href'),
                                title: a.innerText.trim(),
                                snippet: s ? s.innerText.trim() : ''
                            });
                        }
                        return out;
                    }"""
                )
            finally:
                await ctx.close()

        cleaned: list[SearchResult] = []
        seen: set[str] = set()
        for item in results:
            href = _unwrap_duckduckgo_redirect(item.get("href", ""))
            if not href or href in seen:
                continue
            if not href.startswith(("http://", "https://")):
                continue
            seen.add(href)
            cleaned.append(
                SearchResult(
                    url=href,
                    title=item.get("title", "")[:200],
                    snippet=item.get("snippet", "")[:500],
                )
            )
            if len(cleaned) >= limit:
                break

        logger.info("Search '%s' → %d unique results", query[:60], len(cleaned))
        return cleaned

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
