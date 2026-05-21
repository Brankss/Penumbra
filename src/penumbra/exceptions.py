"""Penumbra exception hierarchy."""

from __future__ import annotations


class PenumbraError(Exception):
    """Base class for all Penumbra errors."""


class TorError(PenumbraError):
    """Raised when the Tor controller cannot connect, start, or rotate a circuit."""


class BrowserError(PenumbraError):
    """Raised when the headless browser fails to launch or navigate."""


class LLMError(PenumbraError):
    """Raised when an LLM provider returns an error or invalid output."""


class ScrubError(PenumbraError):
    """Raised when PII scrubbing fails or detects unsafe content for the chosen privacy level."""


class ResearchError(PenumbraError):
    """Raised when the research pipeline fails (planning, extraction, verification)."""


class ConfigurationError(PenumbraError):
    """Raised when Penumbra is misconfigured (missing API key, bad privacy level, etc.)."""
