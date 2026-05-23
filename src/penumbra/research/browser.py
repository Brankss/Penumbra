"""Private browser + multi-backend search.

Two responsibilities:

1. `search(query)` — query a search engine and return result URLs. The default
   path runs DuckDuckGo and Bing through our own headless Chromium because
   real SERPs accept a properly-fingerprinted browser far more reliably than
   they accept lightweight HTTP scrapers. Optional Brave/Tavily API backends
   activate automatically if the corresponding env vars are set.
2. `fetch(url)` — load an arbitrary page and return raw HTML, using Playwright
   headless Chromium in a fresh BrowserContext per source so cookies and
   storage don't leak between sources.

Both layers respect the configured SOCKS5 proxy (Tor) and the active
Fingerprint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


# Public SearXNG instances — used only by the browser-based last-resort path.
_SEARXNG_INSTANCES: tuple[str, ...] = (
    "https://priv.au",
    "https://search.inetol.net",
    "https://baresearch.org",
    "https://searx.be",
    "https://search.disroot.org",
    "https://opnxng.com",
)
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"


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

        Default path uses the headless browser we already have for fetching:
        real SERPs (DuckDuckGo, Bing) accept a properly-fingerprinted browser
        much more reliably than they accept lightweight scrapers. No API key,
        no SearXNG cargo-cult, no third-party dependency — just the browser.

        Cascade order:
        1. Brave Search API     — opt-in via BRAVE_API_KEY (faster if set)
        2. Tavily Search API    — opt-in via TAVILY_API_KEY (faster if set)
        3. DuckDuckGo via Playwright — default, works out of the box
        4. Bing via Playwright       — fallback when DDG blocks
        5. SearXNG via Playwright    — last resort
        """
        query = query.strip()
        if len(query) > 200:
            query = query[:200].rsplit(" ", 1)[0]

        # Premium opt-in APIs. Silent unless the env var is set; no surprise
        # cloud calls and no error if they're not configured.
        brave_key = os.environ.get("BRAVE_API_KEY")
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if brave_key or tavily_key:
            results = await self._try_api_backends(brave_key, tavily_key, query, limit)
            if results:
                return results

        # Default: real SERPs through the headless browser we already run.
        # Brave first (independent index, accepts complex queries, rarely blocks),
        # DuckDuckGo second (now via the simpler html endpoint), Bing last (its
        # results are aggressive about overriding "weird" queries with popular
        # ones, so it's the least reliable for our use case).
        for engine in ("brave", "duckduckgo", "bing"):
            results = await self._try_playwright_engine(engine, query, limit)
            if results:
                logger.info(
                    "Search '%s' via %s (browser) -> %d results",
                    query[:60],
                    engine,
                    len(results),
                )
                return results

        # Last resort: SearXNG JSON endpoint via the browser (passes Cloudflare).
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

    async def _try_api_backends(
        self,
        brave_key: str | None,
        tavily_key: str | None,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        fp = self._fp_engine.generate()
        client_kwargs: dict[str, object] = {
            "timeout": 20.0,
            "follow_redirects": True,
            "headers": {"User-Agent": fp.user_agent, "Accept": "application/json"},
        }
        if self._socks_proxy:
            client_kwargs["proxy"] = self._socks_proxy
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            if brave_key:
                results = await self._try_brave(client, brave_key, query, limit)
                if results:
                    logger.info(
                        "Search '%s' via Brave -> %d results",
                        query[:60],
                        len(results),
                    )
                    return results
            if tavily_key:
                results = await self._try_tavily(client, tavily_key, query, limit)
                if results:
                    logger.info(
                        "Search '%s' via Tavily -> %d results",
                        query[:60],
                        len(results),
                    )
                    return results
        return []

    async def _try_playwright_engine(
        self,
        engine: str,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        """Search a real engine SERP using the configured Playwright browser."""
        if self._browser is None:
            return []

        if engine == "brave":
            url = (
                "https://search.brave.com/search?"
                f"{urllib.parse.urlencode({'q': query, 'source': 'web'})}"
            )
            wait_selector = "#results .snippet, [data-type='web'] a, .snippet"
            eval_js = """
            () => {
                const out = [];
                const items = document.querySelectorAll(
                    "#results .snippet, [data-type='web'].snippet, .snippet"
                );
                for (const el of items) {
                    const a = el.querySelector("a.h, a.heading-serpresult, a[href]");
                    const t = el.querySelector(".title, .heading-serpresult, h3, h4");
                    const d = el.querySelector(
                        ".snippet-description, .description, .snippet-content"
                    );
                    if (!a || !a.href) continue;
                    if (a.href.startsWith("https://search.brave.com")) continue;
                    out.push({
                        url: a.href,
                        title: (t ? t.innerText : a.innerText || "").trim(),
                        snippet: d ? (d.innerText || "").trim() : "",
                    });
                }
                return out;
            }
            """
        elif engine == "duckduckgo":
            # The html.duckduckgo.com SERP is server-rendered (no JS needed
            # to see results) and Playwright clears the Cloudflare interstitial
            # automatically. This is much more reliable than the JS-app SPA at
            # duckduckgo.com/?q=..., which often redirects to the homepage.
            url = (
                "https://html.duckduckgo.com/html/?"
                f"{urllib.parse.urlencode({'q': query, 'kl': 'us-en'})}"
            )
            wait_selector = ".result, .web-result"
            eval_js = """
            () => {
                const out = [];
                for (const el of document.querySelectorAll(".result, .web-result")) {
                    const a = el.querySelector("a.result__a")
                          || el.querySelector("h2 a")
                          || el.querySelector("a");
                    const s = el.querySelector(".result__snippet");
                    if (!a) continue;
                    let href = a.getAttribute("href") || a.href || "";
                    if (href.startsWith("//")) href = "https:" + href;
                    if (href.includes("duckduckgo.com/l/")) {
                        try {
                            const u = new URL(href, "https://duckduckgo.com");
                            const real = u.searchParams.get("uddg");
                            if (real) href = decodeURIComponent(real);
                        } catch (e) {}
                    }
                    if (!href || !href.startsWith("http")) continue;
                    out.push({
                        url: href,
                        title: (a.innerText || '').trim(),
                        snippet: s ? (s.innerText || '').trim() : '',
                    });
                }
                return out;
            }
            """
        elif engine == "bing":
            # Bing treats unquoted `-` as the exclusion operator: a query like
            # "open-source RAG" becomes "open RAG MINUS source", which mangles
            # the intent. Strip dashes for Bing (other engines tolerate them).
            bing_query = query.replace("-", " ")
            bing_params = {
                "q": bing_query,
                "mkt": "en-US",
                "setlang": "en",
                "cc": "US",
            }
            url = f"https://www.bing.com/search?{urllib.parse.urlencode(bing_params)}"
            wait_selector = "li.b_algo, .b_searchboxForm"
            # Bing wraps every organic link in bing.com/ck/a?u=a1<base64>...
            # Decode the `u` param (skip the 2-char version marker) to get the
            # real destination URL.
            eval_js = """
            () => {
                function decodeBing(href) {
                    try {
                        const u = new URL(href);
                        if (!u.searchParams.has('u')) return href;
                        let raw = u.searchParams.get('u');
                        if (raw.startsWith('a1')) raw = raw.slice(2);
                        const pad = '='.repeat((4 - raw.length % 4) % 4);
                        const std = (raw + pad).replace(/-/g, '+').replace(/_/g, '/');
                        return atob(std);
                    } catch (e) { return href; }
                }
                const out = [];
                for (const el of document.querySelectorAll("li.b_algo")) {
                    const a = el.querySelector("h2 a");
                    const p = el.querySelector("p");
                    if (!a || !a.href) continue;
                    let href = a.href;
                    if (href.includes("bing.com/ck/")) {
                        href = decodeBing(href);
                    }
                    out.push({
                        url: href,
                        title: (a.innerText || '').trim(),
                        snippet: p ? (p.innerText || '').trim() : '',
                    });
                }
                return out;
            }
            """
        else:
            return []

        async with self._semaphore:
            ctx = await self._new_search_context()
            if ctx is None:
                return []
            raw: list[dict[str, str]] = []
            try:
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded")
                except Exception as e:  # noqa: BLE001 — best-effort fallback
                    logger.debug("%s navigation failed: %s", engine, e)
                    return []
                if resp is None or resp.status >= 400:
                    logger.debug(
                        "%s returned status %s",
                        engine,
                        resp.status if resp else "none",
                    )
                    return []
                try:
                    await page.wait_for_selector(wait_selector, timeout=10_000)
                except Exception:  # noqa: BLE001 — no results / blocked
                    try:
                        title = await page.title()
                    except Exception:  # noqa: BLE001
                        title = "?"
                    logger.debug(
                        "%s: no result selector after wait (page title: %r)", engine, title
                    )
                    return []
                try:
                    raw = await page.evaluate(eval_js)
                except Exception as e:  # noqa: BLE001
                    logger.debug("%s eval failed: %s", engine, e)
                    return []
            finally:
                await ctx.close()

        cleaned: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            url_r = str(item.get("url", "")).strip()
            if not url_r or url_r in seen:
                continue
            if not url_r.startswith(("http://", "https://")):
                continue
            if any(
                marker in url_r
                for marker in (
                    "bing.com/aclick",
                    "bing.com/ck/",  # any leftover after decode → still tracking, skip
                    "duckduckgo.com/y.js",
                    "duckduckgo.com/l/",
                )
            ):
                continue
            seen.add(url_r)
            cleaned.append(
                SearchResult(
                    url=url_r,
                    title=str(item.get("title", ""))[:200],
                    snippet=str(item.get("snippet", ""))[:500],
                )
            )
            if len(cleaned) >= limit:
                break
        return cleaned

    async def _new_search_context(self) -> object | None:
        """Build a context tailored for search engines: always en-US locale.

        Random locales (it-IT, fr-FR, …) cause Bing and DDG to serve regional
        SERPs that miss the keywords or surface dictionary entries instead of
        web results. We pin English here while keeping a random fingerprint
        for everything else.
        """
        if self._browser is None:
            return None
        fingerprint = self._fp_engine.generate()
        opts = FingerprintEngine.context_options(fingerprint)
        opts["locale"] = "en-US"
        opts["timezone_id"] = "America/New_York"
        try:
            ctx = await self._browser.new_context(**opts)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.debug("search context creation failed: %s", e)
            return None
        await self._fp_engine.apply(ctx, fingerprint)  # type: ignore[arg-type]
        ctx.set_default_navigation_timeout(self._timeout)
        return ctx

    async def _try_brave(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        try:
            resp = await client.get(
                _BRAVE_ENDPOINT,
                params={"q": query, "count": min(max(limit, 1), 20)},
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                },
            )
        except httpx.HTTPError as e:
            logger.debug("Brave network error: %s", e)
            return []
        if resp.status_code != 200:
            logger.debug("Brave returned %d: %s", resp.status_code, resp.text[:200])
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        raw = data.get("web", {}).get("results", [])
        if not isinstance(raw, list):
            return []
        cleaned: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url or url in seen or not url.startswith(("http://", "https://")):
                continue
            seen.add(url)
            cleaned.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title", ""))[:200],
                    snippet=str(item.get("description", ""))[:500],
                )
            )
            if len(cleaned) >= limit:
                break
        return cleaned

    async def _try_tavily(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        try:
            resp = await client.post(
                _TAVILY_ENDPOINT,
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": limit,
                    "search_depth": "basic",
                },
            )
        except httpx.HTTPError as e:
            logger.debug("Tavily network error: %s", e)
            return []
        if resp.status_code != 200:
            logger.debug("Tavily returned %d: %s", resp.status_code, resp.text[:200])
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        raw = data.get("results", [])
        if not isinstance(raw, list):
            return []
        cleaned: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url or url in seen or not url.startswith(("http://", "https://")):
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
