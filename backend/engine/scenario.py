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
import hashlib
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import pvariance
from typing import Dict, List, Optional

from .llm import LLMClient


WORKSPACE_IMAGE = os.getenv("PN_FOUNDRY_IMAGE", "probably-nothing-foundry")
COMPILE_TIMEOUT = int(os.getenv("PN_COMPILE_TIMEOUT", "60"))

V4_PRIMER = """\
# V4 scenario-author primer

You are authoring a Foundry test contract that probes a Uniswap V4 hook.

**Base class** — `PNBase` (imported as `../base/PNBase.t.sol`) gives you:
  - `hook` — the deployed hook under test
  - `poolKey` — an initialised pool using this hook at fee=3000, tickSpacing=60
  - `currency0`, `currency1` — two funded test tokens (assigned in lexical order)
  - `doSwap(int256 amountSpecified, bool zeroForOne) returns (BalanceDelta)`
       negative amountSpecified = exact-input, positive = exact-output
  - `doSwapWithHookData(amount, zeroForOne, bytes hookData)`
  - `doAddLiquidity(int24 tickLower, int24 tickUpper, int256 liquidityDelta)`
  - `doRemoveLiquidity(int24 tickLower, int24 tickUpper, int256 liquidityDelta)`
  - `sandwich(int256 victimAmount, bool zeroForOne, int256 attackerAmount)`

**Imports already wired** (use these exact remapped paths):
  - `forge-std/Test.sol` (Test, assertEq, assertLt, vm.expectRevert, etc.)
  - `@uniswap/v4-core/src/types/BalanceDelta.sol`
  - `@uniswap/v4-core/src/types/PoolKey.sol`
  - `@uniswap/v4-core/src/libraries/TickMath.sol`
  - `@uniswap/v4-core/src/libraries/Hooks.sol`
  - `@uniswap/v4-core/src/types/Currency.sol`
  - `@uniswap/v4-core/test/utils/Constants.sol` (SQRT_PRICE_1_1, etc.)
  - `@openzeppelin/uniswap-hooks/src/base/BaseHook.sol` (only if you need to reference BaseHook)

**Hard rules**:
  - Output MUST be a single complete Solidity contract in one ```solidity fenced block.
  - The contract MUST inherit `PNBase` and MUST NOT override `setUp()` unless
    calling `super.setUp()` first.
  - Every test function MUST start with `test_` (Foundry convention).
  - Do NOT redeclare the hook, poolKey, or currencies.
  - Keep each test focused on ONE behaviour so gas readings are diagnostic.

**Encouraged novelty**:
  - Routing: exact-in / exact-out boundary conditions, near-zero swaps, swaps
    that consume the whole range, swaps across tick boundaries.
  - LP: range-crossing liquidity, mint-burn-mint, fee accrual during sandwich.
  - MEV: multi-block sandwich, JIT provision at the sandwich tick, back-run
    through a second pool.
  - Edge cases: reentrant callbacks, malformed hookData, integer wrap, zero
    liquidity swaps.
  - Hook permissions: deliberate misuse (calling unauthorised hook functions).
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

    async def propose_batch(
        self,
        hook_source: str,
        count: int,
        gen: int,
        recent_findings: List[str],
        skill_md: Optional[str] = None,
        timeout: float = 120.0,
    ) -> List[Scenario]:
        """Propose up to `count` new scenarios. Compile-gated; only valid ones returned."""
        accepted: List[Scenario] = []
        # Single LLM call is cheapest; we ask for `count` scenarios at once and split them.
        prompt = self._build_prompt(hook_source, count, recent_findings, skill_md)
        raw = await self.llm.complete(prompt, timeout=timeout)
        if not raw:
            return []
        for source in _split_scenarios(raw):
            name = _extract_contract_name(source)
            if not name:
                continue
            if name in {s.contract_name for s in self.pool.all()}:
                continue
            ok, reason = await self._compile_gate(name, source)
            if not ok:
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
        return accepted

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

        return (
            f"{V4_PRIMER}\n\n"
            f"{skill_block}"
            f"**Hook under test** (src/Hook.sol):\n```solidity\n{hook_source}\n```\n\n"
            f"**Recent findings from the autoresearch loop:**\n{findings_block}\n\n"
            f"{existing_block}\n\n"
            f"Propose {count} NEW scenarios. Each must be a complete contract inheriting `PNBase`.\n"
            f"Each contract MUST have a unique name starting with `Scenario_` followed by a short\n"
            f"descriptive suffix (e.g. `Scenario_NearZeroSwap`, `Scenario_JIT_Sandwich`).\n"
            f"Output each contract in its own ```solidity fenced block. No commentary between blocks."
        )


# ─── helpers ───────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:solidity|sol)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_CONTRACT_NAME = re.compile(r"\bcontract\s+(Scenario_[A-Za-z0-9_]+|[A-Z][A-Za-z0-9_]*)\b")


def _split_scenarios(raw: str) -> List[str]:
    return [m.group(1).strip() for m in _FENCE.finditer(raw) if "contract" in m.group(1)]


def _extract_contract_name(source: str) -> Optional[str]:
    m = _CONTRACT_NAME.search(source)
    return m.group(1) if m else None
