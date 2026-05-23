"""The Researcher class — the public API users actually touch.

A `Researcher` is the orchestrator. It wires together:

    plan → (search → fetch → extract) per step → build citations → verify → render

It's an async context manager so resources (Tor, the browser) are reliably
torn down even if a research call raises. Most users only need this file.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from penumbra.exceptions import ConfigurationError, PenumbraError
from penumbra.llm.base import LLMProvider, LLMRouter
from penumbra.output.markdown import render_markdown
from penumbra.privacy.fingerprint import FingerprintEngine
from penumbra.privacy.scrubber import PIIScrubber
from penumbra.privacy.tor_controller import TorController
from penumbra.research.browser import PrivateBrowser, SearchResult
from penumbra.research.citations import CitationBuilder
from penumbra.research.extractor import ContentExtractor
from penumbra.research.planner import ResearchPlanner
from penumbra.research.verifier import CrossVerifier
from penumbra.types import (
    Citation,
    PrivacyLevel,
    Report,
    ResearchStep,
    SourcePage,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


_SUMMARY_SYSTEM = """You write tight, factual research briefs. You have been
given a set of attributable claims extracted from web sources. Your output is a
500-800 word summary that synthesizes the claims into a coherent answer to the
original question.

Rules:
- Do NOT invent facts. If the claims don't cover something, say so.
- Cite inline using [N] where N is the claim number from the input.
- Prefer concrete numbers and named entities over vague language.
- Open with the answer in 1-2 sentences. Don't bury the lede.
"""


class Researcher:
    """The main user-facing API.

    >>> async with Researcher(privacy="high") as r:
    ...     report = await r.run("What's the state of open-source LLM agents?")
    ...     print(report.markdown)
    """

    def __init__(
        self,
        *,
        privacy: PrivacyLevel | str | int = PrivacyLevel.MEDIUM,
        cloud_llm: LLMProvider | None = None,
        local_llm: LLMProvider | None = None,
        max_steps: int = 5,
        sources_per_step: int = 4,
        max_concurrent_fetches: int = 3,
        tor_binary: str | None = None,
        verify_tor: bool = True,
    ) -> None:
        self.privacy_level = PrivacyLevel.parse(privacy)
        self._cloud_llm = cloud_llm
        self._local_llm = local_llm
        self._max_steps = max_steps
        self._sources_per_step = sources_per_step
        self._max_concurrent_fetches = max_concurrent_fetches
        self._tor_binary = tor_binary
        self._verify_tor = verify_tor

        self._router: LLMRouter | None = None
        self._tor: TorController | None = None
        self._browser: PrivateBrowser | None = None
        self._scrubber = PIIScrubber() if self.privacy_level.scrubs_pii else None
        self._fp_engine = FingerprintEngine()
        self._extractor = ContentExtractor()
        self._verifier = CrossVerifier()
        self._planner: ResearchPlanner | None = None
        self._citation_builder: CitationBuilder | None = None
        self._started = False

    async def __aenter__(self) -> Researcher:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._started:
            return
        self._setup_router()
        if self.privacy_level.uses_tor:
            self._tor = TorController(tor_binary=self._tor_binary)
            await self._tor.start()
            if self._verify_tor:
                exit_ip = await self._tor.verify_connectivity()
                logger.info("Tor verified — exit IP: %s", exit_ip)
        socks = self._tor.socks_proxy_url if self._tor else None
        self._browser = PrivateBrowser(
            socks_proxy=socks,
            fingerprint_engine=self._fp_engine,
            max_concurrent_fetches=self._max_concurrent_fetches,
        )
        await self._browser.start()
        assert self._router is not None
        self._planner = ResearchPlanner(router=self._router, max_steps=self._max_steps)
        self._citation_builder = CitationBuilder(router=self._router)
        self._started = True
        logger.info(
            "Researcher started (privacy=%s, tor=%s, cloud_llm=%s, local_llm=%s)",
            self.privacy_level.name.lower(),
            self._tor is not None,
            self._cloud_llm.name if self._cloud_llm else "—",
            self._local_llm.name if self._local_llm else "—",
        )

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.stop()
            self._browser = None
        if self._tor is not None:
            await self._tor.stop()
            self._tor = None
        self._started = False

    def _setup_router(self) -> None:
        cloud = self._cloud_llm
        local = self._local_llm
        if cloud is None and local is None:
            cloud, local = self._auto_detect_llms()
        if cloud is None and local is None:
            raise ConfigurationError(
                "No LLM provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
                "or run Ollama locally, or pass cloud_llm=/local_llm= explicitly."
            )
        if self.privacy_level.routes_sensitive_to_local and local is None:
            logger.warning(
                "Privacy level HIGH requested but no local LLM is available — "
                "sensitive subqueries will fall back to cloud."
            )
        self._router = LLMRouter(
            cloud=cloud,
            local=local,
            privacy_level=self.privacy_level,
        )

    def _auto_detect_llms(self) -> tuple[LLMProvider | None, LLMProvider | None]:
        """Try each provider that has credentials; never raise during autodetect."""
        import contextlib

        cloud: LLMProvider | None = None
        local: LLMProvider | None = None
        with contextlib.suppress(Exception):
            from penumbra.llm.anthropic_llm import AnthropicLLM

            cloud = AnthropicLLM()
        if cloud is None:
            with contextlib.suppress(Exception):
                from penumbra.llm.openai_llm import OpenAILLM

                cloud = OpenAILLM()
        with contextlib.suppress(Exception):
            from penumbra.llm.ollama_llm import OllamaLLM

            local = OllamaLLM()
        return cloud, local

    async def run(self, query: str) -> Report:
        """Execute a full research run and return a Report."""
        if not self._started:
            raise PenumbraError("Researcher not started — use `async with` or call .start().")
        assert self._planner is not None
        assert self._citation_builder is not None
        assert self._router is not None
        assert self._browser is not None

        t0 = time.perf_counter()
        scrubbed_query = self._scrub(query)

        plan = await self._planner.plan(scrubbed_query)
        logger.info("Planned %d subqueries", len(plan))

        sources = await self._gather_sources(plan)
        if not sources:
            raise PenumbraError("Research produced no usable sources.")

        any_sensitive = any(s.sensitive for s in plan)
        citations = await self._citation_builder.build(
            scrubbed_query,
            sources,
            sensitive=any_sensitive,
        )
        citations = self._verifier.verify(citations)

        summary = await self._summarize(scrubbed_query, citations, sensitive=any_sensitive)

        duration = time.perf_counter() - t0
        markdown = render_markdown(
            query=query,
            summary=summary,
            plan=plan,
            sources=sources,
            citations=citations,
            privacy_level=self.privacy_level,
            duration_seconds=duration,
        )

        return Report(
            query=query,
            summary=summary,
            markdown=markdown,
            plan=plan,
            sources=sources,
            citations=citations,
            privacy_level=self.privacy_level,
            duration_seconds=duration,
            metadata={
                "cloud_llm": self._cloud_llm.name if self._cloud_llm else None,
                "local_llm": self._local_llm.name if self._local_llm else None,
                "via_tor": self._tor is not None,
            },
        )

    def _scrub(self, text: str) -> str:
        if self._scrubber is None:
            return text
        return self._scrubber.scrub(text).text

    async def _gather_sources(self, plan: list[ResearchStep]) -> list[SourcePage]:
        """Run search + fetch + extract for every step, with per-source isolation."""
        all_sources: list[SourcePage] = []
        seen_urls: set[str] = set()

        for step in plan:
            try:
                results = await self._browser.search(  # type: ignore[union-attr]
                    step.subquery, limit=self._sources_per_step
                )
            except PenumbraError as e:
                logger.warning("Search failed for '%s': %s", step.subquery, e)
                continue

            fetch_tasks = [
                self._fetch_one(r, sensitive_step=step.sensitive)
                for r in results
                if r.url not in seen_urls
            ]
            for r in results:
                seen_urls.add(r.url)

            if not fetch_tasks:
                continue
            results_pages = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for page in results_pages:
                if isinstance(page, SourcePage):
                    all_sources.append(page)

            step.completed = True
            if self._tor is not None and step.sensitive:
                await self._tor.new_circuit()

        return all_sources

    async def _fetch_one(
        self,
        result: SearchResult,
        *,
        sensitive_step: bool,
    ) -> SourcePage | None:
        assert self._browser is not None
        try:
            fetched = await self._browser.fetch(result.url)
        except PenumbraError as e:
            logger.debug("Skip %s: %s", result.url, e)
            return None
        if fetched.status >= 400:
            return None
        extracted = self._extractor.extract(fetched)
        if extracted is None:
            return None
        if not extracted.title:
            extracted = extracted.model_copy(update={"title": result.title})
        return extracted

    async def _summarize(
        self,
        query: str,
        citations: list[Citation],
        *,
        sensitive: bool,
    ) -> str:
        assert self._router is not None
        if not citations:
            return "No verifiable claims were extracted. Try a more specific query."
        bullets = "\n".join(
            f"[{i + 1}] (confidence {c.confidence:.0%}) {c.claim}" for i, c in enumerate(citations)
        )
        prompt = (
            f"Original question:\n{query}\n\n"
            f"Available claims:\n{bullets}\n\n"
            f"Write a 500-800 word synthesis that answers the question using only these claims, "
            f"citing them inline as [N]."
        )
        response = await self._router.complete(
            prompt,
            sensitive=sensitive,
            system=_SUMMARY_SYSTEM,
            max_tokens=1500,
            temperature=0.3,
        )
        return response.text.strip()
