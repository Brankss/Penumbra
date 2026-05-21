"""Anthropic Claude provider."""

from __future__ import annotations

import os
from typing import Any

from penumbra.exceptions import ConfigurationError, LLMError
from penumbra.llm.base import LLMProvider, LLMResponse


class AnthropicLLM(LLMProvider):
    """Provider for Anthropic Claude models."""

    name = "anthropic"
    is_local = False

    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise ConfigurationError(
                "`anthropic` is not installed. Install with: pip install penumbra[anthropic]"
            ) from e

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigurationError(
                "ANTHROPIC_API_KEY not set. Pass api_key= or export the env var."
            )
        self.model = model or self.DEFAULT_MODEL
        self._client = AsyncAnthropic(api_key=key, base_url=base_url)

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> LLMResponse:
        try:
            resp: Any = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise LLMError(f"Anthropic call failed: {e}") from e

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        return LLMResponse(
            text=text,
            model=self.model,
            provider=self.name,
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
        )
