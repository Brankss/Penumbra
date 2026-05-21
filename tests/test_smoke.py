"""Smoke tests — no network, no LLM calls. Catch regressions in deterministic code."""

from __future__ import annotations

import pytest

from penumbra import PrivacyLevel, __version__
from penumbra.output.markdown import render_markdown
from penumbra.privacy.fingerprint import FingerprintEngine
from penumbra.privacy.scrubber import (
    PIICategory,
    PIIScrubber,
    ScrubMode,
)
from penumbra.research.verifier import CrossVerifier
from penumbra.types import Citation, ResearchStep, SourcePage


def test_version_present() -> None:
    assert __version__
    assert __version__.count(".") == 2


class TestPrivacyLevel:
    def test_parse_strings(self) -> None:
        assert PrivacyLevel.parse("off") is PrivacyLevel.OFF
        assert PrivacyLevel.parse("low") is PrivacyLevel.LOW
        assert PrivacyLevel.parse("MEDIUM") is PrivacyLevel.MEDIUM
        assert PrivacyLevel.parse("high") is PrivacyLevel.HIGH
        assert PrivacyLevel.parse("max") is PrivacyLevel.HIGH

    def test_parse_ints(self) -> None:
        assert PrivacyLevel.parse(0) is PrivacyLevel.OFF
        assert PrivacyLevel.parse(3) is PrivacyLevel.HIGH

    def test_parse_unknown(self) -> None:
        with pytest.raises(ValueError):
            PrivacyLevel.parse("paranoid-plus")

    def test_properties(self) -> None:
        assert not PrivacyLevel.OFF.uses_tor
        assert not PrivacyLevel.LOW.uses_tor
        assert PrivacyLevel.MEDIUM.uses_tor
        assert PrivacyLevel.HIGH.uses_tor
        assert PrivacyLevel.HIGH.routes_sensitive_to_local
        assert not PrivacyLevel.MEDIUM.routes_sensitive_to_local


class TestPIIScrubber:
    def test_email(self) -> None:
        s = PIIScrubber()
        result = s.scrub("Email me at alice@example.com tomorrow")
        assert "alice@example.com" not in result.text
        assert "[EMAIL]" in result.text
        assert result.matches_by_category[PIICategory.EMAIL] == 1

    def test_ipv4(self) -> None:
        s = PIIScrubber()
        result = s.scrub("Server is at 192.168.1.42 right now")
        assert "192.168.1.42" not in result.text
        assert result.matches_by_category[PIICategory.IPV4] == 1

    def test_credit_card_luhn(self) -> None:
        s = PIIScrubber()
        # Real Luhn-valid test number
        valid = "4111 1111 1111 1111"
        # Invalid checksum
        invalid = "4111 1111 1111 1112"
        r1 = s.scrub(f"Card: {valid}")
        r2 = s.scrub(f"Card: {invalid}")
        assert PIICategory.CREDIT_CARD in r1.matches_by_category
        assert PIICategory.CREDIT_CARD not in r2.matches_by_category

    def test_tokenize_mode_round_trip(self) -> None:
        s = PIIScrubber(mode=ScrubMode.TOKENIZE)
        original = "Contact bob@example.com from 10.0.0.1"
        result = s.scrub(original)
        assert "<PII_1>" in result.text
        assert "<PII_2>" in result.text
        restored = result.restore(result.text)
        assert "bob@example.com" in restored
        assert "10.0.0.1" in restored

    def test_disabled_category(self) -> None:
        s = PIIScrubber(disabled_categories={PIICategory.EMAIL})
        result = s.scrub("Send to a@b.com")
        assert "a@b.com" in result.text

    def test_eth_address(self) -> None:
        s = PIIScrubber()
        addr = "0x" + "a" * 40
        result = s.scrub(f"My wallet is {addr}")
        assert addr not in result.text

    def test_empty_input(self) -> None:
        assert PIIScrubber().scrub("").text == ""

    def test_no_match(self) -> None:
        result = PIIScrubber().scrub("Just a normal sentence with no PII.")
        assert result.total_matches == 0


class TestFingerprint:
    def test_engine_generates_realistic(self) -> None:
        fp = FingerprintEngine(seed=42).generate()
        assert "Mozilla/5.0" in fp.user_agent
        assert "Chrome/" in fp.user_agent
        assert fp.viewport[0] >= 1024
        assert fp.locale
        assert "/" in fp.timezone

    def test_deterministic_with_seed(self) -> None:
        a = FingerprintEngine(seed=7).generate()
        b = FingerprintEngine(seed=7).generate()
        assert a == b

    def test_context_options_shape(self) -> None:
        fp = FingerprintEngine(seed=1).generate()
        opts = FingerprintEngine.context_options(fp)
        assert opts["user_agent"] == fp.user_agent
        assert opts["viewport"]["width"] == fp.viewport[0]
        assert opts["locale"] == fp.locale


class TestCrossVerifier:
    def test_multi_domain_boost(self) -> None:
        c = Citation(
            claim="The sky is blue",
            source_urls=["https://a.com/x", "https://b.com/y"],
            confidence=0.7,
        )
        verified = CrossVerifier().verify([c])
        assert verified[0].cross_verified
        assert verified[0].confidence > 0.7

    def test_single_domain_penalty(self) -> None:
        c = Citation(
            claim="Doubt",
            source_urls=["https://a.com/x", "https://a.com/y"],
            confidence=0.7,
        )
        verified = CrossVerifier().verify([c])
        assert not verified[0].cross_verified
        assert verified[0].confidence < 0.7


class TestMarkdownRender:
    def test_renders_basic(self) -> None:
        md = render_markdown(
            query="Test query?",
            summary="A short summary.",
            plan=[
                ResearchStep(index=0, subquery="Sub 1", rationale="r1"),
                ResearchStep(index=1, subquery="Sub 2", rationale="r2", sensitive=True),
            ],
            sources=[
                SourcePage(
                    url="https://example.com",
                    title="Example",
                    content="hello",
                    via_tor=True,
                ),
            ],
            citations=[
                Citation(
                    claim="A claim",
                    source_urls=["https://example.com"],
                    confidence=0.9,
                    cross_verified=True,
                ),
            ],
            privacy_level=PrivacyLevel.MEDIUM,
            duration_seconds=3.7,
        )
        assert "# Research:" in md
        assert "Test query?" in md
        assert "🔒" in md
        assert "via Tor" in md
        assert "Penumbra" in md
