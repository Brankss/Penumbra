"""Content extraction — strip a page down to its meaningful text.

We use `trafilatura` as the primary extractor (best-in-class for news/article
content) with a `selectolax` fallback for cases trafilatura returns empty.

The goal is to produce text an LLM can summarize without wasting tokens on
boilerplate (nav, footer, cookie banners). We deliberately keep the extracted
content compact (truncated to a configurable byte budget) because feeding a
600KB HTML dump to an LLM is the fastest way to burn a credit card.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass

from penumbra.research.browser import FetchedPage
from penumbra.types import SourcePage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExtractionOptions:
    max_chars: int = 12_000
    excerpt_chars: int = 280
    include_links: bool = False


class ContentExtractor:
    """Reduce a fetched page to its essential content."""

    def __init__(self, options: ExtractionOptions | None = None) -> None:
        self.options = options or ExtractionOptions()

    def extract(self, page: FetchedPage) -> SourcePage | None:
        text = self._extract_trafilatura(page.html)
        if not text:
            text = self._extract_fallback(page.html)
        if not text or len(text.strip()) < 50:
            logger.debug("Skipping %s — extraction produced too little content", page.url)
            return None

        title = self._extract_title(page.html) or page.url
        truncated = text[: self.options.max_chars]
        excerpt = truncated[: self.options.excerpt_chars].strip()
        return SourcePage(
            url=page.final_url,
            title=title,
            content=truncated,
            excerpt=excerpt,
            via_tor=page.via_tor,
            word_count=len(truncated.split()),
            domain=urllib.parse.urlparse(page.final_url).netloc,
        )

    def _extract_trafilatura(self, html: str) -> str:
        try:
            import trafilatura
        except ImportError:
            return ""
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                include_links=self.options.include_links,
                favor_precision=True,
                no_fallback=False,
            )
        except Exception as e:  # noqa: BLE001 — trafilatura raises various, never fatal
            logger.debug("Trafilatura failed: %s", e)
            return ""
        return extracted or ""

    def _extract_fallback(self, html: str) -> str:
        try:
            from selectolax.parser import HTMLParser
        except ImportError:
            return ""
        try:
            tree = HTMLParser(html)
            for tag in ("script", "style", "noscript", "iframe", "footer", "nav", "aside"):
                for node in tree.css(tag):
                    node.decompose()
            body = tree.body
            if body is None:
                return ""
            text = body.text(separator="\n", strip=True)
            lines = [ln for ln in text.splitlines() if len(ln.strip()) > 30]
            return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return ""

    def _extract_title(self, html: str) -> str | None:
        import contextlib

        with contextlib.suppress(Exception):
            from selectolax.parser import HTMLParser

            tree = HTMLParser(html)
            t = tree.css_first("title")
            if t is not None and t.text(strip=True):
                return t.text(strip=True)[:300]
        return None
