"""Research planner — break a user question into investigable subqueries.

The planner is intentionally LLM-driven (not heuristic). Heuristic planners
produce subqueries that look reasonable but miss the angles a curious human
would chase. We trade some predictability for better coverage.

What we DON'T do: ask the LLM to decide privacy policy. Sensitivity is a
content judgement (does this query name a real person, expose intent, etc.)
and we ask the LLM to label, but the policy of what to do with the label is
made by the router. This separation keeps the LLM honest.
"""

from __future__ import annotations

import logging
from typing import Any

from penumbra.exceptions import ResearchError
from penumbra.llm.base import LLMRouter
from penumbra.types import ResearchStep

logger = logging.getLogger(__name__)


_PLANNER_SYSTEM_PROMPT = """You are a research planning agent. Given a user's
question, you break it into 3-6 focused subqueries that, taken together, would
let a careful researcher write a thorough answer.

Each subquery should:
- be self-contained (a human could search the web for it without context)
- target a distinct angle (avoid near-duplicates)
- prefer concrete, factual angles over vague exploratory ones
- be in the same language as the original question

You also label each subquery as `sensitive: true` if answering it could reveal
something the user might want to keep private (their identity, intent, medical
condition, financial situation, ongoing legal matter, real names of people they
know, specific IP/server they own). Default to `false`.

Respond with a JSON object of the form:
{
  "subqueries": [
    {"subquery": "...", "rationale": "why this angle matters", "sensitive": false},
    ...
  ]
}
"""


class ResearchPlanner:
    """Plan a research run via an LLM call."""

    def __init__(self, *, router: LLMRouter, max_steps: int = 6) -> None:
        self._router = router
        self._max_steps = max_steps

    async def plan(self, query: str) -> list[ResearchStep]:
        if not query.strip():
            raise ResearchError("Empty query — nothing to plan.")

        user_prompt = (
            f"User question:\n{query.strip()}\n\n"
            f"Produce up to {self._max_steps} subqueries."
        )
        data = await self._router.complete_json(
            user_prompt,
            sensitive=False,
            system=_PLANNER_SYSTEM_PROMPT,
            max_tokens=1500,
            temperature=0.2,
        )
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> list[ResearchStep]:
        raw = data.get("subqueries")
        if not isinstance(raw, list) or not raw:
            raise ResearchError(f"Planner returned no subqueries: {data!r}")

        steps: list[ResearchStep] = []
        for idx, item in enumerate(raw[: self._max_steps]):
            if not isinstance(item, dict):
                continue
            subquery = str(item.get("subquery", "")).strip()
            if not subquery:
                continue
            steps.append(
                ResearchStep(
                    index=idx,
                    subquery=subquery,
                    rationale=str(item.get("rationale", "")).strip(),
                    sensitive=bool(item.get("sensitive", False)),
                )
            )

        if not steps:
            raise ResearchError("Planner returned malformed subqueries.")
        logger.debug(
            "Plan: %d steps (%d sensitive)",
            len(steps),
            sum(1 for s in steps if s.sensitive),
        )
        return steps
