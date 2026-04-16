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
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
```

Do NOT import anything else — not IPoolManager, not PoolKey, not Currency, nothing.
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
 11. NEVER call hook functions that take arguments. You do not know the exact
     Solidity types (e.g. `Currency` ≠ `address`, raw ints ≠ enums) and any
     mismatch is a compile error. ONLY call zero-parameter view getters
     (e.g. `hook.owner()`, `hook.poolManager()`). For everything else, test
     behavior indirectly via doSwap/doAddLiquidity/sandwich.
 12. Do NOT use Solidity reserved words as variable names: `after`, `before`,
     `var`, `let`, `match`, `in`, `of`, `null`, `switch`, `case`, `default`,
     `static`, `typeof`. Use descriptive names like `amountOut`, `delta`,
     `tokenId`, `gasUsed` instead.
 13. pragma solidity MUST be exactly `^0.8.26`. Do NOT write `^0.826` or any
     other variation — that will fail to compile.
 14. Hook-internal enum types and constants are NOT in scope in test contracts.
     They are defined inside the hook, not imported. Do NOT reference them:
     WRONG: hook.setDepegState(..., uint256(DepegSeverity.SEVERE)); ← DepegSeverity undefined
     WRONG: assertEq(fee, Hook.FEE_SEVERE);   ← Hook is not a type you can access
     RIGHT: use numeric values or ignore the constant entirely.

 15. Do NOT use `{value: ...}` in any function call. Hook functions are not
     payable. `hook.anything{value: 0}(...)` causes Error 7006 even with value: 0.
     WRONG: hook.setDepegState{value: 0}(...)   ← Error 7006
     RIGHT: never call hook functions with args at all (see Rule 11)

 16. doSwap() returns BalanceDelta — NOT int128, NOT uint256, NOT an int256.
     WRONG: int128 x = doSwap(-1 ether, true);        ← Error 9574, won't compile
     RIGHT: int128 x = doSwap(-1 ether, true).amount1();  ← inline .amount1()
     RIGHT: BalanceDelta d = doSwap(-1 ether, true);  ← named var (import required)

     To convert int128 amounts to uint256 you need TWO casts:
     WRONG: uint256(delta.amount1())                  ← Error 9640, int128→uint256
     RIGHT: uint256(int256(delta.amount1()))           ← for signed values
     RIGHT: uint256(uint128(-delta.amount0()))         ← absolute value of negative

═══ WORKING EXAMPLE ═══

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {PNBase} from "../base/PNBase.t.sol";

contract Scenario_WorkingExample is PNBase {
    function test_ExactInput_0For1_outputPositive() public {
        // Read amounts inline — no BalanceDelta import needed
        int128 spent = doSwap(-1 ether, true).amount0();    // int128, negative
        int128 received = doSwap(-1 ether, true).amount1(); // int128, positive
        assertLt(int256(spent), 0, "spent token0");
        assertGt(int256(received), 0, "received token1");
        // To compare as uint256: need TWO casts
        uint256 absOut = uint256(int256(received));  // int128→int256→uint256 ✓
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

# Anti-pattern block injected before every hook source to deter the single most
# common class of failures: calling hook functions with wrong argument types.
_HOOK_CALL_WARNING = """\
═══ HOOK CALL ANTI-PATTERNS (memorise — these cause EVERY run to fail) ═══

  WRONG — do NOT call hook functions marked [NOT CALLABLE] in the hook source:
    Any call like hook.setDepegState(...) or hook.registerPool(...) will fail.
    These functions are present for reading WHAT the hook does — not for calling.

  WRONG (Error 9574/9640 — doSwap returns BalanceDelta, not int128):
    int128 x = doSwap(-1 ether, true);  ← Error 9574
    uint256 y = uint256(delta.amount1()); ← Error 9640 (int128 needs two casts)
  RIGHT:
    int128 x = doSwap(-1 ether, true).amount1();           ← inline works
    uint256 y = uint256(int256(doSwap(-1e18, true).amount1())); ← two casts

  WRONG (Error 9640 — Currency ≠ address):
    address s = currency0;          ← Currency is NOT implicitly address
    address s = address(currency0); ← also invalid (Error 9640)
  RIGHT (if you must compare to an address):
    address s = Currency.unwrap(currency0);   ← correct explicit unwrap

  RIGHT — test behavior indirectly, no hook calls with args:
    doSwap(-1 ether, true);                         ← tests the hook implicitly
    int128 out = doSwap(-1 ether, true).amount1();
    uint256 tokenId = doAddLiquidity(-60, 60, 1 ether);
    sandwich(-0.5 ether, true, -0.1 ether);
    // Read zero-arg public state: hook.totalProtectedVolume(), hook.stalenessThreshold()

"""


def _safe_hook_source(source: str) -> str:
    """Return a version of the hook source safe for LLM consumption.

    Functions with parameters are replaced with a one-line stub comment so the
    LLM cannot copy-paste signatures and produce type-mismatch compile errors.
    Zero-argument functions (safe to call from tests) are kept intact.

    Enum definitions are removed entirely: the LLM can see enum names in the
    code but cannot access them from tests (they are scoped to the hook contract),
    so showing them causes `DepegSeverity.SEVERE`-style 7576 errors.

    Constant state-variable declarations are also removed for the same reason
    (e.g. `uint256 public constant FEE_SEVERE = 100` → seeing it causes LLM to
    write `Hook.FEE_SEVERE` which fails to compile).
    """
    lines = source.splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]

        # Strip enum definitions (replace with a one-liner note)
        enum_start = re.match(r'(\s*)enum\s+(\w+)\s*\{', ln)
        if enum_start:
            indent = enum_start.group(1)
            enum_name = enum_start.group(2)
            # Skip the enum body
            depth = ln.count('{') - ln.count('}')
            i += 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count('{') - lines[i].count('}')
                i += 1
            out.append(f"{indent}// [ENUM {enum_name} — NOT accessible from tests, do not reference]\n")
            continue

        # Strip `constant` state variable declarations
        # Matches: `type public constant NAME = value;` or `type constant NAME = value;`
        const_m = re.match(r'(\s*\w[\w\s*]*\bconstant\b.*?;)', ln)
        if const_m and 'function' not in ln:
            # Extract just the constant name to show in the stub
            const_name_m = re.search(r'\bconstant\s+(\w+)\s*=', ln)
            const_name = const_name_m.group(1) if const_name_m else "CONSTANT"
            indent = re.match(r'(\s*)', ln).group(1)
            out.append(f"{indent}// [CONSTANT {const_name} — NOT accessible as Hook.{const_name} from tests]\n")
            i += 1
            continue

        # Look for start of a function declaration
        fn_start = re.match(r'(\s*)function\s+(\w+)\s*\(', ln)
        if fn_start:
            indent = fn_start.group(1)
            fn_name = fn_start.group(2)
            # Accumulate the full signature (until matching ')' of param list)
            sig_lines = [ln.rstrip('\n')]
            j = i
            depth = ln.count('(') - ln.count(')')
            while depth > 0 and j + 1 < len(lines):
                j += 1
                sig_lines.append(lines[j].rstrip('\n'))
                depth += lines[j].count('(') - lines[j].count(')')
            full_sig = ' '.join(sig_lines)

            # Extract parameter list content
            param_m = re.search(r'function\s+\w+\s*\(([^)]*)\)', full_sig)
            params = param_m.group(1).strip() if param_m else ''

            if params:
                # Has parameters — replace with a named stub and skip the body.
                # Show the function name so the LLM knows it exists but cannot see
                # arg types. Combine with the _HOOK_CALL_WARNING to deter direct calls.
                out.append(f"{indent}// [NOT CALLABLE — call from tests will fail] function {fn_name}(...)\n")
                i = j + 1  # skip to line after closing paren of params
                # Start with brace depth from sig_lines (handles '{' on same line as signature)
                brace_depth = sum(sl.count('{') - sl.count('}') for sl in sig_lines)
                # If opening brace not yet found, scan forward past return type / modifiers
                if brace_depth == 0:
                    while i < len(lines):
                        brace_depth += lines[i].count('{') - lines[i].count('}')
                        i += 1
                        if brace_depth > 0:
                            break  # found function body start
                        if ';' in lines[i - 1]:
                            break  # abstract/interface function, no body
                # Skip the body
                while i < len(lines) and brace_depth > 0:
                    brace_depth += lines[i].count('{') - lines[i].count('}')
                    i += 1
                continue
            else:
                # Zero-arg function — keep it intact
                out.append(ln)
        else:
            out.append(ln)
        i += 1
    return "".join(out)


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
                            fixed = self._preprocess_source(fixed)
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
        if "9574" in error or "not implicitly convertible" in error:
            hints.append("HINT: 'not implicitly convertible' — doSwap() returns BalanceDelta, NOT int128. "
                         "Use inline: int128 x = doSwap(-1 ether, true).amount1(); "
                         "Never assign doSwap() directly to int128.")
        if "9640" in error or "Explicit type conversion not allowed" in error:
            hints.append("HINT: 'Explicit type conversion not allowed' — two common causes: "
                         "(1) address(currency0) is forbidden — use Currency.unwrap(currency0) instead; "
                         "(2) uint256(int128_value) is forbidden — use uint256(int256(int128_value)) instead.")
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

    @staticmethod
    def _preprocess_source(source: str) -> str:
        """Auto-fix common trivial issues before compilation.

        1. Replace Unicode curly quotes with straight quotes (Error 8936).
        2. Auto-inject known missing imports (Error 7920 for BalanceDelta/Hooks).
        These avoid burning a full LLM fix pass on easily fixable issues.
        """
        # 1. Replace Unicode punctuation that LLMs emit but Solidity strings can't contain
        source = (source
                  .replace('\u201c', '"').replace('\u201d', '"')   # " " curly double quotes
                  .replace('\u2018', "'").replace('\u2019', "'")   # ' ' curly single quotes
                  .replace('\u2032', "'").replace('\u2033', '"')   # ′ ″ prime/double-prime
                  .replace('\u2014', '--').replace('\u2013', '-')  # — – em/en dash
                  .replace('\u2192', '->').replace('\u2190', '<-') # → ← arrows
                  .replace('\u2264', '<=').replace('\u2265', '>=') # ≤ ≥ comparison operators
                  .replace('\u2260', '!=')                         # ≠ not-equal
                  .replace('\u00d7', '*').replace('\u00f7', '/')   # × ÷ multiply/divide
                  .replace('\u2026', '...')                        # … ellipsis
                  )

        # 2. Strip {value: X} from function calls (Error 7006 — non-payable functions)
        #    LLMs sometimes write hook.setDepegState{value: 0}(...) which always fails.
        source = re.sub(r'\{value:\s*[^}]+\}', '', source)

        # 2c. Remove .toString() calls — numeric types have no toString() in Solidity (Error 9582)
        #     LLMs write JS-style: i.toString() — not valid in Solidity.
        source = re.sub(r'\.toString\(\)', '', source)

        # 2d. Strip JS-style string concatenation with + (Error 2271)
        #     Solidity does not support "str" + expr or expr + "str".
        #     Strategy: strip non-string expr after a string literal, then merge adjacent
        #     string literals (adjacent "a" "b" IS valid in Solidity — compiler concatenates them).
        #     Pass 1: "literal" + non-string-expr → "literal"  (stops before next " or , or ) or \n)
        source = re.sub(r'("(?:[^"\\]|\\.)*")(\s*\+\s*[^,\n)"]+)+', r'\1', source)
        #     Pass 2: "literal" + "literal" → "literal" "literal"  (adjacent string literals)
        source = re.sub(r'("(?:[^"\\]|\\.)*")\s*\+\s*(?=")', r'\1 ', source)

        # 2a. Normalize hook.poolManager() → address(hook.poolManager())
        #     hook.poolManager() returns IPoolManager, NOT address. assertEq(IPoolManager, address)
        #     fails with 9322 (no matching overload); address() cast fixes all comparison contexts.
        #     Idempotent: strip any existing address() wrap first, then re-wrap.
        source = re.sub(r'address\(hook\.poolManager\(\)\)', 'hook.poolManager()', source)
        source = re.sub(r'\bhook\.poolManager\(\)', 'address(hook.poolManager())', source)

        # 2e. Fix broken "NOT CALLABLE" stubs without placeholder value (Error 6933)
        #     The fast fix-LLM sometimes writes /* NOT CALLABLE ... */; without the 0.
        #     That leaves bare assignments like `x = /* ... */;` → "Expected primary expression".
        #     Insert 0 before the ; so the expression is valid.
        source = re.sub(r'(NOT CALLABLE[^*]*\*+/)\s*;', r'\1 0;', source)

        # 2f. Auto-fix int128 var = doSwap(...) → int128 var = doSwap(...).amount1() (Error 9574)
        #     Rule 16 says doSwap() returns BalanceDelta, but LLMs still write int128 assignments.
        #     Adding .amount1() keeps the variable type as int128 so all downstream usage still works.
        #     (Changing the type to BalanceDelta breaks assertLt(delta, 0) etc. — don't do that.)
        #
        #     Edge case: after fixing line 1, the LLM sometimes also calls varname.amount1() or
        #     varname.amount0() on a subsequent line, treating it as BalanceDelta. Since the var
        #     is now int128, those calls fail with 9582. Collect fixed var names and strip the
        #     redundant .amount0/.amount1 suffix from any later use of those vars.
        _fixed_int128_vars: list[str] = []
        def _fix_int128_doswap(m: re.Match) -> str:
            # Extract the variable name from the declaration
            var_m = re.search(r'\bint128\s+(\w+)\s*=', m.group(0))
            if var_m:
                _fixed_int128_vars.append(var_m.group(1))
            return m.group(1) + '.amount1()'
        source = re.sub(
            r'(\bint128\s+\w+\s*=\s*doSwap\s*\([^)\n]*\))(?!\.)',
            _fix_int128_doswap,
            source,
        )
        # Strip .amount0() / .amount1() calls on vars we just fixed to int128
        for _var in _fixed_int128_vars:
            source = re.sub(r'\b' + re.escape(_var) + r'\.(amount[01])\(\)', _var, source)

        # 2g2. Strip .amount0()/.amount1() from primitive-type-cast expressions (Error 9582)
        #      Pattern: int256(x).amount1() — x is int128, so int256(x) has no .amount1().
        #      Safe because legit code writes int256(d.amount1()) (method inside parens), not
        #      int256(d).amount1() (method after the cast closes). Strip the trailing call.
        source = re.sub(
            r'\b(?:int|uint)(?:256|128|64|32)\s*\([^)]+\)\.(amount[01])\(\)',
            lambda m: m.group(0)[:m.group(0).rfind('.' + m.group(1))],
            source,
        )

        # 2g. Auto-fix doSwap(-uint_var, ...) → doSwap(-int128(uint_var), ...) (Error 4907)
        #     Unary negation on uint128/uint256 is invalid. Collect uint-typed vars and cast.
        _uint_vars: list[str] = []
        for _um in re.finditer(r'\buint(?:128|256)\s+(\w+)\s*=', source):
            _uint_vars.append(re.escape(_um.group(1)))
        if _uint_vars:
            _uint_pat = '|'.join(_uint_vars)
            source = re.sub(
                r'\bdoSwap\s*\(\s*-(' + _uint_pat + r')\s*,',
                lambda m: f'doSwap(-int128({m.group(1)}),',
                source,
            )

        # 2h. Strip poolManager.getPool(...) — method doesn't exist (Error 9582)
        #     LLMs write poolManager.getPool(poolKey).fee etc. Replace the whole chain with 0.
        source = re.sub(r'\bpoolManager\.getPool\s*\([^)]*\)(?:\.\w+)*', '/* getPool N/A */ 0', source)

        # 2i. Fix missing closing paren in assert calls (Error 2314)
        #     LLMs sometimes write assertGt(expr, 0; instead of assertGt(expr, 0);
        #     Allow one level of nested parens (e.g. hook.getter()) in the first arg.
        source = re.sub(
            r'(assert(?:Gt|Lt|Ge|Le|Eq|Ne)\s*\([^)]*(?:\([^)]*\)[^)]*)*),\s*(\d+|true|false|[a-zA-Z_]\w*)\s*;',
            r'\1, \2);',
            source,
        )

        # 2b. Replace hook.fn(non-empty-args) with a comment + 0 placeholder.
        #     All multi-arg hook calls are NOT CALLABLE; replacing them avoids 9553/7576
        #     while keeping zero-arg calls like hook.owner() or hook.totalProtectedVolume().
        #     Single-arg calls (e.g. hook.protectedVolume(poolId)) are OK — keep those.
        #     We only strip calls with 2+ args (contain a comma).
        def _strip_multi_arg_hook_call(m: re.Match) -> str:
            fn_name = m.group(1)
            args = m.group(2)
            # Keep single-arg calls (no comma) — they may be valid view getters
            if ',' not in args:
                return m.group(0)
            return f"/* hook.{fn_name}(...) — NOT CALLABLE, removed */ 0"
        source = re.sub(
            r'\bhook\.(\w+)\s*\(([^)\n]*\S[^)\n]*)\)',
            _strip_multi_arg_hook_call,
            source,
        )

        # 3. Auto-inject / normalise known imports.
        #    Problem: LLMs often import from wrong paths (e.g. "lib/uniswap-hooks/..." or relative
        #    paths) causing Error 2904 "Declaration not found". Solution: for each known type,
        #    strip ALL existing imports of that type (regardless of path) then re-inject the
        #    canonical path. This is idempotent — if the import is already correct it gets removed
        #    and re-added in the same position (or at the end of the import block).
        _AUTO_IMPORTS = {
            "BalanceDelta": 'import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";',
            "Hooks": 'import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";',
            "IHooks": 'import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";',
            "TickMath": 'import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";',
            "Constants": 'import {Constants} from "@uniswap/v4-core/test/utils/Constants.sol";',
        }
        for type_name, import_line in _AUTO_IMPORTS.items():
            if not re.search(r'\b' + type_name + r'\b', source):
                continue  # type not used — skip
            # Strip any existing import that exposes only this type (handles wrong-path imports).
            # Matches: import {TypeName} from "..."; or import {TypeName, ...} (combined imports
            # are left alone to avoid accidentally removing needed sibling types).
            source = re.sub(
                r'^import\s+\{' + re.escape(type_name) + r'\}\s*from\s*[^;]+;\n?',
                '',
                source,
                flags=re.MULTILINE,
            )
            if import_line not in source:
                import_re = re.compile(r'^import\s+.*$', re.MULTILINE)
                matches = list(import_re.finditer(source))
                if matches:
                    last_import_end = matches[-1].end()
                    source = source[:last_import_end] + '\n' + import_line + source[last_import_end:]
                else:
                    # No imports at all — prepend before contract declaration
                    source = import_line + '\n' + source
        return source

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
            # Dump failing source for debugging before cleanup.
            _debug_dir = Path("/tmp/pn-failed-scenarios")
            _debug_dir.mkdir(exist_ok=True)
            (_debug_dir / f"{name}.t.sol").write_text(source)
            (_debug_dir / f"{name}.err").write_text(r.stderr or r.stdout or "")
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
                # Auto-inject known missing imports before compile gate so the
                # stored scenario source matches what was actually compiled.
                source = self._preprocess_source(source)
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
                            fixed = self._preprocess_source(fixed)
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
            f"{_HOOK_CALL_WARNING}"
            f"{security_block}"
            f"{skill_block}"
            f"═══ PERSONA: {persona.label} ═══\n\n"
            f"You are acting as {persona.description}\n\n"
            f"Suggested angles for this persona:\n{angles_block}\n\n"
            f"═══ HOOK UNDER TEST (src/Hook.sol) ═══\n\n"
            f"```solidity\n{_safe_hook_source(hook_source)}\n```\n\n"
            f"═══ RECENT FAILURES FOR THIS PERSONA ═══\n{findings_block}\n\n"
            f"{failed_block}"
            f"{existing_block}\n\n"
            f"═══ LAST-CHANCE CHECKLIST (read this before writing a single line) ═══\n\n"
            f"  ✗ hook.registerPool(...)   — NOT CALLABLE (9553)\n"
            f"  ✗ hook.setDepegState(...)  — NOT CALLABLE (9553)\n"
            f"  ✗ hook.setCallbackSource(...)  — NOT CALLABLE (9553)\n"
            f"  ✗ poolManager.getPool(...)   — method does not exist (9582)\n"
            f"  ✗ poolManager.getSlot0(...).sqrtPrice  — wrong return type (9582)\n"
            f"  ✗ positionManager.positions(...)  — method does not exist (9582)\n"
            f"  ✗ hook.fn{{value: X}}(...)  — non-payable (7006)\n"
            f"  ✗ DepegSeverity.X, Hook.FEE_X  — not in scope (7576)\n"
            f"  ✗ hook.poolManager() returns IPoolManager NOT address (9322/9574)\n"
            f"  ✓ address(hook.poolManager())  — CORRECT to compare to address\n"
            f"  ✗ i.toString(), x.toString()  — Solidity has NO .toString() (9582)\n"
            f"  ✗ \"text\" + expr  — Solidity has NO + string concat (2271)\n"
            f"  ✗ int128 x = doSwap(...)  — WRONG, doSwap returns BalanceDelta (9574)\n"
            f"  ✓ BalanceDelta d = doSwap(-1 ether, true);  then d.amount1()  — CORRECT\n"
            f"  ✗ IPoolManager.SwapParams, poolManager.swap(...)  — NOT in scope, NOT callable (7576/9582)\n"
            f"  ✗ PNBase.getAmountsForLiquidity(...)  — does NOT exist (7576)\n"
            f"  ✓ doSwap(-1 ether, true).amount1()  — CORRECT\n"
            f"  ✓ hook.owner()  — zero-arg getter, CORRECT\n"
            f"  ✗ doSwap(-uint_var, ...) — uint cannot be negated (4907); cast: doSwap(-int128(uint_var), ...)\n"
            f"  ✗ poolManager.getPool(...) — does NOT exist (9582); remove entirely\n"
            f"  ✗ assertGt(expr, 0; — missing closing paren (2314); always write assertGt(expr, 0);\n\n"
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
            f"{_HOOK_CALL_WARNING}"
            f"{security_block}"
            f"{skill_block}"
            f"═══ HOOK UNDER TEST (src/Hook.sol) ═══\n\n"
            f"```solidity\n{_safe_hook_source(hook_source)}\n```\n\n"
            f"═══ RECENT FINDINGS ═══\n{findings_block}\n\n"
            f"{failed_block}"
            f"{existing_block}\n\n"
            f"═══ LAST-CHANCE CHECKLIST (read this before writing a single line) ═══\n\n"
            f"  ✗ hook.registerPool(...)   — NOT CALLABLE (9553)\n"
            f"  ✗ hook.setDepegState(...)  — NOT CALLABLE (9553)\n"
            f"  ✗ hook.setCallbackSource(...)  — NOT CALLABLE (9553)\n"
            f"  ✗ poolManager.getPool(...)   — method does not exist (9582)\n"
            f"  ✗ poolManager.getSlot0(...).sqrtPrice  — wrong return type (9582)\n"
            f"  ✗ positionManager.positions(...)  — method does not exist (9582)\n"
            f"  ✗ hook.fn{{value: X}}(...)  — non-payable (7006)\n"
            f"  ✗ DepegSeverity.X, Hook.FEE_X  — not in scope (7576)\n"
            f"  ✗ hook.poolManager() returns IPoolManager NOT address (9322/9574)\n"
            f"  ✓ address(hook.poolManager())  — CORRECT to compare to address\n"
            f"  ✗ i.toString(), x.toString()  — Solidity has NO .toString() (9582)\n"
            f"  ✗ \"text\" + expr  — Solidity has NO + string concat (2271)\n"
            f"  ✗ int128 x = doSwap(...)  — WRONG, doSwap returns BalanceDelta (9574)\n"
            f"  ✓ BalanceDelta d = doSwap(-1 ether, true);  then d.amount1()  — CORRECT\n"
            f"  ✗ IPoolManager.SwapParams, poolManager.swap(...)  — NOT in scope, NOT callable (7576/9582)\n"
            f"  ✗ PNBase.getAmountsForLiquidity(...)  — does NOT exist (7576)\n"
            f"  ✓ doSwap(-1 ether, true).amount1()  — CORRECT\n"
            f"  ✓ hook.owner()  — zero-arg getter, CORRECT\n"
            f"  ✗ doSwap(-uint_var, ...) — uint cannot be negated (4907); cast: doSwap(-int128(uint_var), ...)\n"
            f"  ✗ poolManager.getPool(...) — does NOT exist (9582); remove entirely\n"
            f"  ✗ assertGt(expr, 0; — missing closing paren (2314); always write assertGt(expr, 0);\n\n"
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
