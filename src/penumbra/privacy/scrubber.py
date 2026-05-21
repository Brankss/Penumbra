"""PII scrubbing — strip identifying tokens from queries before they hit a cloud LLM.

This is the deliberate "I don't trust the LLM provider with my raw query" layer.
The scrubber operates in two modes:

- `redact`     replace each match with a generic marker (`[EMAIL]`, `[IP]`, …)
- `tokenize`   replace each match with a stable placeholder (`<PII_3>`) and return
               a mapping so the caller can re-inject originals locally after the
               LLM returns its answer.

The patterns are intentionally conservative. Penumbra prefers false positives
(over-redaction) to false negatives (PII leakage). The user can opt out per
category via `disabled_categories`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from penumbra.exceptions import ScrubError


class ScrubMode(str, Enum):
    REDACT = "redact"
    TOKENIZE = "tokenize"


class PIICategory(str, Enum):
    EMAIL = "email"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    MAC = "mac"
    IBAN = "iban"
    CRYPTO_BTC = "crypto_btc"
    CRYPTO_ETH = "crypto_eth"
    URL_AUTH = "url_auth"
    JWT = "jwt"
    API_KEY = "api_key"


# Patterns are applied in this order. Specific patterns (credit cards, SSNs)
# must come before more general ones (phone numbers) so they claim their bytes
# first and the greedy PHONE regex doesn't eat the digits.
_PATTERNS: Final[dict[PIICategory, re.Pattern[str]]] = {
    PIICategory.URL_AUTH: re.compile(
        r"https?://[^/\s:@]+:[^/\s@]+@\S+",
    ),
    PIICategory.JWT: re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    ),
    PIICategory.API_KEY: re.compile(
        r"\b(?:sk|pk|rk|ak|api[_-]?key)[_\-][A-Za-z0-9]{16,}\b",
        re.IGNORECASE,
    ),
    PIICategory.EMAIL: re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    ),
    PIICategory.CRYPTO_ETH: re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    PIICategory.CRYPTO_BTC: re.compile(
        r"\b(?:bc1[a-z0-9]{8,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b",
    ),
    PIICategory.IBAN: re.compile(
        r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b",
    ),
    PIICategory.IPV6: re.compile(
        r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b",
    ),
    PIICategory.IPV4: re.compile(
        r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
        r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b",
    ),
    PIICategory.MAC: re.compile(
        r"\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b",
    ),
    PIICategory.CREDIT_CARD: re.compile(
        r"\b(?:\d[ \-]?){13,19}\b",
    ),
    PIICategory.SSN: re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    PIICategory.PHONE: re.compile(
        r"(?<!\w)(?:\+?\d{1,3}[\s\-.]?)?(?:\(\d{2,4}\)[\s\-.]?)?"
        r"\d{2,4}[\s\-.]?\d{2,4}[\s\-.]?\d{2,5}(?!\w)",
    ),
}


def _luhn_valid(number: str) -> bool:
    """Validate a credit-card-ish number using the Luhn checksum."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


@dataclass(slots=True)
class ScrubResult:
    """Result of scrubbing a piece of text."""

    text: str
    mapping: dict[str, str] = field(default_factory=dict)
    matches_by_category: dict[PIICategory, int] = field(default_factory=dict)

    @property
    def total_matches(self) -> int:
        return sum(self.matches_by_category.values())

    def restore(self, text: str) -> str:
        """Reverse a tokenization — inject the original PII back into a response."""
        for token, original in self.mapping.items():
            text = text.replace(token, original)
        return text


class PIIScrubber:
    """Deterministic, regex-based PII scrubber.

    >>> scrubber = PIIScrubber()
    >>> result = scrubber.scrub("Contact me at alice@example.com or 192.168.1.1")
    >>> "alice" in result.text
    False
    >>> result.matches_by_category[PIICategory.EMAIL]
    1
    """

    def __init__(
        self,
        *,
        mode: ScrubMode = ScrubMode.REDACT,
        disabled_categories: set[PIICategory] | None = None,
    ) -> None:
        self.mode = mode
        self.disabled_categories = disabled_categories or set()

    def scrub(self, text: str) -> ScrubResult:
        if not text:
            return ScrubResult(text=text)
        result = ScrubResult(text=text)
        token_counter = 0

        for category, pattern in _PATTERNS.items():
            if category in self.disabled_categories:
                continue
            matches = list(pattern.finditer(result.text))
            if category is PIICategory.CREDIT_CARD:
                matches = [m for m in matches if _luhn_valid(m.group(0))]
            if not matches:
                continue

            new_text: list[str] = []
            cursor = 0
            for m in matches:
                new_text.append(result.text[cursor : m.start()])
                if self.mode is ScrubMode.REDACT:
                    placeholder = f"[{category.value.upper()}]"
                else:
                    token_counter += 1
                    placeholder = f"<PII_{token_counter}>"
                    result.mapping[placeholder] = m.group(0)
                new_text.append(placeholder)
                cursor = m.end()
            new_text.append(result.text[cursor:])
            result.text = "".join(new_text)
            result.matches_by_category[category] = (
                result.matches_by_category.get(category, 0) + len(matches)
            )

        return result

    def assert_clean(self, text: str) -> None:
        """Raise ScrubError if any known PII pattern survives. Useful as a paranoid gate."""
        result = self.scrub(text)
        if result.total_matches > 0:
            raise ScrubError(
                f"Text contains {result.total_matches} suspected PII match(es): "
                f"{dict(result.matches_by_category)}"
            )
