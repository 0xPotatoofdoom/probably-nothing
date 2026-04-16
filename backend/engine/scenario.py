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

from .llm import LLMClient


# ─── uniswap-ai security context ───────────────────────────────────────────────

_UNISWAP_AI_BASE = "https://raw.githubusercontent.com/Uniswap/uniswap-ai/main"
_UNISWAP_AI_FILES = [
    "packages/plugins/uniswap-hooks/skills/v4-security-foundations/references/vulnerabilities-catalog.md",
    "packages/plugins/uniswap-hooks/skills/v4-security-foundations/references/audit-checklist.md",
]
_UNISWAP_AI_CAP_BYTES = 12 * 1024  # cap total injected context at 12 KB

_uniswap_ai_context: Optional[str] = None


async def _load_uniswap_ai_context() -> str:
    """Fetch security reference docs from uniswap-ai. Cached after first call."""
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
    except Exception:
        pass  # network unavailable — proceed without
    combined = "\n\n".join(parts)[:_UNISWAP_AI_CAP_BYTES]
    _uniswap_ai_context = combined
    return combined


WORKSPACE_IMAGE = os.getenv("PN_FOUNDRY_IMAGE", "probably-nothing-foundry")
COMPILE_TIMEOUT = int(os.getenv("PN_COMPILE_TIMEOUT", "60"))

V4_PRIMER = """\
# V4 scenario-author primer

You are authoring a Foundry test contract that probes a Uniswap V4 hook.
Study the WORKING EXAMPLE below and follow its structure exactly.

═══ WORKING EXAMPLE (copy this structure) ═══

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {PNBase} from "../base/PNBase.t.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";

contract Scenario_WorkingExample is PNBase {
    // test a swap and assert direction is correct
    function test_ExactInput_0For1_outputPositive() public {
        BalanceDelta delta = doSwap(-1 ether, true);
        assertLt(int256(delta.amount0()), 0, "spent token0");
        assertGt(int256(delta.amount1()), 0, "received token1");
    }

    // test liquidity round-trip produces no net loss
    function test_AddRemove_NoNetLoss() public {
        uint256 tokenId = doAddLiquidity(-60, 60, 1 ether);
        doRemoveLiquidity(tokenId, 1 ether);
    }

    // test hook survives a basic sandwich
    function test_Sandwich_Survives() public {
        sandwich(-0.5 ether, true, -0.1 ether);
    }

    // test near-zero swap does not revert
    function test_TinySwap_NoRevert() public {
        doSwap(-1000, true);
    }
}
```

═══ APPROVED IMPORTS (use ONLY these — no others) ═══

```
import {PNBase} from "../base/PNBase.t.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Constants} from "@uniswap/v4-core/test/utils/Constants.sol";
```

Do NOT import anything else. PNBase already re-exports: hook, poolKey, currency0,
currency1, poolManager, positionManager, swapRouter, TICK_SPACING, FEE.

═══ AVAILABLE HELPERS (from PNBase) ═══

  doSwap(int256 amount, bool zeroForOne) returns (BalanceDelta)
    — negative amount = exact-input, positive = exact-output
    — zeroForOne=true means selling token0 to buy token1

  doSwapWithHookData(int256 amount, bool zeroForOne, bytes memory hookData) returns (BalanceDelta)

  doAddLiquidity(int24 lower, int24 upper, uint128 liquidity) returns (uint256 tokenId)
    — tick values must be multiples of TICK_SPACING (60)
    — e.g. lower=-120, upper=120  or  lower=-600, upper=600

  doRemoveLiquidity(uint256 tokenId, uint128 liquidity)

  sandwich(int256 victimAmount, bool zeroForOne, int256 attackerAmount)
    — runs: front-run, victim swap, back-run in one call

═══ HARD RULES ═══

  1. Exactly ONE ```solidity fenced block per scenario. No prose between blocks.
  2. Contract name MUST start with `Scenario_` (e.g. `Scenario_JIT_LP`).
  3. Contract MUST inherit `PNBase`. Do NOT override `setUp()`.
  4. Every test function MUST start with `test_`.
  5. Do NOT redeclare hook, poolKey, currency0, currency1, or any PNBase variable.
  6. ONLY use imports from the APPROVED list above.
  7. Tick arguments to doAddLiquidity must be multiples of 60.

═══ SCENARIO IDEAS (pick angles the working example does NOT cover) ═══

  Routing edge cases:
  - Swap that would move price across a tick boundary (large amount, e.g. 50 ether)
  - Exact-output swap (positive amountSpecified, e.g. doSwap(1 ether, true))
  - Sequential swaps in opposite directions checking price impact symmetry

  LP stress:
  - Narrow range: doAddLiquidity(-60, 60, 10 ether) then swap through it
  - Wide range: doAddLiquidity(-6000, 6000, 1 ether)
  - JIT: add liquidity, swap, remove liquidity in one test — measure fee capture

  MEV / ordering:
  - Multi-swap sandwich with larger front-run
  - Repeated sandwiches checking hook state monotonicity

  Hook behaviour:
  - doSwapWithHookData passing abi.encode(uint256(0)) or abi.encode(address(this))
  - vm.expectRevert() if the hook is known to gate certain inputs

  Gas regression:
  - Large swap vs small swap — assert gas < some reasonable threshold
    (use gasleft() before/after: uint256 g = gasleft(); doSwap(...); assertLt(g - gasleft(), 300_000))
"""


@dataclass
class Scenario:
    scenario_id: str
    contract_name: str
    filename: str
    source: str
    proposer: str  # "seed" | "llm" | "human" (promoted from vault)
    gen_created: int
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


class ScenarioProposer:
    """LLM-backed scenario author."""

    def __init__(self, llm: LLMClient, workspace: Path, pool: ScenarioPool):
        self.llm = llm
        self.workspace = Path(workspace)
        self.pool = pool
        self._security_context: Optional[str] = None

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
        """Propose up to `count` new scenarios. Compile-gated.

        Returns (accepted, rejection_reasons) so callers can surface failures.
        """
        await self._ensure_security_context()
        accepted: List[Scenario] = []
        rejections: List[str] = []
        # Single LLM call is cheapest; we ask for `count` scenarios at once and split them.
        prompt = self._build_prompt(hook_source, count, recent_findings, skill_md)
        raw = await self.llm.complete(prompt, timeout=timeout)
        if not raw:
            return [], []
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
            err = (r.stderr or r.stdout).splitlines()
            first = next((ln for ln in err if "Error" in ln or "error:" in ln), err[0] if err else "compile failed")
            return False, first.strip()[:200]
        except Exception as e:
            path.unlink(missing_ok=True)
            return False, f"compile-gate exception: {e}"

    def _build_prompt(
        self,
        hook_source: str,
        count: int,
        recent_findings: List[str],
        skill_md: Optional[str],
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

        return (
            f"{V4_PRIMER}\n\n"
            f"{security_block}"
            f"{skill_block}"
            f"═══ HOOK UNDER TEST (src/Hook.sol) ═══\n\n"
            f"```solidity\n{hook_source}\n```\n\n"
            f"═══ RECENT FINDINGS ═══\n{findings_block}\n\n"
            f"{existing_block}\n\n"
            f"═══ YOUR TASK ═══\n\n"
            f"Propose {count} NEW test scenarios. Requirements:\n"
            f"  - Each is a COMPLETE Solidity contract in its own ```solidity fenced block\n"
            f"  - Contract name starts with `Scenario_` and is unique\n"
            f"  - Inherits PNBase, uses only APPROVED imports, no setUp() override\n"
            f"  - Tests probe the hook above from angles NOT already covered by existing scenarios\n"
            f"  - Prioritise probing the known V4 vulnerability patterns from the security reference above\n"
            f"  - NEVER import anything outside the approved list — it will fail to compile\n\n"
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
