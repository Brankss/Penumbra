"""OpenAI provider."""

from __future__ import annotations

import os
from typing import Any

from penumbra.exceptions import ConfigurationError, LLMError
from penumbra.llm.base import LLMProvider, LLMResponse


class OpenAILLM(LLMProvider):
    """Provider for OpenAI models."""

    name = "openai"
    is_local = False

    DEFAULT_MODEL = "gpt-4o"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ConfigurationError(
                "`openai` is not installed. Install with: pip install penumbra[openai]"
            ) from e

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ConfigurationError(
                "OPENAI_API_KEY not set. Pass api_key= or export the env var."
            )
        self.model = model or self.DEFAULT_MODEL
        self._client = AsyncOpenAI(api_key=key, base_url=base_url)

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp: Any = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            raise LLMError(f"OpenAI call failed: {e}") from e

        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return LLMResponse(
            text=text,
            model=self.model,
            provider=self.name,
            input_tokens=getattr(usage, "prompt_tokens", 0),
            output_tokens=getattr(usage, "completion_tokens", 0),
        )
