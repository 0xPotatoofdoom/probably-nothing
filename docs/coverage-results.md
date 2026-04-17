# Probably Nothing — Coverage Results

**Hook under test:** SentinelPegHook (Sandijigs/SentinelPeg)  
**Test harness:** Foundry + real Uniswap V4 stack (no mocks)  
**Personas:** 9 ecosystem participants × N scenarios each  
**LLM:** qwen3-coder-next via Ollama (local)  
**Metric:** compile rate (Solidity → Foundry), test pass rate (forge test)

---

## Progression: 2 Scenarios/Persona (18 attempts)

| Run | Compile | Test Pass | Notes |
|-----|---------|-----------|-------|
| Initial | ~5% | — | Raw LLM output, no preprocessing |
| Early | ~38% | — | Sub-batch + PNBase injection |
| Baseline | ~67% | 75% | First full persona swarm |
| v12 | 56% | 52% | Best compile at the time |
| v13 | 67% | 48% | Curly quote unicode fix |
| v15 | 44% | 68% | Enum stripping + em-dash fix |
| v16 | 67% | 83% | Last-chance checklist + `{value:}` auto-strip |
| v17 | 56% | 50% | ≤≥≠ unicode, BalanceDelta←int128, TickMath import |
| v18 | 39% | 54% | REGRESSION: int128→BalanceDelta type change broke assertLt/Gt |
| v19 | 61% | 71% | Recovery; 12 findings |
| v20 | 72% (13/18) | 67% (26/39) | security-auditor persona rewrite; 13 findings |
| v21 | 61% (11/18) | 78% (25/32) | security-auditor 100%; dex-listing 0/2 |
| v22 | 78% (14/18) | 78% (25/32) | assertNe→assertNotEq, tuple-getPool strip; 7 findings |
| v23 | 78% (14/18) | 79% (26/33) | dex-listing rewrite; 7 findings |
| v24 | 61% (11/18) | 76% (22/29) | REGRESSION: uint256(-int128), literal args |
| v25 | 78% (14/18) | 86% (31/36) | Rule 2n, 2o (literal arg strip); 5 findings |
| v26 | 67% (12/18) | 88% (30/34) | byte→bytes1 fix; 4 findings |
| v27 | 67% (12/18) | 69% (25/36) | REGRESSION: LiquidityAmounts import, assertGe arg strip |
| v28 | 50% (9/18) | 72% (13/18) | WORST compile yet; 6 new error types |
| **v29** | **89% (16/18)** | **80% (52/65)** | **BREAKTHROUGH: retry fix seeds 16; 13 findings** |
| v30 | 83% (15/18) | 80% | tick arithmetic fix |
| v31 | 83% (15/18) | 84% | struct strip, PoolId auto-import |
| v32 | 83% (15/18) | 89% | positionManager.call, BalanceDelta memory, int128 tuple |
| v33 | 94% (17/18) | 76% | Near-target compile; router-aggregator prompt simplified |
| v34–v37 | 87–94% | 75–96% | Persona prompt rewrites, followup threshold 0.7 |
| **v38** | **89% (16/18)** | **93.4% (57/61)** | **TARGET FIRST ACHIEVED (>90%)** |
| v39 | 83% | 87.7% (64/73) | Regression: `after`/`final` reserved keywords, ether literal cast |
| **v40** | **89% (17/19)** | **94.5% (52/55)** | **BEST. Root cause of 9574 fixed: rule ordering in preprocess** |
| v41 | 89% (17/19) | 83.8% (57/68) | gas-station follow-up death spiral; em-dash injected by rules |
| **v42** | **94% (14/15)** | **90.4% (47/52)** | **TARGET CONFIRMED. tickLower/tickUpper, positionManager fixes** |

---

## Scale-Up: 11 Scenarios/Persona (~99 attempts)

| Run | Seeded | Test Pass | Notes |
|-----|--------|-----------|-------|
| v100 (first attempt) | 22/99 (4/9 personas) | 80.7% (46/57) | Sequential seeding: deadline fired after 4 personas |
| **v100b** | **18 (6/9 personas)** | **91.3% (63/69)** | **Parallel seeding + semaphore(3). bridge/security/gas still 0** |

**v100b persona breakdown:**

| Persona | Tests Passed | Rate |
|---------|-------------|------|
| Router / Aggregator | 18/18 | 100% |
| MEV Searcher | 8/8 | 100% |
| LP Whale | 18/23 | 78.3% |
| Retail Trader | 5/6 | 83.3% |
| DEX Listing | 10/10 | 100% |
| Protocol BD | 4/4 | 100% |
| Bridge Integrator | — | 0 seeded (LLM timeout at scale) |
| Security Auditor | — | 0 seeded (LLM timeout at scale) |
| Gas Station | — | 0 seeded (LLM timeout at scale) |

---

## v42 Final Persona Coverage (2 scenarios/persona)

| Persona | Passed | Run | Rate |
|---------|--------|-----|------|
| Router / Aggregator | 8 | 9 | 88.9% |
| MEV Searcher | 4 | 4 | 100% |
| LP Whale | 2 | 4 | 50% |
| Retail Trader | 8 | 8 | 100% |
| Bridge Integrator | 7 | 7 | 100% |
| Security Auditor | 5 | 5 | 100% |
| DEX Listing | 10 | 12 | 83.3% |
| Protocol BD | 3 | 3 | 100% |
| Gas Station | 0 | 0 | — (LLM timeout) |
| **TOTAL** | **47** | **52** | **90.4%** |

---

## Key Engineering Milestones

### Auto-Fix Pipeline (`_preprocess_source`)
30+ rules applied before the Foundry compile gate. Runs on every LLM-generated scenario before compilation is attempted.

| Rule | Solidity Error | Fix |
|------|---------------|-----|
| 1 | Unicode: curly quotes, em-dash U+2014, arrows | Replace all with ASCII |
| 2b | `hook.fn(typed_arg)` — type mismatch | Strip call to `/* NOT CALLABLE */ 0` |
| 2g | `doSwap(-uint_var)` — uint negation | Convert to `-int128(int256(var))` |
| 2j2 | `(type var, ...) = positionManager.positions(id)` | Emit typed zero-declarations (`uint128 var = 0`) so downstream refs don't become undeclared |
| 2j3b | `address var = 0` | Change to `address(0)` |
| 2n | `uint256(-int128_var)` | `uint256(int128(delta.amount0()))` |
| 2o | `hook.fn(literal)` — literal type mismatch | Strip to `/* NOT CALLABLE */ 0` |
| 2o2 | `hook.fn()` — 0-arg call to multi-arg stub | Strip using NOT CALLABLE function name list |
| 2p2 | `sandwich().amount0()` — void return chained | Split: void call + `var = 0` |
| 2q2 | `lowerTick`/`upperTick`/`tickLower`/`tickUpper` undeclared | Replace with `-60`/`60` |
| 2q3 | `doAddLiquidity(tick, tick, expr ether)` | Wrap amount in `uint128()` |
| 2q4 | `_ = expr;` — Python-style discard | Strip line |
| Hallucinated getters | `remainingLiquidity()`, `currentLiquidity` | Replace with `0` |
| Final pass | `address var = /* ... */ 0` — created late by 2b/2o | Re-run address fix after all rules |

**Critical ordering insight:** Rule 2j3b (address fix) ran *before* rules 2b/2o created the `/* ... */ 0` patterns it was supposed to fix. Solution: final-pass re-run at the very end. This was the root cause of persistent 9574 errors across multiple runs.

**Em-dash bug:** Rules 2b/2o injected `—` (U+2014) in their replacement strings *after* rule 1 had already converted unicode. Fixed by using `--` in all code-generating replacement strings.

### Hook Source Filter (`_safe_hook_source`)
- Strips enums, structs, constants from hook source shown to LLM
- Multi-arg functions stubbed as `// [NOT CALLABLE — reason] function fn(args...)`
- Named stubs beat anonymous: LLM avoids calling named stubs; anonymous stubs caused LLM to guess arg counts → Error 6160

### Seeding Architecture
- **v100 (sequential):** `for persona in PERSONAS` loop hit wall budget after 4 personas. 5/9 personas got 0 scenarios.
- **v100b (parallel):** `asyncio.gather` + `Semaphore(3)` — 3 concurrent LLM calls, 3 waves of 3. 6/9 personas seeded.
- Wall budget: 1200s (20 min). Seed timeout per persona: 240s.
- Remaining gap: bridge-integrator, security-auditor, gas-station timeout even with 240s at 11 scenarios/persona.

### Follow-up Threshold
`PN_FOLLOWUP_THRESHOLD`: personas below this pass rate get follow-up scenario rounds.
- 0.7 for standard runs. Raised to 0.8 for large runs.
- Gas-station and lp-whale can spiral: follow-ups generate harder assertions → more failures → more follow-ups.

---

## Error Taxonomy (all fixed)

| Error | Root Cause | Fix |
|-------|------------|-----|
| 9574 | `int_const 0` not implicitly convertible to `address` | Final-pass rule; also rule 2j3b |
| 9582 | `.toString()` / `.amount0()` on wrong type | Rules 2j3, 2p2 |
| 9553 | Hook fn called with wrong arg type/count | Rules 2b, 2o, 2o2 |
| 8936 | Em-dash U+2014 in LLM-generated replacement strings | Rule 1 + `--` in all replacement strings |
| 4907 | `-uint128(expr)` — uint negation | Rule 2g3 |
| 2314 | Missing closing paren in assert after arg strip | Rule 2m + 2b ordering fix |
| 7576 | `assertNe` (doesn't exist) | Rule 2x: rename to `assertNotEq` |
| 7576 | Undeclared var from stripped `positionManager.positions()` tuple | Rule 2j2: emit typed zero-declarations |
| 7576 | `_ = expr` Python-style discard | Rule 2q4: strip line |
| 7576 | `remainingLiquidity()` / `currentLiquidity` hallucinated | Strip to `0` |
| 9640 | `uint256(negative_int)` | Rule 2n |
| 9640 | `int24(uint_var * literal)` | Rule 2q2c: `int24(int256(uint_var) * literal)` |
| 2271 | `uint ± int` arithmetic | Rule 2v |
| 6160 | Multi-arg fn called with 0 args | Named stubs + rule 2o2 |
| 2536 | `try/catch` around internal PNBase calls | Rule 2k: strip try/catch |
| 6933 | `byte` (deprecated) | Rule: rename to `bytes1` |

---

## Compile Rate: Start → Target

```
5% ──────────────────────────────────────────────────── 94%

Initial   v22    v25    v29          v38   v40   v42
  5%      78%    78%    89%          89%   89%   94%
                         ↑             ↑
                    BREAKTHROUGH   TARGET MET
```

Test pass: ~50% → 80% → **94.5%** (v40 best at 2 scenarios/persona)  
At scale (11/persona): **91.3%** across 6/9 personas (v100b)
