"""Show how Penumbra plugs into an agent loop (LangGraph, CrewAI, custom).

Penumbra is a library — not a framework. You drop the `Researcher` wherever you
need a privacy-aware research tool and the rest of your agent code stays the
same.
"""

from __future__ import annotations

import asyncio

from penumbra import Researcher


async def private_research_tool(query: str, *, paranoid: bool = False) -> str:
    """A reusable tool wrapper you can attach to any agent loop."""
    level = "high" if paranoid else "medium"
    async with Researcher(privacy=level) as r:
        report = await r.run(query)
    return report.markdown


async def main() -> None:
    """Pretend we're an agent that decides when to use the tool."""
    user_question = "Compare open-source RAG frameworks in 2026"
    is_sensitive = False

    print(f"User: {user_question}")
    print(f"Agent: invoking private_research_tool (paranoid={is_sensitive})...")

    result = await private_research_tool(user_question, paranoid=is_sensitive)
    print("\n" + result)


if __name__ == "__main__":
    asyncio.run(main())
