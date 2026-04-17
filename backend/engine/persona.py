"""
Ecosystem persona definitions for the Probably Nothing swarm.

Each PersonaDef represents a real participant in a Uniswap V4 hook's
lifecycle — the people and systems that will interact with it after
deployment. The swarm generates test scenarios from each persona's
perspective and runs them against the hook to build a coverage matrix:
"does this hook work for everyone who will use it?"
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List


@dataclass
class PersonaDef:
    id: str                          # machine-readable slug
    label: str                       # display name
    direction: str                   # UI positioning hint (top/right/bottom/left)
    description: str                 # injected verbatim into LLM prompt
    scenario_angles: List[str]       # suggested test angles for this persona


PERSONAS: List[PersonaDef] = [
    PersonaDef(
        id="router-aggregator",
        label="Router / Aggregator",
        direction="top",
        description=(
            "a DEX aggregator or router (1inch, Paraswap, CoW Protocol, Uniswap Universal Router). "
            "You need to route swaps efficiently through this pool. You care about: "
            "correct price quotes, predictable gas costs, no silent reverts, "
            "and that the hook doesn't break routing assumptions. "
            "IMPORTANT: use ONLY doSwap/doSwapWithHookData/doAddLiquidity/doRemoveLiquidity. "
            "Do NOT test fee-on-transfer, multi-hop routing, or exactOutput — those require external "
            "infrastructure not in PNBase. Test only what doSwap directly returns."
        ),
        scenario_angles=[
            "zeroForOne swap: doSwap(-1 ether, true).amount0() < 0 and .amount1() > 0 — verify signs",
            "oneForZero swap: doSwap(-1 ether, false).amount0() > 0 and .amount1() < 0 — verify signs",
            "Symmetry: doSwap(-1 ether, true) and doSwap(-1 ether, false) both return non-zero BalanceDelta",
            "No silent revert: doSwap(-0.001 ether, true) succeeds without revert",
            "Large swap: doSwap(-100 ether, true) succeeds — hook doesn't block large router flows",
            "Back-to-back: doSwap(-1 ether, true) then doSwap(-1 ether, false) — both succeed",
        ],
    ),
    PersonaDef(
        id="mev-searcher",
        label="MEV Searcher",
        direction="top-right",
        description=(
            "an MEV bot or searcher (sandwich attacker, JIT liquidity provider, arbitrageur). "
            "You are testing whether MEV operations SUCCEED OR FAIL without reverting unexpectedly. "
            "IMPORTANT: in default test state (no depeg active), the hook does NOT block swaps. "
            "Do NOT assert that sandwich attacks are blocked — in default state they succeed. "
            "Instead: verify that MEV-style sequences EXECUTE without revert and return valid deltas. "
            "Use the `sandwich(bigAmount, zeroForOne, victimAmount)` helper for sandwich sequences."
        ),
        scenario_angles=[
            "sandwich(-10 ether, true, -5 ether): verify the sequence doesn't revert",
            "JIT: doAddLiquidity(-60, 60, 100 ether) → doSwap(-1 ether, true) → doRemoveLiquidity(id) — all succeed",
            "Repeated swaps: doSwap 5× large amounts alternating direction — no state corruption",
            "Delta signs after sandwich: returned BalanceDelta has expected signs (.amount0 < 0 or .amount1 > 0)",
            "Large swap: doSwap(-1000 ether, true) succeeds — hook doesn't block large MEV flows",
            "Back-to-back large + small: doSwap(-100 ether, true) then doSwap(-1, true) — both valid",
        ],
    ),
    PersonaDef(
        id="lp-whale",
        label="LP Whale",
        direction="right",
        description=(
            "a large liquidity provider (protocol treasury, DAO, market maker) deploying millions in liquidity. "
            "You care about: predictable fee earnings, safe removal of liquidity, "
            "no stuck positions, correct behavior at tick boundaries, and that "
            "your liquidity isn't silently disadvantaged by the hook's fee logic. "
            "IMPORTANT: in default test state (no depeg active), totalProtectedVolume and fee state "
            "may NOT change after swaps. Use assertGe (not assertGt) for state that might not change, "
            "or test structural invariants (add+remove round-trip) instead of fee-delta assertions."
        ),
        scenario_angles=[
            "Add/remove round-trip: doAddLiquidity then doRemoveLiquidity — verify no stuck funds",
            "Add liquidity out of range — verify no immediate token loss (assertGe, not assertGt)",
            "Large add: doAddLiquidity(-600, 600, 100 ether) succeeds without revert",
            "Multiple positions: add two positions at different tick ranges — both should return valid tokenId",
            "Tick boundary: add liquidity exactly at current tick — no revert, valid position",
            "Remove partial: add liquidity, remove half — remaining position is still valid",
        ],
    ),
    PersonaDef(
        id="retail-trader",
        label="Retail Trader",
        direction="bottom-right",
        description=(
            "an ordinary end user swapping tokens through a front-end (Uniswap UI, Rabby swap). "
            "You care about: getting the quoted amount, not being sandwiched, "
            "reasonable gas costs, clear revert messages when something fails, "
            "and that the hook doesn't silently take extra tokens."
        ),
        scenario_angles=[
            "Small swap succeeds: doSwap(-1000, true).amount1() > 0 — verify non-zero output",
            "Minimum amount: doSwap(-1, true) — must not revert (no rounding-to-zero errors)",
            "Symmetry: doSwap(-1 ether, true) and doSwap(-1 ether, false) both return non-zero",
            "Delta signs: doSwap(-1 ether, true).amount0() < 0 (spent), .amount1() > 0 (received)",
            "Large swap: doSwap(-100 ether, true) succeeds without overflow or revert",
            "Back-to-back swaps: two sequential swaps should both succeed without state corruption",
        ],
    ),
    PersonaDef(
        id="bridge-integrator",
        label="Bridge Integrator",
        direction="bottom",
        description=(
            "a cross-chain bridge or relayer (LayerZero, CCIP, Stargate, Axelar) delivering "
            "liquidity or swap instructions from another chain. "
            "You care about: atomic execution, no partial fills leaving the bridge stuck, "
            "that the hook doesn't reject cross-chain messages, and consistent behavior "
            "regardless of the originating chain."
        ),
        scenario_angles=[
            "Bridge delivers tokens and immediately swaps — atomic execution",
            "Bridge delivers an amount slightly different from expected — hook handles gracefully",
            "Cross-chain message arrives when pool is paused or in unusual state",
            "Verify the hook doesn't use msg.sender assumptions that break bridge patterns",
            "Test with tokens arriving from a trusted bridge address (no approvals pre-set)",
            "Verify no callback patterns that bridges can't satisfy",
        ],
    ),
    PersonaDef(
        id="security-auditor",
        label="Security Auditor",
        direction="bottom-left",
        description=(
            "a smart contract security auditor probing hook invariants and edge cases "
            "using only the PNBase test helpers (doSwap, doAddLiquidity, doRemoveLiquidity, "
            "doSwapWithHookData, sandwich, and zero-arg hook getters). "
            "You test: delta sign correctness, state invariants via getters, "
            "hook behavior under unusual inputs (minimal amounts, reversed directions, "
            "custom hookData payloads), and sequencing/ordering effects."
        ),
        scenario_angles=[
            "Delta signs: doSwap(-1 ether, true).amount0() must be negative (spent), .amount1() must be positive (received)",
            "hookData fuzzing: doSwapWithHookData with empty bytes, abi.encode(uint256(0)), abi.encode(address(this))",
            "Boundary inputs: doSwap(-1, true) minimal 1-wei swap — must not revert",
            "Sequencing: 5 swaps alternating direction — hook.totalProtectedVolume() or similar getter should change monotonically",
            "LP round-trip invariant: doAddLiquidity then doRemoveLiquidity — pool state should not be corrupted",
            "Sandwich state: run sandwich(-1 ether, true, -0.5 ether), verify hook zero-arg getters return consistent values after",
            "Idempotency: same doSwap amount twice — second result should be same sign as first",
            "Large input: doSwap(-100 ether, true) then doSwap(-100 ether, false) — hook should not revert or corrupt state",
        ],
    ),
    PersonaDef(
        id="dex-listing",
        label="DEX Listing / Front-end",
        direction="left",
        description=(
            "a front-end team or DEX listing service (Uniswap Labs interface, DeFiLlama, GeckoTerminal) "
            "deciding whether to surface this pool. "
            "You care about: whether the pool's hook interface is valid, whether basic swaps succeed, "
            "and whether hook zero-arg getters return sensible values for display. "
            "Use ONLY PNBase helpers (doSwap, doAddLiquidity, doRemoveLiquidity, hook zero-arg getters). "
            "Do NOT check poolKey.hooks (it is IHooks not address), do NOT use poolManager.getSlot0, "
            "do NOT expect events — just verify hook behavior via getters and swap outcomes."
        ),
        scenario_angles=[
            "Small swap succeeds: doSwap(-0.01 ether, true) should return non-zero BalanceDelta",
            "Hook owner is set: hook.owner() should return non-zero address",
            "Pool accepts liquidity: doAddLiquidity(-60, 60, 1 ether) returns valid tokenId (>0)",
            "Hook zero-arg getters return without revert: call each getter and assert non-revert",
            "Round-trip: doAddLiquidity then doRemoveLiquidity — no permanent state corruption",
            "Symmetric swaps: doSwap(-1 ether, true) and doSwap(-1 ether, false) both return non-zero",
        ],
    ),
    PersonaDef(
        id="protocol-bd",
        label="Protocol BD / Integrator",
        direction="left",
        description=(
            "a protocol integration team building on top of this hook "
            "(e.g., a yield aggregator, a structured product, a points system). "
            "You care about: stable external interfaces, predictable hook behavior, "
            "whether the hook can be composed with other protocols, "
            "and that state changes don't break downstream integrations."
        ),
        scenario_angles=[
            "Call the hook's public/external functions from a third-party contract",
            "Verify hook state is consistent across multiple transactions",
            "Test composability: hook pool inside a multicall with other protocol interactions",
            "Verify the hook's custom storage is not corrupted by concurrent pool operations",
            "Test that the hook works correctly as one leg of a more complex strategy",
            "Check that any privileged functions (owner, admin) have appropriate guards",
        ],
    ),
    PersonaDef(
        id="gas-station",
        label="Gas Station / High-Frequency",
        direction="top-left",
        description=(
            "a high-frequency trader, keeper bot, or scalper executing many transactions per block. "
            "You care about: gas cost CONSISTENCY across repeated calls (not absolute values — "
            "hooks always add overhead), whether the hook causes gas cost SPIKES or GROWTH over time, "
            "throughput under load, and that repeated rapid interactions don't corrupt state. "
            "NEVER assert specific gas amounts (50_000, 100_000 etc.) — always compare gas "
            "between two operations using assertLt(gasA, gasB * 2) style relative checks."
        ),
        scenario_angles=[
            "Gas consistency: compare gasleft() before/after first vs tenth swap — assertLt(gas10, gas1 * 2)",
            "Throughput: 10 doSwap calls in a loop — all should succeed without reverting",
            "No O(n) growth: run doSwap 5 times, verify last gas cost is not 5x the first",
            "LP + swap batch: doAddLiquidity → 3× doSwap → doRemoveLiquidity — no interference",
            "hookData overhead: doSwap vs doSwapWithHookData(empty bytes) — assertLt(gasWithData, gasNoData * 3)",
            "Direction alternation: swap 0→1 then 1→0 repeatedly — gas should stay stable",
        ],
    ),
]

PERSONA_BY_ID = {p.id: p for p in PERSONAS}
