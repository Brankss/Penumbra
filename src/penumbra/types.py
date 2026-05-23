"""Shared data models for Penumbra."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PrivacyLevel(IntEnum):
    """Configurable privacy dial.

    OFF      No Tor, no scrubbing, no fingerprint randomization. Fastest.
    LOW      Fingerprint randomization + PII scrubbing. No Tor. Cloud LLMs.
    MEDIUM   Tor + fingerprint + scrubbing. Cloud LLMs (with scrubbed queries).
    HIGH     Tor + fingerprint + scrubbing. Sensitive subqueries routed to local LLM.
    """

    OFF = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @classmethod
    def parse(cls, value: PrivacyLevel | str | int) -> PrivacyLevel:
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        normalized = value.strip().lower()
        mapping = {
            "off": cls.OFF,
            "none": cls.OFF,
            "0": cls.OFF,
            "low": cls.LOW,
            "1": cls.LOW,
            "medium": cls.MEDIUM,
            "med": cls.MEDIUM,
            "2": cls.MEDIUM,
            "high": cls.HIGH,
            "max": cls.HIGH,
            "3": cls.HIGH,
        }
        if normalized not in mapping:
            raise ValueError(f"Unknown privacy level: {value!r}")
        return mapping[normalized]

    @property
    def uses_tor(self) -> bool:
        return self >= PrivacyLevel.MEDIUM

    @property
    def scrubs_pii(self) -> bool:
        return self >= PrivacyLevel.LOW

    @property
    def randomizes_fingerprint(self) -> bool:
        return self >= PrivacyLevel.LOW

    @property
    def routes_sensitive_to_local(self) -> bool:
        return self >= PrivacyLevel.HIGH


class ResearchStep(BaseModel):
    """A single planned subquery in a multi-step research."""

    index: int
    subquery: str
    rationale: str
    sensitive: bool = False
    completed: bool = False


class SourcePage(BaseModel):
    """A retrieved and extracted web page."""

    url: str
    title: str
    content: str
    excerpt: str = ""
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    via_tor: bool = False
    word_count: int = 0
    domain: str = ""
    confidence: float = 1.0


class Citation(BaseModel):
    """An attributable claim with source(s)."""

    claim: str
    source_urls: list[str]
    confidence: float = 1.0
    cross_verified: bool = False


class Report(BaseModel):
    """Final output of a research run."""

    query: str
    summary: str
    markdown: str
    plan: list[ResearchStep] = Field(default_factory=list)
    sources: list[SourcePage] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    privacy_level: PrivacyLevel = PrivacyLevel.MEDIUM
    duration_seconds: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def save(self, path: str | Path) -> Path:
        """Write the markdown report to disk and return the absolute path."""
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.markdown, encoding="utf-8")
        return path

    def save_json(self, path: str | Path) -> Path:
        """Persist the full structured report as JSON."""
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path
