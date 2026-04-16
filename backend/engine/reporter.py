"""
ReACT-style security report synthesizer.

Runs a two-step LLM pass over the completed audit:
  1. Plan  — given findings + vuln catalog, decide which vulnerability
             classes are relevant and what evidence exists for each.
  2. Write — synthesize a structured security narrative with cited evidence.

Both steps use the local Ollama instance. Falls back gracefully if the LLM
is unavailable or times out — the vault still exports without the narrative.
"""
from __future__ import annotations

from typing import List, Optional, Dict, Any

from .llm import LLMClient


_PLAN_PROMPT = """\
You are a senior smart contract security auditor reviewing a Uniswap V4 hook.

Below is the vulnerability reference catalog and the audit findings from an \
automated research loop. Your task is to PLAN a security report by identifying \
which vulnerability classes from the catalog are:
  A) Confirmed present (evidence in findings)
  B) Not present / mitigated (evidence of absence)
  C) Unable to determine (insufficient coverage)

Respond with a JSON object only, no prose:
{{
  "hook_summary": "one sentence describing what this hook does",
  "risk_level": "CRITICAL | HIGH | MEDIUM | LOW | INFORMATIONAL",
  "confirmed": [{{"class": "...", "evidence": "..."}}],
  "not_present": [{{"class": "...", "reason": "..."}}],
  "undetermined": [{{"class": "...", "gap": "..."}}],
  "key_gas_finding": "...",
  "key_mev_finding": "..."
}}

=== VULNERABILITY CATALOG ===
{vuln_catalog}

=== HOOK SOURCE ===
```solidity
{hook_source}
```

=== AUDIT FINDINGS ({finding_count} total) ===
{findings_block}

=== SCENARIO RESULTS ===
{scenario_block}
"""

_WRITE_PROMPT = """\
You are writing the final security report for a Uniswap V4 hook audit.
Use the structured plan below and write a clear, concise markdown report.
Cite specific findings as evidence. Be direct — no filler language.

=== AUDIT PLAN ===
{plan_json}

=== BEST VARIANT (LLM-proposed improvement) ===
{best_variant_note}

Write the report in this exact structure:
# Security Report — {hook_name}

## Executive Summary
[2-3 sentences: what the hook does, overall risk level, single most important finding]

## Vulnerability Assessment

### Confirmed Issues
[for each confirmed vulnerability: name, severity, evidence, recommendation]

### Not Present / Mitigated
[brief list with reason]

### Coverage Gaps
[what scenarios couldn't be tested and why it matters]

## Gas Analysis
[key gas finding, comparison to baseline if LLM variant improved it]

## MEV Resistance
[sandwich survival result, any ordering vulnerabilities]

## Recommendations
[3-5 concrete, actionable items, prioritised by severity]
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
        best_source: str,
        findings: List[Dict[str, Any]],
        scenarios: List[Any],
        vuln_catalog: str,
        timeout: float = 120.0,
    ) -> Optional[str]:
        if not vuln_catalog:
            return None

        finding_texts = [
            f.get("text", str(f)) if isinstance(f, dict) else str(f)
            for f in findings
        ]
        findings_block = "\n".join(f"- {t}" for t in finding_texts[:40]) or "- (no findings)"

        scenario_block = "\n".join(
            f"- {s.contract_name}: {len(s.pass_samples)} pass samples, "
            f"{len(s.fail_samples)} fail samples, "
            f"failure_rate={s.failure_rate:.1%}"
            for s in scenarios
        ) if scenarios else "- (no scenarios run)"

        # Step 1: Plan
        plan_prompt = _PLAN_PROMPT.format(
            vuln_catalog=vuln_catalog[:6000],  # cap catalog to keep prompt size sane
            hook_source=hook_source[:4000],
            finding_count=len(findings),
            findings_block=findings_block,
            scenario_block=scenario_block,
        )
        plan_raw = await self.llm.complete(plan_prompt, timeout=timeout / 2)
        if not plan_raw:
            return None

        # Extract JSON from plan (model may wrap it in fences)
        plan_json = _extract_json(plan_raw)
        if not plan_json:
            return None

        # Step 2: Write
        source_changed = hook_source.strip() != best_source.strip()
        best_variant_note = (
            f"The LLM mutation tier produced an improved variant "
            f"({len(best_source.splitlines())} lines vs {len(hook_source.splitlines())} original). "
            f"Differences reflect structural optimisations."
            if source_changed
            else "No improvement over original — best variant is the unmodified hook."
        )

        write_prompt = _WRITE_PROMPT.format(
            plan_json=plan_json[:3000],
            best_variant_note=best_variant_note,
            hook_name=hook_name,
        )
        report = await self.llm.complete(write_prompt, timeout=timeout / 2)
        return report.strip() if report else None


def _extract_json(raw: str) -> Optional[str]:
    """Pull JSON from a response that may be wrapped in markdown fences."""
    import re
    # Try fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return m.group(1)
    # Try bare JSON object
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return m.group(0)
    return None
