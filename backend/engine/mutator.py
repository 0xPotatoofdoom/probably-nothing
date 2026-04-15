"""Parametric → structural → LLM-assisted mutation engine."""
import re, random, copy
from typing import List, Dict, Any, Optional

from .llm import LLMClient


_SOLIDITY_FENCE = re.compile(r"```(?:solidity|sol)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_FEE_TIERS = [100, 500, 3000, 10000]

# Archetype-aware bias: each agent family perturbs the mutation space differently,
# so 100 agents don't all explore the same point.
_ARCHETYPE_BIAS = {
    "gas-optimizer":    {"int_scale": (0.5, 1.0),  "prefer_tier": "low"},
    "mev-sentinel":     {"int_scale": (0.8, 1.4),  "prefer_tier": "any"},
    "lp-deployer":      {"int_scale": (0.9, 1.3),  "prefer_tier": "high"},
    "swap-scenario":    {"int_scale": (0.7, 1.3),  "prefer_tier": "any"},
    "edge-case-hunter": {"int_scale": (0.1, 2.5),  "prefer_tier": "any"},
    "security-auditor": {"int_scale": (0.9, 1.1),  "prefer_tier": "low"},
}


class HookMutator:
    def extract_params(self, source: str) -> Dict[str, Any]:
        """Extract mutable parameters via regex (fee tiers, uint/int literals, booleans)."""
        params: Dict[str, Any] = {}
        fee_matches = re.findall(r'\b(100|500|3000|10000)\b', source)
        if fee_matches:
            params["fee_tier"] = int(fee_matches[0])
        for m in re.finditer(r'(?:uint\d*|int\d*)\s+(?:constant\s+|immutable\s+|public\s+|private\s+|internal\s+)*(\w+)\s*=\s*(\d+)', source):
            params[m.group(1)] = int(m.group(2))
        for m in re.finditer(r'bool\s+(?:public\s+|private\s+|internal\s+|constant\s+)*(\w+)\s*=\s*(true|false)', source):
            params[m.group(1)] = m.group(2) == "true"
        return params

    def parametric_variants(
        self,
        source: str,
        params: Dict,
        count: int = 6,
        agents: Optional[List[dict]] = None,
        seed: Optional[int] = None,
    ) -> List[str]:
        """
        Generate N distinct variants by perturbing extracted params.

        When `agents` is provided, variant i is biased toward the archetype of agent i —
        this spreads exploration across the param space instead of everyone sampling
        the same distribution.

        Distinctness is guaranteed: if the parametric space collapses (e.g. no params
        extracted, or the LLM's output has nothing numeric to mutate), each variant
        still gets a unique `// variant-{i}` marker so the harness scores them
        independently.
        """
        rng = random.Random(seed) if seed is not None else random
        variants: List[str] = []
        for i in range(count):
            archetype = self._archetype_for(agents, i) if agents else None
            bias = _ARCHETYPE_BIAS.get(archetype or "", {"int_scale": (0.8, 1.2), "prefer_tier": "any"})
            variant = self._mutate(source, params, bias, rng)
            # Guarantee distinctness so the harness cannot collapse to a single point.
            if variant in variants or variant == source:
                variant = self._tag_variant(variant, i, archetype)
            variants.append(variant)
        return variants

    @staticmethod
    def _archetype_for(agents: List[dict], i: int) -> Optional[str]:
        if not agents:
            return None
        a = agents[i % len(agents)]
        aid = a.get("id", "")
        return aid.rsplit("-", 1)[0] if aid[-1:].isdigit() else aid

    @staticmethod
    def _mutate(source: str, params: Dict, bias: Dict, rng) -> str:
        variant = source
        for key, val in params.items():
            if key == "fee_tier" and isinstance(val, int):
                pref = bias.get("prefer_tier", "any")
                pool = [f for f in _FEE_TIERS if f != val]
                if pref == "low":
                    pool = [f for f in pool if f <= val] or pool
                elif pref == "high":
                    pool = [f for f in pool if f >= val] or pool
                new_fee = rng.choice(pool)
                variant = re.sub(r'\b' + str(val) + r'\b', str(new_fee), variant, count=1)
            elif isinstance(val, bool):
                variant = variant.replace(
                    f"= {'true' if val else 'false'}",
                    f"= {'false' if val else 'true'}", 1,
                )
            elif isinstance(val, int):
                lo, hi = bias.get("int_scale", (0.8, 1.2))
                scale = rng.uniform(lo, hi)
                new_val = max(0, int(val * scale))
                # Avoid a no-op when scale rounds back to val.
                if new_val == val:
                    new_val = val + rng.choice([-1, 1]) * max(1, val // 10)
                    new_val = max(0, new_val)
                variant = variant.replace(f"= {val}", f"= {new_val}", 1)
        return variant

    @staticmethod
    def _tag_variant(source: str, i: int, archetype: Optional[str]) -> str:
        tag = f"// variant-{i}" + (f" · {archetype}" if archetype else "")
        # Insert after the pragma line if present, else prepend.
        lines = source.splitlines()
        for idx, line in enumerate(lines):
            if line.strip().startswith("pragma"):
                lines.insert(idx + 1, tag)
                return "\n".join(lines)
        return tag + "\n" + source


class LLMMutator:
    """
    Structural mutations proposed by an LLM. Invoked when the parametric tier
    plateaus. Prompt includes best source + last 12 findings + optional skill.md
    research seed. Output is a single ```solidity-fenced variant, sanity-checked
    for basic Solidity shape before use.
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def propose(
        self,
        best_source: str,
        recent_findings: List[str],
        skill_md: Optional[str] = None,
        timeout: float = 60.0,
    ) -> Optional[str]:
        prompt = self._build_prompt(best_source, recent_findings[-12:], skill_md)
        raw = await self.llm.complete(prompt, timeout=timeout)
        if not raw:
            return None
        variant = self._extract_solidity(raw)
        if not variant or not self._looks_like_solidity(variant):
            return None
        return variant

    def _build_prompt(
        self,
        best_source: str,
        recent_findings: List[str],
        skill_md: Optional[str],
    ) -> str:
        findings_block = "\n".join(f"- {f}" for f in recent_findings) or "- (none yet)"
        skill_block = f"<skill>\n{skill_md.strip()}\n</skill>\n\n" if skill_md else ""
        return (
            "You are an expert Uniswap V4 hook engineer. Propose ONE structural "
            "variant of the hook below that could score higher on the composite "
            "metric (40% gas, 30% MEV resistance, 20% LP quality, 10% simplicity).\n\n"
            f"{skill_block}"
            f"Recent findings from the autoresearch loop:\n{findings_block}\n\n"
            "Rules:\n"
            "- Output MUST be a single complete Solidity file in one ```solidity fenced block.\n"
            "- Preserve the original SPDX header and pragma if present.\n"
            "- Preserve the public interface (hook permission flags, exposed functions).\n"
            "- Prefer small, surgical structural changes over rewrites.\n"
            "- No commentary outside the fence.\n\n"
            "Current best variant:\n"
            "```solidity\n"
            f"{best_source}\n"
            "```\n"
        )

    @staticmethod
    def _extract_solidity(raw: str) -> Optional[str]:
        m = _SOLIDITY_FENCE.search(raw)
        if m:
            return m.group(1).strip()
        # Fall back to whole payload if the model forgot fences but clearly returned code.
        if "pragma solidity" in raw or "contract " in raw:
            return raw.strip()
        return None

    @staticmethod
    def _looks_like_solidity(src: str) -> bool:
        if len(src) < 40:
            return False
        has_pragma = "pragma solidity" in src
        has_contract = re.search(r"\b(contract|library|abstract contract)\b", src) is not None
        balanced_braces = src.count("{") == src.count("}")
        return (has_pragma or has_contract) and balanced_braces
