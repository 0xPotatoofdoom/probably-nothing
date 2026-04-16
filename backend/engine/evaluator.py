"""
Probably Nothing — Core Evaluation Engine

Two compounding agent loops run concurrently in this evaluator:

  1. Variant agents  (HookMutator + LLMMutator)
       Parametric → structural → LLM-assisted mutations of the hook source.
  2. Scenario agents (ScenarioProposer)
       LLM-authored Forge test contracts that probe the hook from new angles.
       Compile-gated, pooled, variance-ranked.

Both feed each other: findings from the harness inform new scenarios AND new
variants every generation. Runs until the wall-clock budget expires or the
variant population plateaus with no new informative scenarios arriving.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import AsyncGenerator, Dict, Any, List, Optional

from .fetcher import HookFetcher
from .mutator import HookMutator, LLMMutator
from .harness import FoundryHarness, MockHarness, build_harness
from .scorer import Scorer
from .exporter import VaultExporter
from .llm import build_llm
from .scenario import ScenarioPool, ScenarioProposer
from .knowledge import KnowledgeGraph
from .reporter import ReACTReporter


WALL_BUDGET_SECONDS = float(os.getenv("PN_WALL_BUDGET", "600"))
MAX_CONCURRENT_VARIANTS = int(os.getenv("PN_MAX_CONCURRENCY", "12"))
SKILL_MD_CAP_BYTES = 20 * 1024
SEED_RING_SIZE = 32

# Scenario generation controls
# Kept small: each LLM call asks for BATCH_SIZE scenarios at a time so the
# model can actually complete all of them within the num_predict budget.
SEED_SCENARIO_COUNT = int(os.getenv("PN_SEED_SCENARIOS", "8"))
PER_GEN_SCENARIO_COUNT = int(os.getenv("PN_PER_GEN_SCENARIOS", "4"))
MAX_ACTIVE_SCENARIOS = int(os.getenv("PN_MAX_SCENARIOS", "128"))


class HookEvaluator:
    def __init__(self):
        self.fetcher = HookFetcher()
        self.mutator = HookMutator()
        self.scorer = Scorer()
        self.exporter = VaultExporter()
        self.llm = build_llm()
        self.llm_mutator = LLMMutator(self.llm)
        self.knowledge = KnowledgeGraph()
        self.reporter = ReACTReporter(self.llm)

    async def analyze(
        self,
        github_url: str,
        num_agents: int = 6,
        skill_md: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        start = time.monotonic()
        deadline = start + WALL_BUDGET_SECONDS
        seed_ring: deque[str] = deque(maxlen=SEED_RING_SIZE)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_VARIANTS)

        if skill_md:
            skill_md = skill_md[:SKILL_MD_CAP_BYTES]
            yield {"type": "status", "message": f"Loaded skill.md ({len(skill_md)} chars) — using as research seed."}

        try:
            # Load prior knowledge for this hook before fetching
            prior_ctx = self.knowledge.get_prior_context(github_url, patterns=[])
            if prior_ctx:
                yield {"type": "status", "message": f"Knowledge graph: {self.knowledge.total_runs()} prior runs loaded."}

            yield {"type": "status", "message": "Fetching hook source..."}
            hook_source = await self.fetcher.fetch(github_url)
            yield {"type": "status", "message": f"Fetched: {self.fetcher.last_filename}"}

            workspace = self.fetcher.last_workspace
            harness = build_harness(workspace)
            yield {"type": "status", "message": f"Harness: {harness.mode}"
                   + (" (real Foundry + V4 stack)" if harness.mode == "foundry"
                      else " (image 'probably-nothing-foundry' not found — falling back to content-hashed stubs)")}

            # Scenario pool — seeded from Baseline + any human-promoted scenarios from prior vaults.
            pool = ScenarioPool(workspace) if workspace else None
            proposer: Optional[ScenarioProposer] = None
            if pool is not None:
                pool.register_existing_baseline()
                # Milestone D: pick up author:human scenarios compounded from prior runs.
                human_items = self.exporter.load_human_scenarios(github_url)
                if human_items:
                    installed = pool.add_human_scenarios(human_items)
                    yield {"type": "status", "message": f"Loaded {installed} author:human scenarios from prior vault."}

                proposer = ScenarioProposer(self.llm, workspace, pool)

                if harness.mode == "foundry":
                    yield {"type": "status", "message": "Loading V4 security reference from uniswap-ai..."}
                    await proposer._ensure_security_context()
                    ctx_size = len(proposer._security_context or "")
                    yield {"type": "status", "message": f"Security context: {ctx_size:,} chars loaded." if ctx_size else "Security context: unavailable (offline?), continuing without."}
                    # Inject prior knowledge into seed proposals
                    prior_ctx = self.knowledge.get_prior_context(github_url, patterns=[])
                    seed_skill = "\n\n".join(filter(None, [skill_md, prior_ctx]))
                    yield {"type": "status", "message": f"Seeding scenario pool — proposing {SEED_SCENARIO_COUNT} scenarios..."}
                    seed, seed_rejects = await proposer.propose_batch(
                        hook_source, count=SEED_SCENARIO_COUNT, gen=0,
                        recent_findings=[], skill_md=seed_skill or None,
                        timeout=min(180.0, max(5.0, deadline - time.monotonic())),
                    )
                    for s in seed:
                        yield {"type": "scenario_added", "scenario_id": s.scenario_id,
                               "contract": s.contract_name, "proposer": s.proposer, "gen": 0}
                    for r in seed_rejects:
                        yield {"type": "scenario_rejected", "reason": r}
                    yield {"type": "status", "message": f"Scenario pool seeded: {len(pool.active())} active ({len(seed_rejects)} rejected)."}

            # Spawn variant agents (visual + role assignment).
            agent_roles = self._assign_roles(num_agents)
            for agent in agent_roles:
                await asyncio.sleep(0.3)
                yield {"type": "agent_spawn", "agent_id": agent["id"],
                       "label": agent["label"], "direction": agent["direction"]}

            yield {"type": "status", "message": "Starting parametric mutations..."}
            params = self.mutator.extract_params(hook_source)
            population = self.mutator.parametric_variants(hook_source, params,
                                                          count=num_agents, agents=agent_roles)

            generation = 0
            all_findings: List[Dict[str, Any]] = []
            best_score = 0.0
            best_source = hook_source
            tier = "parametric"
            llm_attempts = 0
            stagnation = 0
            # Agent memory: rolling list of unique findings per agent archetype
            agent_memories: Dict[str, List[str]] = {a["id"]: [] for a in agent_roles}
            scored: List[Dict[str, Any]] = []

            while True:
                if time.monotonic() >= deadline:
                    yield {"type": "status", "message": "Wall-clock budget exhausted — stopping."}
                    break

                generation += 1
                yield {"type": "generation_start", "gen": generation,
                       "population": len(population), "tier": tier,
                       "scenarios": len(pool.active()) if pool else 0}

                # Variant work announcements
                for i, _ in enumerate(population):
                    a = agent_roles[i % len(agent_roles)]
                    yield {"type": "variant_start", "agent_id": a["id"], "label": a["label"],
                           "variant_index": i, "gen": generation, "tier": tier}

                active_scenarios = [
                    {"contract": s.contract_name, "scenario_id": s.scenario_id}
                    for s in (pool.active() if pool else [])
                ]

                async def _run(source: str, agent: dict, idx: int):
                    async with semaphore:
                        r = await harness.test(source, agent, scenarios=active_scenarios)
                        r["variant_index"] = idx
                        return r

                tasks = [asyncio.create_task(_run(v, agent_roles[i % len(agent_roles)], i))
                         for i, v in enumerate(population)]

                scored = []
                for coro in asyncio.as_completed(tasks):
                    try:
                        result = await coro
                    except Exception:
                        continue
                    score = self.scorer.score(result["metrics"])
                    result["score"] = score
                    scored.append(result)

                    # Feed per-scenario gas back into the pool so variance rankings update.
                    if pool is not None:
                        per = result["metrics"].get("per_scenario", {}) or {}
                        for key, rec in per.items():
                            contract = key.split("::")[0] if "::" in key else key
                            contract = contract.rsplit("/", 1)[-1].replace(".t.sol", "")
                            sid = self._resolve_sid(pool, contract)
                            if sid:
                                pool.record_result(
                                    sid, int(rec.get("gas", 0)),
                                    1 if rec.get("status") == "success" else 0,
                                    1 if rec.get("status") == "failure" else 0,
                                )

                    yield {
                        "type": "variant_complete",
                        "agent_id": result["agent_id"],
                        "variant_index": result.get("variant_index"),
                        "gen": generation, "tier": tier,
                        "score": round(score, 4),
                        "gas_used": result["metrics"].get("gas_used"),
                        "tests_passed": result["metrics"].get("tests_passed"),
                        "tests_failed": result["metrics"].get("tests_failed"),
                        "mode": result["metrics"].get("mode"),
                    }

                    aid = result["agent_id"]
                    for finding in result.get("findings", []):
                        if finding in seed_ring:
                            continue  # already surfaced — suppress re-confirmation noise
                        seed_ring.append(finding)
                        # Accumulate into agent memory for LLM tier context
                        mem = agent_memories.setdefault(aid, [])
                        if finding not in mem:
                            mem.append(finding)
                        record = {"agent_id": aid, "text": finding,
                                  "score": score, "generation": generation}
                        all_findings.append(record)
                        yield {"type": "finding", "agent_id": aid,
                               "text": finding,
                               "score_delta": round(score - best_score, 4),
                               "total_findings": len(all_findings)}

                    if time.monotonic() >= deadline:
                        break

                if not scored:
                    break

                scored.sort(key=lambda x: x["score"], reverse=True)
                gen_best = scored[0]["score"]
                if gen_best > best_score:
                    best_source = scored[0]["source"]

                yield {"type": "generation_complete", "gen": generation,
                       "best_score": round(gen_best, 4),
                       "variants_tested": len(scored), "tier": tier,
                       "scenarios": len(pool.active()) if pool else 0}

                improvement = gen_best - best_score
                best_score = max(best_score, gen_best)
                # Detect population collapse: all variants scored identically — parametric
                # mutations produced no differentiation, no point running more gens.
                score_spread = max(x["score"] for x in scored) - min(x["score"] for x in scored)
                # Stagnation: increment once per gen if population collapsed OR no improvement
                if score_spread < 0.001 or improvement < 0.01:
                    stagnation += 1
                else:
                    stagnation = 0

                # Milestone C: propose new scenarios every gen, prune stale ones.
                if proposer is not None and harness.mode == "foundry" and time.monotonic() < deadline - 30:
                    recent_texts = [f["text"] for f in all_findings[-40:]]
                    yield {"type": "status",
                           "message": f"Proposing {PER_GEN_SCENARIO_COUNT} scenarios for gen {generation + 1}..."}
                    new_scenarios, new_rejects = await proposer.propose_batch(
                        best_source, count=PER_GEN_SCENARIO_COUNT, gen=generation,
                        recent_findings=recent_texts, skill_md=skill_md,
                        timeout=min(120.0, max(5.0, deadline - time.monotonic() - 10)),
                    )
                    for s in new_scenarios:
                        yield {"type": "scenario_added", "scenario_id": s.scenario_id,
                               "contract": s.contract_name, "proposer": s.proposer, "gen": generation}
                    for r in new_rejects:
                        yield {"type": "scenario_rejected", "reason": r}
                    dropped = pool.prune(keep_top_k=MAX_ACTIVE_SCENARIOS)
                    for sid in dropped:
                        yield {"type": "scenario_pruned", "scenario_id": sid}

                # Variant plateau handling — escalate to LLM mutator (as before).
                if stagnation >= 3:
                    if tier == "parametric" and llm_attempts == 0:
                        tier = "llm"
                        llm_attempts += 1
                        remaining = max(0.0, deadline - time.monotonic())
                        llm_timeout = min(180.0, remaining)
                        yield {"type": "status", "message": f"Parametric tier converged. Requesting LLM-assisted mutations (budget={remaining:.0f}s, timeout={llm_timeout:.0f}s)..."}
                        if llm_timeout < 5.0:
                            yield {"type": "status", "message": "No budget left for LLM tier — stopping."}
                            break
                        # Flatten agent memories into a cross-agent context for the mutator
                        all_agent_memory = [
                            f"[{aid}] {finding}"
                            for aid, memories in agent_memories.items()
                            for finding in memories[-5:]  # last 5 per agent
                        ]
                        variant = await self.llm_mutator.propose(
                            best_source=best_source,
                            recent_findings=[f["text"] for f in all_findings],
                            agent_memory=all_agent_memory,
                            skill_md=skill_md, timeout=llm_timeout,
                        )
                        if not variant:
                            yield {"type": "status", "message": "LLM declined — stopping."}
                            break
                        population = [best_source, variant] + self.mutator.parametric_variants(
                            variant, self.mutator.extract_params(variant),
                            count=max(1, num_agents - 2), agents=agent_roles,
                        )
                        stagnation = 0
                        continue
                    else:
                        yield {"type": "status", "message": "Converged. Stopping."}
                        break

                survivors = scored[: max(1, len(scored) // 5)]
                population = [s["source"] for s in survivors]
                population += self.mutator.parametric_variants(
                    survivors[0]["source"], params,
                    count=max(0, num_agents - len(survivors)), agents=agent_roles,
                )

            # Generate ReACT security narrative
            report_md: Optional[str] = None
            vuln_catalog = proposer._security_context if proposer else None
            if vuln_catalog and time.monotonic() < deadline - 20:
                yield {"type": "status", "message": "Synthesising security report..."}
                hook_name = github_url.rstrip("/").split("/")[-1]
                report_md = await self.reporter.generate(
                    hook_name=hook_name,
                    hook_source=hook_source,
                    best_source=best_source,
                    findings=all_findings,
                    scenarios=(pool.all() if pool else []),
                    vuln_catalog=vuln_catalog,
                    timeout=min(120.0, deadline - time.monotonic()),
                )
                if report_md:
                    yield {"type": "status", "message": "Security report synthesised."}

            # Persist cross-run learning
            finding_texts = [f["text"] for f in all_findings if isinstance(f, dict)]
            detected_patterns = list(self.mutator.extract_params(hook_source).keys())
            scenario_stats = {
                s.contract_name: {
                    "runs": len(s.gas_samples),
                    "pass": sum(s.pass_samples),
                    "fail": sum(s.fail_samples),
                    "gas_samples": s.gas_samples[-10:],
                }
                for s in (pool.all() if pool else [])
            }
            self.knowledge.record_run(github_url, detected_patterns, finding_texts, best_score, scenario_stats)
            self.knowledge.save()

            yield {"type": "status", "message": "Generating Obsidian vault..."}
            vault_url = await self.exporter.export(
                scored, all_findings, github_url,
                scenarios=(pool.all() if pool else []),
                report_md=report_md,
            )

            elapsed = time.monotonic() - start
            yield {
                "type": "complete",
                "total_findings": len(all_findings),
                "best_score": round(best_score, 4),
                "generations": generation,
                "elapsed_seconds": round(elapsed, 2),
                "llm_backend": self.llm.backend,
                "llm_model": self.llm.model,
                "harness_mode": harness.mode,
                "scenarios_active": len(pool.active()) if pool else 0,
                "vault_url": vault_url,
            }

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    @staticmethod
    def _resolve_sid(pool: ScenarioPool, contract_name: str) -> Optional[str]:
        for s in pool.all():
            if s.contract_name == contract_name:
                return s.scenario_id
        return None

    def _assign_roles(self, num_agents: int):
        archetypes = [
            {"id": "gas-optimizer",    "label": "Gas Optimizer",    "direction": "top"},
            {"id": "mev-sentinel",     "label": "MEV Sentinel",     "direction": "right"},
            {"id": "lp-deployer",      "label": "LP Deployer",      "direction": "bottom"},
            {"id": "swap-scenario",    "label": "Swap Scenario",    "direction": "left"},
            {"id": "edge-case-hunter", "label": "Edge Case Hunter", "direction": "top"},
            {"id": "security-auditor", "label": "Security Auditor", "direction": "right"},
        ]
        agents = []
        for i in range(num_agents):
            base = archetypes[i % len(archetypes)].copy()
            base["id"] = f"{base['id']}-{i+1}" if num_agents > 6 else base["id"]
            agents.append(base)
        return agents
