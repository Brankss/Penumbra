"""Minimal Penumbra example — medium privacy, cloud LLM, no Tor required."""

from __future__ import annotations

import asyncio

from penumbra import Researcher


async def main() -> None:
    async with Researcher(privacy="low") as researcher:
        report = await researcher.run(
            "What are the most starred open-source AI agent frameworks in 2026?"
        )
    print(report.markdown)
    report.save("basic_research_output.md")


if __name__ == "__main__":
    asyncio.run(main())
