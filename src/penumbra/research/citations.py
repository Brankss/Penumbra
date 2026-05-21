"""Citation builder — turn extracted sources into attributable claims.

Asks the LLM to read N source pages and emit a list of factual claims, each
tagged with the source URLs that support it. Penumbra then runs cross-verification
(see verifier.py) to flag claims with weak support.

Quality > quantity: we deliberately ask for 5-15 claims, not "every fact".
Citation lists with 100 entries are noise; what users want is "here are the
load-bearing claims and where they come from".
"""

from __future__ import annotations

import logging
from typing import Any

from penumbra.exceptions import ResearchError
from penumbra.llm.base import LLMRouter
from penumbra.types import Citation, SourcePage

logger = logging.getLogger(__name__)


_CITATION_SYSTEM_PROMPT = """You extract attributable factual claims from a set
of web sources. Each claim must be supported by at least one source in the set.

Rules:
- A claim is a concrete, falsifiable statement, not an opinion or vague summary.
- Each claim must cite the URL(s) it comes from (using exact URLs from the input).
- Prefer claims that appear in multiple sources — they're stronger.
- Aim for 5-15 high-quality claims. Skip filler.
- If a "fact" only appears in one source and looks dubious, lower its confidence.

Respond with a JSON object:
{
  "citations": [
    {
      "claim": "Concrete statement.",
      "source_urls": ["https://...", "https://..."],
      "confidence": 0.0-1.0
    }
  ]
}
"""


class CitationBuilder:
    """Use the LLM to extract claim-source pairs from a batch of pages."""

    def __init__(self, *, router: LLMRouter, max_claims: int = 15) -> None:
        self._router = router
        self._max_claims = max_claims

    async def build(
        self,
        query: str,
        sources: list[SourcePage],
        *,
        sensitive: bool = False,
    ) -> list[Citation]:
        if not sources:
            return []
        prompt = self._format_prompt(query, sources)
        try:
            data = await self._router.complete_json(
                prompt,
                sensitive=sensitive,
                system=_CITATION_SYSTEM_PROMPT,
                max_tokens=2500,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001 — log and degrade gracefully
            logger.warning("Citation extraction failed: %s", e)
            return []
        return self._parse(data, valid_urls={s.url for s in sources})

    def _format_prompt(self, query: str, sources: list[SourcePage]) -> str:
        chunks = [f"Research question:\n{query}\n\nSources:\n"]
        for i, s in enumerate(sources, start=1):
            preview = s.content[:1500].replace("\n\n", "\n")
            chunks.append(
                f"--- Source [{i}] ---\nURL: {s.url}\nTitle: {s.title}\n\n{preview}\n"
            )
        chunks.append(
            f"\nExtract up to {self._max_claims} attributable claims with source URLs."
        )
        return "\n".join(chunks)

    def _parse(self, data: dict[str, Any], *, valid_urls: set[str]) -> list[Citation]:
        raw = data.get("citations")
        if not isinstance(raw, list):
            raise ResearchError("Citation extractor returned no `citations` array.")

        result: list[Citation] = []
        for item in raw[: self._max_claims]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim", "")).strip()
            if not claim:
                continue
            urls_raw = item.get("source_urls", [])
            if not isinstance(urls_raw, list):
                continue
            urls = [u for u in (str(x).strip() for x in urls_raw) if u in valid_urls]
            if not urls:
                continue
            confidence = float(item.get("confidence", 0.7))
            confidence = max(0.0, min(1.0, confidence))
            result.append(
                Citation(claim=claim, source_urls=urls, confidence=confidence),
            )
        return result
