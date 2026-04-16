"""
LLM-authored scenario agents.

The scenario-generation loop is the second compounding agent in Probably Nothing.
Where the mutator proposes new *hook variants*, the proposer proposes new
*test scenarios* — standalone Forge test contracts that probe the hook from
new angles. Both loops feed each other: findings from one inform the other.

Each proposal is wrapped in a strict template (only the test body is free-form)
and compile-gated via `forge build` before it enters the pool. Scenarios that
don't differentiate variants (low variance across the current population) get
deprioritised; ones that find regressions compound.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import pvariance
from typing import Dict, List, Optional

import httpx

from .llm import LLMClient, build_fast_llm
from .persona import PersonaDef


# ─── security context sources ──────────────────────────────────────────────────

_UNISWAP_AI_BASE = "https://raw.githubusercontent.com/Uniswap/uniswap-ai/main"
_UNISWAP_AI_FILES = [
    "packages/plugins/uniswap-hooks/skills/v4-security-foundations/references/vulnerabilities-catalog.md",
    "packages/plugins/uniswap-hooks/skills/v4-security-foundations/references/audit-checklist.md",
]

_ETHSKILLS_AMM_URL = (
    "https://raw.githubusercontent.com/austintgriffith/evm-audit-skills/main"
    "/evm-audit-defi-amm/references/checklist.md"
)

_SECURITY_CTX_CAP_BYTES = 16 * 1024  # cap total injected context at 16 KB

_uniswap_ai_context: Optional[str] = None


def _extract_v4_hooks_section(md: str) -> str:
    """Extract just the Uniswap V4 Hooks sections from a larger checklist."""
    lines = md.splitlines()
    in_section = False
    extracted: List[str] = []
    for line in lines:
        if re.match(r"^##\s+Uniswap V4 Hooks", line):
            in_section = True
        elif in_section and re.match(r"^##\s+", line) and "Uniswap V4" not in line:
            in_section = False
        if in_section:
            extracted.append(line)
    return "\n".join(extracted)


async def _load_uniswap_ai_context() -> str:
    """Fetch security reference docs from uniswap-ai + ethskills AMM checklist. Cached."""
    global _uniswap_ai_context
    if _uniswap_ai_context is not None:
        return _uniswap_ai_context
    parts: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for path in _UNISWAP_AI_FILES:
                url = f"{_UNISWAP_AI_BASE}/{path}"
                r = await client.get(url)
                if r.status_code == 200:
                    parts.append(r.text)
            # Augment with the V4 hooks section from ethskills AMM checklist
            r = await client.get(_ETHSKILLS_AMM_URL)
            if r.status_code == 200:
                v4_section = _extract_v4_hooks_section(r.text)
                if v4_section:
                    parts.append("## EthSkills — Uniswap V4 Hook Vulnerabilities\n\n" + v4_section)
    except Exception:
        pass  # network unavailable — proceed without
    combined = "\n\n".join(parts)[:_SECURITY_CTX_CAP_BYTES]
    _uniswap_ai_context = combined
    return combined


WORKSPACE_IMAGE = os.getenv("PN_FOUNDRY_IMAGE", "probably-nothing-foundry")
COMPILE_TIMEOUT = int(os.getenv("PN_COMPILE_TIMEOUT", "60"))

V4_PRIMER_HEADER = """\
# V4 scenario-author primer

You are authoring a Foundry test contract that probes a Uniswap V4 hook.
The base contract your scenario MUST inherit is shown below — read it carefully.

═══ APPROVED IMPORTS (use ONLY these — no others ever) ═══

```
import {PNBase} from "../base/PNBase.t.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Constants} from "@uniswap/v4-core/test/utils/Constants.sol";
```

Do NOT import anything else — not IPoolManager, not PoolKey, not IHooks, nothing.
Everything you need is already inherited from PNBase.
"""

V4_PRIMER_RULES = """\
═══ HARD RULES (violation = compile failure) ═══

  1. Exactly ONE ```solidity fenced block per scenario. No prose between blocks.
  2. Contract name MUST start with `Scenario_` (e.g. `Scenario_JIT_LP`).
  3. Contract MUST inherit `PNBase`. Do NOT override `setUp()`.
  4. Every test function MUST start with `test_`.
  5. Do NOT redeclare hook, poolKey, currency0, currency1, poolId, poolManager,
     positionManager, swapRouter, FEE, TICK_SPACING, tickLower, tickUpper,
     seedTokenId — these are already in PNBase.
  6. ONLY use imports from the APPROVED list above. No other imports.
  7. Tick arguments to doAddLiquidity MUST be multiples of 60.
  8. doSwap first arg is int256. Use NEGATIVE values for exact-input swaps:
     `doSwap(-1 ether, true)` ✓   `doSwap(1 ether, true)` ✗ (wrong sign)
     Do NOT cast to uint128/uint256 — pass plain int256 literals.
  9. Do NOT call poolManager, positionManager, or swapRouter directly — use
     the helper functions defined in PNBase.
 10. The hook is deployed as `Hook` (renamed from its original name). To call
     hook-specific public functions write: `hook.method()` — no extra import
     needed. Do NOT import the original contract name from src/ — that file is
     now called Hook.sol and is already imported by PNBase.
 11. PREFER testing behavior indirectly via doSwap/doAddLiquidity/sandwich.
     Only call `hook.method()` for functions you can see in the hook source
     shown below. When uncertain, skip hook-specific calls — a working indirect
     test beats a broken direct one.
 12. Do NOT use Solidity reserved words as variable names: `after`, `before`,
     `var`, `let`, `match`, `in`, `of`, `null`, `switch`, `case`, `default`,
     `static`, `typeof`. Use descriptive names like `amountOut`, `delta`,
     `tokenId`, `gasUsed` instead.

═══ WORKING EXAMPLE ═══

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {PNBase} from "../base/PNBase.t.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";

contract Scenario_WorkingExample is PNBase {
    function test_ExactInput_0For1_outputPositive() public {
        BalanceDelta delta = doSwap(-1 ether, true);
        assertLt(int256(delta.amount0()), 0, "spent token0");
        assertGt(int256(delta.amount1()), 0, "received token1");
    }

    function test_AddRemove_NoNetLoss() public {
        uint256 tokenId = doAddLiquidity(-60, 60, 1 ether);
        doRemoveLiquidity(tokenId, 1 ether);
    }

    function test_Sandwich_Survives() public {
        sandwich(-0.5 ether, true, -0.1 ether);
    }

    function test_GasRegression_SmallSwap() public {
        uint256 g = gasleft();
        doSwap(-1000, true);
        assertLt(g - gasleft(), 500_000, "gas too high");
    }
}
```

═══ SCENARIO IDEAS (pick angles the working example does NOT cover) ═══

  Routing edge cases:
  - Large swap through tick boundary: doSwap(-50 ether, true)
  - Sequential swaps in both directions checking symmetry
  - doSwap(-1, true) — minimum viable swap

  LP stress:
  - Narrow range: doAddLiquidity(-60, 60, 10 ether) then doSwap(-5 ether, true)
  - Wide range: doAddLiquidity(-6000, 6000, 1 ether)
  - JIT: doAddLiquidity → doSwap → doRemoveLiquidity in one test

  MEV / ordering:
  - sandwich(-1 ether, true, -5 ether) — aggressive front-run
  - Repeated sandwiches: run sandwich twice and assert state doesn't drift

  Hook data probing:
  - doSwapWithHookData(-1 ether, true, abi.encode(uint256(0)))
  - doSwapWithHookData(-1 ether, true, abi.encode(address(this)))
  - doSwapWithHookData(-1 ether, true, hex"") — empty hookData

  Security probes (from V4 vulnerability catalog):
  - Delta accounting: add liquidity, swap, remove — assert net token balance unchanged
  - Reentrancy surface: rapid sequential swaps checking no state corruption
"""


@dataclass
class Scenario:
    scenario_id: str
    contract_name: str
    filename: str
    source: str
    proposer: str  # "seed" | "llm" | "human" (promoted from vault)
    gen_created: int
    persona_id: str = ""  # which ecosystem persona generated this scenario
    gas_samples: List[int] = field(default_factory=list)
    pass_samples: List[int] = field(default_factory=list)
    fail_samples: List[int] = field(default_factory=list)

    @property
    def informativeness(self) -> float:
        """Variance of gas across variants — higher = more discriminating."""
        if len(self.gas_samples) < 2:
            return float("inf")  # protect newly-born scenarios from early pruning
        return float(pvariance(self.gas_samples))

    @property
    def failure_rate(self) -> float:
        total = sum(self.pass_samples) + sum(self.fail_samples)
        return (sum(self.fail_samples) / total) if total else 0.0


class ScenarioPool:
    """Tracks scenarios on disk (workspace/test/scenarios/) with per-scenario metadata."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.scenarios_dir = self.workspace / "test" / "scenarios"
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)
        self._scenarios: Dict[str, Scenario] = {}

    def register_existing_baseline(self) -> None:
        """Register any .t.sol files shipped with the workspace (Baseline etc.) as seed scenarios."""
        for sol in self.scenarios_dir.glob("*.t.sol"):
            if sol.name.startswith("Scenario_"):
                continue  # generated ones register themselves on add
            name = _extract_contract_name(sol.read_text()) or sol.stem
            sid = f"seed::{sol.stem}"
            if sid in self._scenarios:
                continue
            self._scenarios[sid] = Scenario(
                scenario_id=sid, contract_name=name, filename=sol.name,
                source=sol.read_text(), proposer="seed", gen_created=0,
            )

    def add_human_scenarios(self, items: List[dict]) -> int:
        """Install `author: human` scenarios lifted from a prior vault. Returns count installed."""
        installed = 0
        for item in items:
            src = item.get("source", "")
            name = _extract_contract_name(src) or f"Human_{uuid.uuid4().hex[:8]}"
            filename = f"{name}.t.sol"
            (self.scenarios_dir / filename).write_text(src)
            sid = f"human::{name}"
            self._scenarios[sid] = Scenario(
                scenario_id=sid, contract_name=name, filename=filename,
                source=src, proposer="human", gen_created=0,
            )
            installed += 1
        return installed

    def add(self, scenario: Scenario) -> None:
        path = self.scenarios_dir / scenario.filename
        path.write_text(scenario.source)
        self._scenarios[scenario.scenario_id] = scenario

    def remove(self, scenario_id: str) -> None:
        s = self._scenarios.pop(scenario_id, None)
        if s:
            try:
                (self.scenarios_dir / s.filename).unlink(missing_ok=True)
            except Exception:
                pass

    def all(self) -> List[Scenario]:
        return list(self._scenarios.values())

    def active(self) -> List[Scenario]:
        """Scenarios currently feeding the harness (everything not pruned)."""
        return list(self._scenarios.values())

    def record_result(self, scenario_id: str, gas: int, passed: int, failed: int) -> None:
        s = self._scenarios.get(scenario_id)
        if s:
            s.gas_samples.append(gas)
            s.pass_samples.append(passed)
            s.fail_samples.append(failed)

    def get_by_contract_name(self, contract_name: str) -> Optional["Scenario"]:
        for s in self._scenarios.values():
            if s.contract_name == contract_name:
                return s
        return None

    def prune(self, keep_top_k: int = 64, min_samples: int = 4) -> List[str]:
        """Drop low-informativeness scenarios once they have enough samples. Keeps human scenarios."""
        rankable = [
            s for s in self._scenarios.values()
            if s.proposer != "human" and s.proposer != "seed"
            and len(s.gas_samples) >= min_samples
        ]
        if len(rankable) <= keep_top_k:
            return []
        rankable.sort(key=lambda s: s.informativeness, reverse=True)
        drop = [s for s in rankable[keep_top_k:]]
        for s in drop:
            self.remove(s.scenario_id)
        return [s.scenario_id for s in drop]


_PNBASE_TEMPLATE = Path(__file__).parent.parent / "foundry_workspace" / "test" / "base" / "PNBase.t.sol"


class ScenarioProposer:
    """LLM-backed scenario author."""

    def __init__(self, llm: LLMClient, workspace: Path, pool: ScenarioPool):
        self.llm = llm
        self.fast_llm = build_fast_llm()  # small model for quick fix/repair passes
        self.workspace = Path(workspace)
        self.pool = pool
        self._security_context: Optional[str] = None
        # Load PNBase source so the model sees the exact API, not a summary.
        ws_pnbase = self.workspace / "test" / "base" / "PNBase.t.sol"
        if ws_pnbase.exists():
            self._pnbase_source = ws_pnbase.read_text()
        elif _PNBASE_TEMPLATE.exists():
            self._pnbase_source = _PNBASE_TEMPLATE.read_text()
        else:
            self._pnbase_source = None

    async def _ensure_security_context(self) -> None:
        if self._security_context is None:
            self._security_context = await _load_uniswap_ai_context()

    async def propose_batch(
        self,
        hook_source: str,
        count: int,
        gen: int,
        recent_findings: List[str],
        skill_md: Optional[str] = None,
        timeout: float = 120.0,
    ) -> tuple[List[Scenario], List[str]]:
        """Propose up to `count` new scenarios. Compile-gated with fix-and-retry.

        Returns (accepted, rejection_reasons) so callers can surface failures.
        """
        await self._ensure_security_context()
        accepted: List[Scenario] = []
        rejections: List[str] = []
        failed_examples: List[tuple[str, str]] = []  # (source_snippet, error)

        time_start = asyncio.get_event_loop().time()

        # Ask for scenarios in small sub-batches of 2 so the model can actually
        # complete all of them within the num_predict token budget. A 48GB
        # thinking model generating 20 full Solidity contracts in one shot will
        # exhaust tokens before finishing — 2 at a time is reliable.
        SUB_BATCH = 2
        remaining_count = count
        while remaining_count > 0 and len(accepted) < count:
            elapsed = asyncio.get_event_loop().time() - time_start
            budget = timeout - elapsed - 5.0
            if budget < 15.0:
                break
            sub_count = min(SUB_BATCH, remaining_count)
            time_limit = budget * 0.65  # reserve 35% for fix attempts
            prompt = self._build_prompt(hook_source, sub_count, recent_findings, skill_md, failed_examples)
            raw = await self.llm.complete(prompt, timeout=time_limit)
            if not raw:
                break
            remaining_count -= sub_count

            for source in _split_scenarios(raw):
                name = _extract_contract_name(source)
                if not name:
                    rejections.append("no contract name found")
                    continue
                if name in {s.contract_name for s in self.pool.all()}:
                    rejections.append(f"{name}: duplicate")
                    continue
                ok, reason = await self._compile_gate(name, source)
                if not ok:
                    # Attempt one fix pass with the compiler error as feedback
                    elapsed = asyncio.get_event_loop().time() - time_start
                    fix_budget = timeout - elapsed - 5.0
                    if fix_budget > 15.0:
                        fixed = await self._fix_scenario(name, source, reason, hook_source, fix_budget)
                        if fixed:
                            ok2, reason2 = await self._compile_gate(name, fixed)
                            if ok2:
                                source = fixed
                                ok = True
                            else:
                                reason = reason2
                    if not ok:
                        failed_examples.append((source[:400], reason))
                        rejections.append(f"{name}: {reason}")
                        continue
                scenario = Scenario(
                    scenario_id=f"llm::{name}",
                    contract_name=name,
                    filename=f"{name}.t.sol",
                    source=source,
                    proposer="llm",
                    gen_created=gen,
                )
                self.pool.add(scenario)
                accepted.append(scenario)
        return accepted, rejections

    async def _fix_scenario(
        self, name: str, source: str, error: str, hook_source: str, timeout: float
    ) -> Optional[str]:
        """Ask the fast LLM to fix a scenario that failed to compile.

        Uses a small fast model (qwen2.5:3b, ~5s) instead of the main model
        (~90s) because fix passes need low latency more than deep reasoning.
        The prompt is intentionally minimal to stay within the 3B model's
        context window.
        """
        # Extract only the relevant part of PNBase (function signatures, not full source)
        pnbase_sig_block = ""
        if self._pnbase_source:
            # Pull out just function declarations (lines with "function" keyword)
            sigs = [ln.strip() for ln in self._pnbase_source.splitlines()
                    if "function " in ln and not ln.strip().startswith("//")]
            pnbase_sig_block = "PNBase functions available:\n" + "\n".join(sigs[:30]) + "\n\n"

        # Build error-specific hints
        hints = []
        if "9553" in error or "Invalid type for argument" in error:
            hints.append("HINT: 'Invalid type for argument' — check argument types match signatures exactly. "
                         "doSwap takes int256 (not uint128/uint256). doAddLiquidity ticks are int24.")
        if "7920" in error or "Identifier not found" in error:
            hints.append("HINT: 'Identifier not found' — replace any unknown identifier with a PNBase helper "
                         "or remove the offending line entirely.")
        if "6275" in error or "not found" in error.lower():
            hints.append("HINT: Missing import — remove the import and use PNBase helpers instead.")
        hint_block = "\n".join(hints) + "\n\n" if hints else ""

        prompt = (
            f"Fix this Solidity compiler error. Output ONLY the corrected ```solidity block.\n\n"
            f"Contract name must remain: `{name}`\n"
            f"RULES:\n"
            f"  - APPROVED imports ONLY (remove any others):\n"
            f'      import {{PNBase}} from "../base/PNBase.t.sol";\n'
            f'      import {{BalanceDelta}} from "@uniswap/v4-core/src/types/BalanceDelta.sol";\n'
            f'      import {{TickMath}} from "@uniswap/v4-core/src/libraries/TickMath.sol";\n'
            f'      import {{Constants}} from "@uniswap/v4-core/test/utils/Constants.sol";\n'
            f"  - Use PNBase helpers (EXACT signatures below).\n"
            f"    Never call poolManager/positionManager/swapRouter directly.\n\n"
            f"{pnbase_sig_block}"
            f"{hint_block}"
            f"Compiler error:\n```\n{error}\n```\n\n"
            f"Broken source:\n```solidity\n{source}\n```\n\n"
            f"Fixed source:"
        )
        raw = await self.fast_llm.complete(prompt, timeout=min(timeout, 30.0))
        if not raw:
            return None
        parts = _split_scenarios(raw)
        return parts[0] if parts else None

    async def _compile_gate(self, name: str, source: str) -> tuple[bool, str]:
        """Write the scenario, run `forge build --match-path` against it, roll back on failure."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._compile_gate_sync, name, source)

    def _compile_gate_sync(self, name: str, source: str) -> tuple[bool, str]:
        path = self.pool.scenarios_dir / f"{name}.t.sol"
        path.write_text(source)
        try:
            # Compile every test path that includes this scenario. We deliberately
            # don't `--skip test`: that would short-circuit our gate and let the
            # bad scenario poison subsequent forge test runs.
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{self.workspace}:/workspace",
                "-w", "/workspace",
                WORKSPACE_IMAGE,
                "build",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
            if r.returncode == 0:
                return True, ""
            # Clean up the rejected file so it doesn't poison subsequent builds.
            path.unlink(missing_ok=True)
            err_text = r.stderr or r.stdout or ""
            err_lines = err_text.splitlines()
            # Collect meaningful error lines — skip forge noise, capture actual errors
            _NOISE_STRS = {"Compiler run failed", "nightly build",
                           "Warning: Failed to get git",
                           "PNBase.t.sol",  # noise from warning about PNBase:79
                           "forge-std/"}    # noise from forge-std warnings
            detail_lines = [
                ln.strip() for ln in err_lines
                if ("Error" in ln or "-->" in ln)
                and not any(n in ln for n in _NOISE_STRS)
            ]
            if not detail_lines:
                # Fall back to first non-blank, non-noise line
                detail_lines = [ln.strip() for ln in err_lines
                                 if ln.strip() and not any(n in ln for n in _NOISE_STRS)][:2]
            summary = "; ".join(detail_lines[:4]) if detail_lines else "compile failed"
            return False, summary[:400]
        except Exception as e:
            path.unlink(missing_ok=True)
            return False, f"compile-gate exception: {e}"

    async def propose_for_persona(
        self,
        hook_source: str,
        persona: "PersonaDef",
        count: int,
        recent_findings: List[str],
        skill_md: Optional[str] = None,
        timeout: float = 120.0,
    ) -> tuple[List[Scenario], List[str]]:
        """Propose scenarios from a specific ecosystem persona's perspective.

        Same compile-gate + fix-retry mechanics as propose_batch but the
        prompt is persona-specific and accepted scenarios are tagged with
        the persona's id for coverage matrix attribution.
        """
        await self._ensure_security_context()
        accepted: List[Scenario] = []
        rejections: List[str] = []
        failed_examples: List[tuple[str, str]] = []

        time_start = asyncio.get_event_loop().time()
        SUB_BATCH = 2
        remaining_count = count
        while remaining_count > 0 and len(accepted) < count:
            elapsed = asyncio.get_event_loop().time() - time_start
            budget = timeout - elapsed - 5.0
            if budget < 15.0:
                break
            sub_count = min(SUB_BATCH, remaining_count)
            time_limit = budget * 0.65
            prompt = self._build_persona_prompt(
                hook_source, persona, sub_count, recent_findings, skill_md, failed_examples
            )
            raw = await self.llm.complete(prompt, timeout=time_limit)
            if not raw:
                break
            remaining_count -= sub_count

            for source in _split_scenarios(raw):
                name = _extract_contract_name(source)
                if not name:
                    rejections.append("no contract name found")
                    continue
                if name in {s.contract_name for s in self.pool.all()}:
                    rejections.append(f"{name}: duplicate")
                    continue
                ok, reason = await self._compile_gate(name, source)
                if not ok:
                    elapsed = asyncio.get_event_loop().time() - time_start
                    fix_budget = timeout - elapsed - 5.0
                    if fix_budget > 15.0:
                        fixed = await self._fix_scenario(name, source, reason, hook_source, fix_budget)
                        if fixed:
                            ok2, reason2 = await self._compile_gate(name, fixed)
                            if ok2:
                                source = fixed
                                ok = True
                            else:
                                reason = reason2
                    if not ok:
                        failed_examples.append((source[:400], reason))
                        rejections.append(f"{name}: {reason}")
                        continue
                scenario = Scenario(
                    scenario_id=f"persona-{persona.id}::{name}",
                    contract_name=name,
                    filename=f"{name}.t.sol",
                    source=source,
                    proposer="llm",
                    gen_created=0,
                    persona_id=persona.id,
                )
                self.pool.add(scenario)
                accepted.append(scenario)
        return accepted, rejections

    def _build_persona_prompt(
        self,
        hook_source: str,
        persona: "PersonaDef",
        count: int,
        recent_findings: List[str],
        skill_md: Optional[str],
        failed_examples: Optional[List[tuple]] = None,
    ) -> str:
        existing = sorted({s.contract_name for s in self.pool.all()})
        existing_block = ("Do NOT duplicate these existing scenario contracts:\n  - "
                          + "\n  - ".join(existing)) if existing else ""
        findings_block = "\n".join(f"- {f}" for f in recent_findings[-16:]) or "- (no failures yet — this is the seed round)"
        skill_block = f"<skill>\n{skill_md.strip()}\n</skill>\n\n" if skill_md else ""
        security_block = (
            f"═══ V4 SECURITY REFERENCE ═══\n\n{self._security_context}\n\n"
        ) if self._security_context else ""
        pnbase_block = (
            f"═══ PNBASE CONTRACT (your base — read exact function signatures) ═══\n\n"
            f"```solidity\n{self._pnbase_source}\n```\n\n"
        ) if self._pnbase_source else ""
        failed_block = ""
        if failed_examples:
            lines = ["═══ PREVIOUSLY FAILED PROPOSALS — DO NOT REPEAT THESE PATTERNS ═══\n"]
            for snippet, error in failed_examples[-4:]:
                lines.append(f"Failed source (truncated):\n```solidity\n{snippet}\n```\nError: {error}\n")
            failed_block = "\n".join(lines) + "\n"
        angles_block = "\n".join(f"  - {a}" for a in persona.scenario_angles)

        return (
            f"{V4_PRIMER_HEADER}\n\n"
            f"{pnbase_block}"
            f"{V4_PRIMER_RULES}\n\n"
            f"{security_block}"
            f"{skill_block}"
            f"═══ PERSONA: {persona.label} ═══\n\n"
            f"You are acting as {persona.description}\n\n"
            f"Suggested angles for this persona:\n{angles_block}\n\n"
            f"═══ HOOK UNDER TEST (src/Hook.sol) ═══\n\n"
            f"```solidity\n{hook_source}\n```\n\n"
            f"═══ RECENT FAILURES FOR THIS PERSONA ═══\n{findings_block}\n\n"
            f"{failed_block}"
            f"{existing_block}\n\n"
            f"═══ YOUR TASK ═══\n\n"
            f"Propose {count} NEW test scenarios from the perspective of: {persona.label}.\n"
            f"Each scenario MUST directly reflect how {persona.id} would interact with this hook.\n"
            f"Each must:\n"
            f"  - Be a COMPLETE Solidity contract in its own ```solidity fenced block\n"
            f"  - Start with `Scenario_` and be unique\n"
            f"  - Inherit PNBase, use ONLY APPROVED imports, no setUp() override\n"
            f"  - Call only functions defined in PNBase above — no direct pool/position/swap calls\n"
            f"  - NEVER import anything outside the approved list\n\n"
            f"Output {count} ```solidity blocks, nothing else."
        )

    def _build_prompt(
        self,
        hook_source: str,
        count: int,
        recent_findings: List[str],
        skill_md: Optional[str],
        failed_examples: Optional[List[tuple]] = None,
    ) -> str:
        existing = sorted({s.contract_name for s in self.pool.all()})
        existing_block = ("Do NOT duplicate these existing scenario contracts:\n  - "
                          + "\n  - ".join(existing)) if existing else ""
        findings_block = "\n".join(f"- {f}" for f in recent_findings[-24:]) or "- (no findings yet)"
        skill_block = f"<skill>\n{skill_md.strip()}\n</skill>\n\n" if skill_md else ""
        security_block = (
            f"═══ V4 SECURITY REFERENCE (from Uniswap official docs) ═══\n\n"
            f"{self._security_context}\n\n"
        ) if self._security_context else ""
        pnbase_block = (
            f"═══ PNBASE CONTRACT (your base — read exact function signatures) ═══\n\n"
            f"```solidity\n{self._pnbase_source}\n```\n\n"
        ) if self._pnbase_source else ""
        failed_block = ""
        if failed_examples:
            lines = ["═══ PREVIOUSLY FAILED PROPOSALS — DO NOT REPEAT THESE PATTERNS ═══\n"]
            for snippet, error in failed_examples[-4:]:
                lines.append(f"Failed source (truncated):\n```solidity\n{snippet}\n```\nError: {error}\n")
            failed_block = "\n".join(lines) + "\n"

        return (
            f"{V4_PRIMER_HEADER}\n\n"
            f"{pnbase_block}"
            f"{V4_PRIMER_RULES}\n\n"
            f"{security_block}"
            f"{skill_block}"
            f"═══ HOOK UNDER TEST (src/Hook.sol) ═══\n\n"
            f"```solidity\n{hook_source}\n```\n\n"
            f"═══ RECENT FINDINGS ═══\n{findings_block}\n\n"
            f"{failed_block}"
            f"{existing_block}\n\n"
            f"═══ YOUR TASK ═══\n\n"
            f"Propose {count} NEW test scenarios. Each must:\n"
            f"  - Be a COMPLETE Solidity contract in its own ```solidity fenced block\n"
            f"  - Start with `Scenario_` and be unique\n"
            f"  - Inherit PNBase, use ONLY APPROVED imports, no setUp() override\n"
            f"  - Call only functions defined in PNBase above — no direct pool/position/swap calls\n"
            f"  - Probe vulnerability patterns from the V4 security reference\n"
            f"  - NEVER import anything outside the approved list\n\n"
            f"Output {count} ```solidity blocks, nothing else."
        )


# ─── helpers ───────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:solidity|sol)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_CONTRACT_NAME = re.compile(r"\bcontract\s+(Scenario_[A-Za-z0-9_]+|[A-Z][A-Za-z0-9_]*)\b")


def _split_scenarios(raw: str) -> List[str]:
    return [m.group(1).strip() for m in _FENCE.finditer(raw) if "contract" in m.group(1)]


def _extract_contract_name(source: str) -> Optional[str]:
    m = _CONTRACT_NAME.search(source)
    return m.group(1) if m else None
