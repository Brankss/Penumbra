# Changelog

All notable changes to Penumbra are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Penumbra adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Browser-based DuckDuckGo and Bing search** as the default backend.
  Penumbra now runs SERPs through its own headless Chromium, which sidesteps
  the rate-limiting and Cloudflare challenges that block lightweight scrapers.
  Works out of the box, no API key required.
- **Brave Search API** backend — opt-in via `BRAVE_API_KEY`. Faster than the
  browser path when configured.
- **Tavily Search API** backend — opt-in via `TAVILY_API_KEY`.
- Defensive 200-char truncation on search queries.

### Changed
- Planner prompt now demands keyword-style subqueries (3-8 words, no question
  marks). Verbose multi-clause questions returned zero results.
- New search cascade: Brave (opt-in) → Tavily (opt-in) → DuckDuckGo (browser)
  → Bing (browser) → SearXNG (browser, last resort).

### Removed
- httpx-only SearXNG and DuckDuckGo-Lite backends. They were almost always
  blocked in practice (Cloudflare, 429, 202 bot challenges). The browser path
  replaces them with something that actually works.

## [0.1.1] — CLI fix

### Fixed
- `penumbra "query"` was being interpreted by Typer as a subcommand name
  because the CLI registered two `@app.command()` handlers (`research` and
  `version`). Removed the `version` subcommand; the version is now exposed
  via the `--version` / `-V` flag on the main command.

### Changed
- **Breaking (CLI):** `penumbra version` is no longer a valid command. Use
  `penumbra --version` instead.

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
  (default `gpt-4o`), `OllamaLLM` (default `batiai/gemma4-e2b:q4`).

**Interfaces**
- `Researcher` — async context manager, four privacy levels (off/low/medium/high).
- CLI: `penumbra "query" --privacy high --output report.md`.
- Output: Markdown and structured JSON reports.

**Tests**
- 19 deterministic smoke tests (no network, no LLM dependency).

[Unreleased]: https://github.com/Brankss/Penumbra/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Brankss/Penumbra/releases/tag/v0.1.1
[0.1.0]: https://github.com/Brankss/Penumbra/releases/tag/v0.1.0
