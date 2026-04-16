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
            "and that the hook doesn't break routing assumptions like fee-on-transfer or callback ordering."
        ),
        scenario_angles=[
            "Query amountOut for a standard swap through this pool",
            "Route a multi-hop swap where this hook is one leg",
            "Verify the hook doesn't silently consume extra tokens during routing",
            "Test that slippage protection still works when the hook modifies swap amounts",
            "Check that exactOutput swaps work correctly (not just exactInput)",
            "Verify the hook handles zeroForOne and oneForZero symmetrically",
        ],
    ),
    PersonaDef(
        id="mev-searcher",
        label="MEV Searcher",
        direction="top-right",
        description=(
            "an MEV bot or searcher (sandwich attacker, JIT liquidity provider, arbitrageur). "
            "You are actively trying to extract value from this hook. You care about: "
            "whether the hook can be sandwiched, whether JIT liquidity can front-run it, "
            "whether fee changes can be exploited, and whether oracle dependencies create arbitrage."
        ),
        scenario_angles=[
            "Sandwich attack: buy before a large swap, sell after — does the hook protect against this?",
            "JIT liquidity: add concentrated liquidity just before a swap, remove immediately after",
            "Front-run a fee change — trade before/after hook fee adjustment for profit",
            "Exploit oracle lag: hook uses stale price, arbitrage the deviation",
            "Grief the hook by repeatedly triggering expensive state updates",
            "Flash loan attack: borrow, manipulate hook state, repay in one tx",
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
            "your liquidity isn't silently disadvantaged by the hook's fee logic."
        ),
        scenario_angles=[
            "Add 1M USDC of liquidity — verify correct fee accounting",
            "Remove all liquidity at once — no stuck funds, correct token return",
            "Add liquidity spanning current tick — verify position initializes correctly",
            "Add liquidity out of range — verify no immediate loss",
            "Check fee earnings accumulate correctly over multiple swaps",
            "Verify hook doesn't lock liquidity during a depeg or oracle event",
            "Test add/remove round-trip: end balance should equal start minus gas",
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
            "Swap 100 USDC for ETH — verify slippage is within tolerance",
            "Swap with tight deadline (1 block) — does it succeed or revert clearly?",
            "Swap at minimum viable amount — no rounding errors",
            "Swap at maximum pool depth — no overflow",
            "Verify the user gets back exactly what the quote promised (±slippage)",
            "Swap when hook is in unusual state (depeg, high volatility) — graceful behavior",
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
            "You care about: whether the pool returns valid metadata, correct fee display, "
            "whether the hook breaks standard pool queries, and eligibility for routing inclusion."
        ),
        scenario_angles=[
            "Query pool fee — does the hook return a valid, displayable fee?",
            "Query pool tick spacing and verify it matches the hook's expectations",
            "Verify the pool initializes with a valid sqrtPriceX96 (not zero, not out of range)",
            "Check that hook permissions flags match what the contract actually implements",
            "Test that a small swap succeeds (pool is functional enough to list)",
            "Verify the pool emits standard events that front-ends can index",
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
            "Gas consistency: compare gasleft() before/after first vs tenth swap — should be within 2x",
            "Throughput: 10 doSwap calls in a loop — all should succeed without reverting",
            "No O(n) growth: run doSwap 5 times, verify last gas cost is not 5x the first",
            "LP + swap batch: doAddLiquidity → 3× doSwap → doRemoveLiquidity — no interference",
            "hookData overhead: doSwap vs doSwapWithHookData — compare gas difference",
            "Direction alternation: swap 0→1 then 1→0 repeatedly — gas should stay stable",
        ],
    ),
]

PERSONA_BY_ID = {p.id: p for p in PERSONAS}
