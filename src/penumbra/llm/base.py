"""LLM provider abstraction and sensitivity-based router.

Penumbra deliberately keeps this layer thin. We don't need LangChain. We need
two things from an LLM:

1. `complete(prompt) -> str`
2. `complete_json(prompt, schema) -> dict`

The `LLMRouter` is the policy that picks WHICH provider to use for a given call:

    - default: the chosen primary (Anthropic / OpenAI / etc.)
    - sensitive: a *local* model (Ollama) if HIGH privacy is on

The router is what makes "PrivacyLevel.HIGH" actually mean something — sensitive
subqueries never touch the cloud.
"""

from __future__ import annotations

import abc
import json
import logging
from dataclasses import dataclass
from typing import Any

from penumbra.exceptions import LLMError
from penumbra.types import PrivacyLevel

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMResponse:
    """Provider-agnostic LLM response."""

    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] | None = None


class LLMProvider(abc.ABC):
    """Minimal interface every Penumbra-compatible LLM must implement."""

    name: str = "abstract"
    is_local: bool = False

    @abc.abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """Free-form text completion."""

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Force the model to emit valid JSON and parse it.

        Default implementation appends a strict instruction. Providers that
        support structured output natively should override this.
        """
        full_system = (
            (system or "")
            + "\n\nYou must respond with a single valid JSON object and nothing else. "
            "No prose, no markdown fences, no commentary."
        ).strip()
        response = await self.complete(
            prompt,
            system=full_system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return _parse_json_robust(response.text)


def _parse_json_robust(text: str) -> dict[str, Any]:
    """Try hard to extract a JSON object from an LLM response."""
    text = text.strip()
    # Strip common markdown fences the model might add despite instructions.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find the first { ... } block.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM did not return valid JSON: {e}") from e
        raise LLMError("LLM response contained no JSON object.") from None


class LLMRouter:
    """Decide which LLM handles each call based on sensitivity and privacy level.

    The contract is intentionally narrow:

    - `pick(sensitive)` returns the provider that should handle this call.
    - At LOW/MEDIUM privacy, sensitive and non-sensitive both go to the cloud
      provider (queries are scrubbed before they leave the host).
    - At HIGH privacy, `sensitive=True` is routed to the local provider, even
      if that costs quality. The user opted into paranoia.
    """

    def __init__(
        self,
        *,
        cloud: LLMProvider | None = None,
        local: LLMProvider | None = None,
        privacy_level: PrivacyLevel = PrivacyLevel.MEDIUM,
    ) -> None:
        if cloud is None and local is None:
            raise LLMError("LLMRouter needs at least one provider (cloud or local).")
        self.cloud = cloud
        self.local = local
        self.privacy_level = privacy_level

    @property
    def default(self) -> LLMProvider:
        """The provider used for non-sensitive calls."""
        if self.cloud is not None:
            return self.cloud
        assert self.local is not None
        return self.local

    def pick(self, *, sensitive: bool = False) -> LLMProvider:
        if sensitive and self.privacy_level.routes_sensitive_to_local and self.local is not None:
            return self.local
        return self.default

    async def complete(
        self,
        prompt: str,
        *,
        sensitive: bool = False,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> LLMResponse:
        provider = self.pick(sensitive=sensitive)
        logger.debug(
            "LLM call -> provider=%s sensitive=%s tokens=%d",
            provider.name,
            sensitive,
            max_tokens,
        )
        return await provider.complete(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def complete_json(
        self,
        prompt: str,
        *,
        sensitive: bool = False,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        provider = self.pick(sensitive=sensitive)
        return await provider.complete_json(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
