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


WALL_BUDGET_SECONDS = float(os.getenv("PN_WALL_BUDGET", "300"))
MAX_CONCURRENT_VARIANTS = int(os.getenv("PN_MAX_CONCURRENCY", "12"))
SKILL_MD_CAP_BYTES = 20 * 1024
SEED_RING_SIZE = 32

# Scenario generation controls
SEED_SCENARIO_COUNT = int(os.getenv("PN_SEED_SCENARIOS", "20"))
PER_GEN_SCENARIO_COUNT = int(os.getenv("PN_PER_GEN_SCENARIOS", "8"))
MAX_ACTIVE_SCENARIOS = int(os.getenv("PN_MAX_SCENARIOS", "128"))


class HookEvaluator:
    def __init__(self):
        self.fetcher = HookFetcher()
        self.mutator = HookMutator()
        self.scorer = Scorer()
        self.exporter = VaultExporter()
        self.llm = build_llm()
        self.llm_mutator = LLMMutator(self.llm)

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

                if harness.mode == "foundry" and self.llm.backend != "mock":
                    yield {"type": "status", "message": f"Seeding scenario pool — proposing {SEED_SCENARIO_COUNT} scenarios..."}
                    seed = await proposer.propose_batch(
                        hook_source, count=SEED_SCENARIO_COUNT, gen=0,
                        recent_findings=[], skill_md=skill_md,
                        timeout=min(180.0, max(5.0, deadline - time.monotonic())),
                    )
                    for s in seed:
                        yield {"type": "scenario_added", "scenario_id": s.scenario_id,
                               "contract": s.contract_name, "proposer": s.proposer, "gen": 0}
                    yield {"type": "status", "message": f"Scenario pool seeded: {len(pool.active())} active."}

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

                    for finding in result.get("findings", []):
                        prefix = "Re-checking: " if finding in seed_ring else ""
                        text = f"{prefix}{finding}" + (" → confirmed" if prefix else "")
                        record = {"agent_id": result["agent_id"], "text": text,
                                  "score": score, "generation": generation}
                        all_findings.append(record)
                        seed_ring.append(finding)
                        yield {"type": "finding", "agent_id": result["agent_id"],
                               "text": text,
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
                stagnation = stagnation + 1 if improvement < 0.01 else 0

                # Milestone C: propose new scenarios every gen, prune stale ones.
                if proposer is not None and harness.mode == "foundry" and time.monotonic() < deadline - 30:
                    recent_texts = [f["text"] for f in all_findings[-40:]]
                    yield {"type": "status",
                           "message": f"Proposing {PER_GEN_SCENARIO_COUNT} scenarios for gen {generation + 1}..."}
                    new_scenarios = await proposer.propose_batch(
                        best_source, count=PER_GEN_SCENARIO_COUNT, gen=generation,
                        recent_findings=recent_texts, skill_md=skill_md,
                        timeout=min(120.0, max(5.0, deadline - time.monotonic() - 10)),
                    )
                    for s in new_scenarios:
                        yield {"type": "scenario_added", "scenario_id": s.scenario_id,
                               "contract": s.contract_name, "proposer": s.proposer, "gen": generation}
                    dropped = pool.prune(keep_top_k=MAX_ACTIVE_SCENARIOS)
                    for sid in dropped:
                        yield {"type": "scenario_pruned", "scenario_id": sid}

                # Variant plateau handling — escalate to LLM mutator (as before).
                if stagnation >= 3:
                    if tier == "parametric" and llm_attempts == 0:
                        tier = "llm"
                        llm_attempts += 1
                        yield {"type": "status", "message": "Parametric tier converged. Requesting LLM-assisted mutations..."}
                        remaining = max(0.0, deadline - time.monotonic())
                        llm_timeout = min(60.0, remaining)
                        if llm_timeout < 5.0:
                            yield {"type": "status", "message": "No budget left for LLM tier — stopping."}
                            break
                        variant = await self.llm_mutator.propose(
                            best_source=best_source,
                            recent_findings=[f["text"] for f in all_findings],
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

            yield {"type": "status", "message": "Generating Obsidian vault..."}
            vault_url = await self.exporter.export(
                scored, all_findings, github_url,
                scenarios=(pool.all() if pool else []),
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
