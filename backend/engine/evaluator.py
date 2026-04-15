"""
Probably Nothing — Core Evaluation Engine
Autoresearch loop: parametric → structural → LLM-assisted mutations
"""
import asyncio
from typing import AsyncGenerator, Dict, Any
from .fetcher import HookFetcher
from .mutator import HookMutator
from .harness import DockerHarness
from .scorer import Scorer
from .exporter import VaultExporter

class HookEvaluator:
    def __init__(self):
        self.fetcher = HookFetcher()
        self.mutator = HookMutator()
        self.harness = DockerHarness()
        self.scorer = Scorer()
        self.exporter = VaultExporter()

    async def analyze(self, github_url: str, num_agents: int = 6) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Main autoresearch loop.
        Streams WebSocket events to frontend as work progresses.
        """
        try:
            # Stage 1: Fetch
            yield {"type": "status", "message": "Fetching hook source..."}
            hook_source = await self.fetcher.fetch(github_url)
            yield {"type": "status", "message": f"Fetched: {self.fetcher.last_filename}"}

            # Stage 2: Spawn agents
            agent_roles = self._assign_roles(num_agents)
            for i, agent in enumerate(agent_roles):
                await asyncio.sleep(0.3)
                yield {
                    "type": "agent_spawn",
                    "agent_id": agent["id"],
                    "label": agent["label"],
                    "direction": agent["direction"]
                }

            # Stage 3: Parametric mutation loop
            yield {"type": "status", "message": "Starting parametric mutations..."}
            params = self.mutator.extract_params(hook_source)
            variants = self.mutator.parametric_variants(hook_source, params, count=num_agents)

            generation = 0
            all_findings = []
            best_score = 0.0
            population = variants

            while True:
                generation += 1
                yield {"type": "generation_start", "gen": generation, "population": len(population)}

                # Test all variants in parallel
                tasks = [self.harness.test(v, agent_roles[i % len(agent_roles)]) for i, v in enumerate(population)]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Emit findings as they score
                scored = []
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    score = self.scorer.score(result["metrics"])
                    result["score"] = score
                    scored.append(result)
                    for finding in result.get("findings", []):
                        all_findings.append(finding)
                        yield {
                            "type": "finding",
                            "agent_id": result["agent_id"],
                            "text": finding,
                            "score_delta": round(score - best_score, 4),
                            "total_findings": len(all_findings)
                        }
                        await asyncio.sleep(0.1)

                if not scored:
                    break

                scored.sort(key=lambda x: x["score"], reverse=True)
                gen_best = scored[0]["score"]

                yield {
                    "type": "generation_complete",
                    "gen": generation,
                    "best_score": gen_best,
                    "variants_tested": len(scored)
                }

                # Convergence check: < 1% improvement for 3 generations
                improvement = gen_best - best_score
                if improvement < 0.01 and generation > 3:
                    yield {"type": "status", "message": "Converged. Escalating mutation tier..."}
                    # TODO: escalate to structural, then LLM-assisted
                    break

                best_score = gen_best

                # Evolve: top 20% survive, rest mutate
                survivors = scored[:max(1, len(scored) // 5)]
                population = [s["source"] for s in survivors]
                population += self.mutator.parametric_variants(survivors[0]["source"], params, count=num_agents - len(survivors))

            # Stage 4: Export vault
            yield {"type": "status", "message": "Generating Obsidian vault..."}
            vault_url = await self.exporter.export(scored, all_findings, github_url)

            yield {
                "type": "complete",
                "total_findings": len(all_findings),
                "best_score": round(best_score, 4),
                "generations": generation,
                "vault_url": vault_url
            }

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    def _assign_roles(self, num_agents: int):
        """Assign roles to N agents, cycling through 6 archetypes."""
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
