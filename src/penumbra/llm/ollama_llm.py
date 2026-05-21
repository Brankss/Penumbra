"""Ollama provider — local LLM, never touches the cloud.

Ollama is the privacy-friendly default for sensitive subqueries when
`PrivacyLevel.HIGH` is active. The user is expected to have Ollama running
locally; we talk to its HTTP API directly to avoid pulling the SDK as a hard
dependency.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from penumbra.exceptions import ConfigurationError, LLMError
from penumbra.llm.base import LLMProvider, LLMResponse


class OllamaLLM(LLMProvider):
    """Provider for local Ollama models."""

    name = "ollama"
    is_local = True

    DEFAULT_MODEL = "qwen2.5:7b"

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model or os.environ.get("OLLAMA_MODEL", self.DEFAULT_MODEL)
        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        ).rstrip("/")
        self._timeout = timeout

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system:
            payload["system"] = system

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self.base_url}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.ConnectError as e:
            raise ConfigurationError(
                f"Cannot reach Ollama at {self.base_url}. Is `ollama serve` running?"
            ) from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama call failed: {e}") from e

        return LLMResponse(
            text=data.get("response", ""),
            model=self.model,
            provider=self.name,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            raw=data,
        )
