"""
ReACT-style ecosystem coverage report synthesizer.

Runs a two-step LLM pass over the completed swarm run:
  1. Plan  — assess which personas surfaced real failures vs. false negatives,
             and what the failures indicate about the hook's readiness.
  2. Write — synthesize a structured ecosystem coverage narrative.

Falls back gracefully if the LLM is unavailable — vault exports without it.
"""
from __future__ import annotations

import re
from typing import List, Optional, Dict, Any

from .llm import LLMClient
from .persona import PersonaDef


_PLAN_PROMPT = """\
You are a senior Uniswap V4 hook reviewer. A swarm of ecosystem persona agents \
has just run test scenarios against a hook and produced a coverage matrix. \
Your task is to PLAN a coverage report by assessing what the results mean for \
real-world hook deployment readiness.

Respond with a JSON object only, no prose:
{{
  "hook_summary": "one sentence describing what this hook does",
  "overall_readiness": "PRODUCTION_READY | NEEDS_FIXES | PROOF_OF_CONCEPT | UNSAFE",
  "critical_personas": ["persona_id", ...],
  "safe_personas": ["persona_id", ...],
  "top_failures": [{{"persona": "...", "test": "...", "implication": "..."}}],
  "top_passes": [{{"persona": "...", "implication": "why this matters"}}],
  "gaps": ["what important scenario types were not covered"],
  "priority_fixes": ["concise actionable fix #1", "fix #2", "fix #3"]
}}

=== HOOK SOURCE ===
```solidity
{hook_source}
```

=== COVERAGE MATRIX ===
{coverage_block}

=== FAILURE DETAILS ===
{failures_block}
"""

_WRITE_PROMPT = """\
You are writing the final ecosystem coverage report for a Uniswap V4 hook. \
This report will be read by the hook's developer to understand how their hook \
performs across the real-world ecosystem it will operate in. \
Be direct and actionable — no filler language.

=== COVERAGE PLAN ===
{plan_json}

Write the report in this exact structure:

# Ecosystem Coverage Report — {hook_name}

## Executive Summary
[2-3 sentences: what the hook does, overall readiness, single most critical finding]

## Coverage Matrix
[reproduce the table from the plan — which personas pass, which fail, what the pass rate means]

## Critical Failures
[for each failing persona: what broke, what a real user would experience, concrete fix]

## What's Working
[for passing personas: what this tells us about the hook's strengths]

## Gaps & Blind Spots
[which real-world scenarios weren't covered, why they matter]

## Priority Fixes
[3-5 ordered action items from most to least urgent]
"""


class ReACTReporter:
    """
    Two-step LLM synthesizer: plan then write.
    Falls back to None if LLM is unavailable — vault exports without narrative.
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def generate(
        self,
        hook_name: str,
        hook_source: str,
        coverage: Dict[str, Any],
        personas: List[PersonaDef],
        timeout: float = 120.0,
        # Legacy params kept for compat — ignored
        best_source: str = "",
        findings: Optional[List] = None,
        scenarios: Optional[List] = None,
        vuln_catalog: str = "",
    ) -> Optional[str]:

        coverage_lines = []
        failures_lines = []
        for persona in personas:
            data = coverage.get(persona.id, {})
            total = data.get("total", 0)
            if total == 0:
                coverage_lines.append(f"- {persona.label}: no scenarios run")
                continue
            passed = data.get("passed", 0)
            rate = data.get("pass_rate", 0.0)
            coverage_lines.append(
                f"- {persona.label}: {passed}/{total} passed ({rate:.1%})"
            )
            for failure in data.get("failures", [])[:5]:
                failures_lines.append(
                    f"  [{persona.label}] {failure.get('text', failure)}"
                )

        coverage_block = "\n".join(coverage_lines) or "- (no scenarios run)"
        failures_block = "\n".join(failures_lines) or "- (no failures)"

        # Step 1: Plan
        plan_prompt = _PLAN_PROMPT.format(
            hook_source=hook_source[:3000],
            coverage_block=coverage_block,
            failures_block=failures_block,
        )
        plan_raw = await self.llm.complete(plan_prompt, timeout=timeout / 2)
        if not plan_raw:
            return None

        plan_json = _extract_json(plan_raw)
        if not plan_json:
            return None

        # Step 2: Write
        write_prompt = _WRITE_PROMPT.format(
            plan_json=plan_json[:3000],
            hook_name=hook_name,
        )
        report = await self.llm.complete(write_prompt, timeout=timeout / 2)
        return report.strip() if report else None


def _extract_json(raw: str) -> Optional[str]:
    """Pull JSON from a response that may be wrapped in markdown fences."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return m.group(0)
    return None
