# Changelog

All notable changes to Penumbra are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Penumbra adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — Initial release

### Added

**Privacy primitives**
- `TorController` — manages a local Tor process via `stem`, with on-demand
  circuit rotation and SOCKS5 proxy URL exposure.
- `PrivateBrowser` — Playwright headless wrapper with per-source
  `BrowserContext` isolation and optional SOCKS5 routing.
- `FingerprintEngine` — randomized realistic user-agent / viewport / locale /
  timezone, with stealth JS against `navigator.webdriver` and WebRTC IP leaks.
- `PIIScrubber` — 13 categories (email, IPv4/6, phone, SSN, Luhn-validated
  cards, MAC, IBAN, BTC/ETH, JWT, API keys, basic-auth URLs) with
  redact and reversible-tokenize modes.

**Research engine**
- `ResearchPlanner` — LLM-driven subquery generator with `sensitive` labels.
- DuckDuckGo HTML search with tracking-redirect unwrapping.
- `ContentExtractor` — `trafilatura` primary, `selectolax` fallback.
- `CitationBuilder` — claim → source URL extraction with confidence scoring.
- `CrossVerifier` — multi-domain boost, single-domain penalty.

**LLM layer**
- `LLMRouter` — sensitivity-based provider routing.
- Providers: `AnthropicLLM` (default `claude-sonnet-4-6`), `OpenAILLM`
  (default `gpt-4o`), `OllamaLLM` (default `qwen2.5:7b`).

**Interfaces**
- `Researcher` — async context manager, four privacy levels (off/low/medium/high).
- CLI: `penumbra "query" --privacy high --output report.md`.
- Output: Markdown and structured JSON reports.

**Tests**
- 19 deterministic smoke tests (no network, no LLM dependency).

[Unreleased]: https://github.com/Brankss/Penumbra/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Brankss/Penumbra/releases/tag/v0.1.0
