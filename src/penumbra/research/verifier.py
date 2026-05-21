"""Cross-source verification — boost confidence in claims with multiple sources.

This is deliberately a heuristic, not an LLM call. The LLM has already done
the hard work of extracting claims; the verifier just applies a deterministic
rule: a claim cited by multiple independent domains is more trustworthy than
one cited by a single domain.

This is the place we'll later wire in optional fact-checking against trusted
indexes (Wikipedia, primary sources), but the heuristic gets us 80% of the
value with 0% of the dependency cost.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from penumbra.types import Citation


@dataclass(slots=True)
class VerifierConfig:
    multi_source_boost: float = 0.15
    single_source_penalty: float = 0.10
    cross_verified_threshold: int = 2


class CrossVerifier:
    """Adjust per-citation confidence based on independent source overlap."""

    def __init__(self, config: VerifierConfig | None = None) -> None:
        self.config = config or VerifierConfig()

    def verify(self, citations: list[Citation]) -> list[Citation]:
        for citation in citations:
            domains = {self._domain(u) for u in citation.source_urls}
            domains.discard("")
            n = len(domains)
            if n >= self.config.cross_verified_threshold:
                citation.cross_verified = True
                citation.confidence = min(
                    1.0,
                    citation.confidence + self.config.multi_source_boost,
                )
            else:
                citation.cross_verified = False
                citation.confidence = max(
                    0.0,
                    citation.confidence - self.config.single_source_penalty,
                )
        return citations

    @staticmethod
    def _domain(url: str) -> str:
        try:
            host = urllib.parse.urlparse(url).netloc.lower()
        except ValueError:
            return ""
        if host.startswith("www."):
            host = host[4:]
        return host
