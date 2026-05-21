"""High-privacy run — Tor + local LLM for sensitive subqueries.

Requires:
- A locally-installed Tor (e.g. `choco install tor` on Windows)
- Ollama running locally (`ollama serve`) with a model pulled (`ollama pull qwen2.5:7b`)
- Optionally ANTHROPIC_API_KEY or OPENAI_API_KEY for non-sensitive subqueries
"""

from __future__ import annotations

import asyncio
import logging

from penumbra import Researcher


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async with Researcher(privacy="high") as researcher:
        report = await researcher.run(
            "Recent papers on private LLM inference and how they compare"
        )

    print(f"\n{'=' * 60}")
    print(f"Privacy level : {report.privacy_level.name}")
    print(f"Sources       : {len(report.sources)}")
    print(f"Citations     : {len(report.citations)}")
    print(f"Via Tor       : {report.metadata.get('via_tor')}")
    print(f"Duration      : {report.duration_seconds:.1f}s")
    print(f"{'=' * 60}\n")

    report.save("high_privacy_output.md")
    report.save_json("high_privacy_output.json")


if __name__ == "__main__":
    asyncio.run(main())
