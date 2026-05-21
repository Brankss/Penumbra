"""Penumbra — Privacy-native deep research agent."""

from penumbra.core import Researcher
from penumbra.exceptions import (
    BrowserError,
    LLMError,
    PenumbraError,
    ResearchError,
    ScrubError,
    TorError,
)
from penumbra.types import (
    Citation,
    PrivacyLevel,
    Report,
    ResearchStep,
    SourcePage,
)

__version__ = "0.1.0"

__all__ = [
    "BrowserError",
    "Citation",
    "LLMError",
    "PenumbraError",
    "PrivacyLevel",
    "Report",
    "ResearchError",
    "ResearchStep",
    "Researcher",
    "ScrubError",
    "SourcePage",
    "TorError",
    "__version__",
]
