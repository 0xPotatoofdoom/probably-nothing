"""
Probably Nothing — Ecosystem Persona Swarm Evaluator

A hook lifecycle simulation: 9 ecosystem personas each generate and run
test scenarios against the hook from their real-world perspective.

The hook is IMMUTABLE — we test it, not rewrite it.

Each persona represents a participant the hook will encounter after deployment:
router aggregators, MEV searchers, LP whales, retail traders, bridge integrators,
security auditors, DEX listing teams, BD integrators, and high-frequency gas users.

Output is a coverage matrix: "Router: 6/9 pass | Security: 3/7 pass | ..."
Personas with failures get follow-up scenario rounds so the swarm compounds.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncGenerator, Dict, Any, List, Optional

from .fetcher import HookFetcher
from .harness import build_harness
from .exporter import VaultExporter
from .llm import build_llm
from .scenario import ScenarioPool, ScenarioProposer
from .knowledge import KnowledgeGraph
from .reporter import ReACTReporter
from .persona import PERSONAS, PersonaDef


WALL_BUDGET_SECONDS = float(os.getenv("PN_WALL_BUDGET", "900"))
SKILL_MD_CAP_BYTES = 20 * 1024
SCENARIOS_PER_PERSONA = int(os.getenv("PN_SCENARIOS_PER_PERSONA", "2"))
FOLLOWUP_SCENARIOS = int(os.getenv("PN_FOLLOWUP_SCENARIOS", "2"))
FOLLOWUP_THRESHOLD = float(os.getenv("PN_FOLLOWUP_THRESHOLD", "0.5"))
MAX_FOLLOWUP_ROUNDS = int(os.getenv("PN_FOLLOWUP_ROUNDS", "2"))
MAX_ACTIVE_SCENARIOS = int(os.getenv("PN_MAX_SCENARIOS", "128"))


class HookEvaluator:
    def __init__(self):
        self.fetcher = HookFetcher()
        self.exporter = VaultExporter()
        self.llm = build_llm()
        self.knowledge = KnowledgeGraph()
        self.reporter = ReACTReporter(self.llm)

    async def analyze(
        self,
        github_url: str,
        num_agents: int = 6,   # kept for API compat, ignored (persona count is fixed)
        skill_md: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        start = time.monotonic()
        deadline = start + WALL_BUDGET_SECONDS

        if skill_md:
            skill_md = skill_md[:SKILL_MD_CAP_BYTES]
            yield {"type": "status", "message": f"Loaded skill.md ({len(skill_md)} chars)."}

        try:
            prior_runs = self.knowledge.total_runs()
            if prior_runs:
                yield {"type": "status", "message": f"Knowledge graph: {prior_runs} prior runs loaded."}

            yield {"type": "status", "message": "Fetching hook source..."}
            hook_source = await self.fetcher.fetch(github_url)
            yield {"type": "status", "message": f"Fetched: {self.fetcher.last_filename}"}

            workspace = self.fetcher.last_workspace
            harness = build_harness(workspace)
            yield {"type": "status", "message": f"Harness: {harness.mode}"
                   + (" (real Foundry + V4 stack)" if harness.mode == "foundry"
                      else " (image not found — using stub harness)")}

            # Scenario pool — seeded from Baseline + human-promoted scenarios from prior vaults.
            pool = ScenarioPool(workspace) if workspace else None
            proposer: Optional[ScenarioProposer] = None
            if pool is not None:
                pool.register_existing_baseline()
                human_items = self.exporter.load_human_scenarios(github_url)
                if human_items:
                    installed = pool.add_human_scenarios(human_items)
                    yield {"type": "status", "message": f"Loaded {installed} author:human scenarios from prior vault."}

                proposer = ScenarioProposer(self.llm, workspace, pool)

                if harness.mode == "foundry":
                    yield {"type": "status", "message": "Loading V4 security reference from uniswap-ai + ethskills..."}
                    await proposer._ensure_security_context()
                    ctx_size = len(proposer._security_context or "")
                    yield {"type": "status",
                           "message": f"Security context: {ctx_size:,} chars loaded."
                           if ctx_size else "Security context: unavailable, continuing without."}
                    # Merge security context into skill_md so it reaches every persona prompt
                    if proposer._security_context:
                        skill_md = "\n\n".join(filter(None, [skill_md, proposer._security_context]))[:SKILL_MD_CAP_BYTES] or None

            # Spawn persona agents (visual)
            for persona in PERSONAS:
                await asyncio.sleep(0.2)
                yield {"type": "agent_spawn", "agent_id": persona.id,
                       "label": persona.label, "direction": persona.direction}

            # ── Phase 1: Seed — each persona proposes its initial scenarios ────────
            all_persona_failures: Dict[str, List[str]] = {p.id: [] for p in PERSONAS}

            if proposer is not None and harness.mode == "foundry":
                seed_timeout = min(240.0, max(30.0, deadline - time.monotonic() - 60))
                for persona in PERSONAS:
                    yield {"type": "status",
                           "message": f"[{persona.id}] Seeding {SCENARIOS_PER_PERSONA} scenarios..."}

                async def _seed_one(p: "PersonaDef") -> "tuple[PersonaDef, list, list]":
                    ns, rj = await proposer.propose_for_persona(
                        hook_source, p,
                        count=SCENARIOS_PER_PERSONA,
                        recent_findings=[],
                        skill_md=skill_md,
                        timeout=seed_timeout,
                    )
                    return p, ns, rj

                seed_results = await asyncio.gather(
                    *[_seed_one(p) for p in PERSONAS],
                    return_exceptions=True,
                )
                for item in seed_results:
                    if isinstance(item, BaseException):
                        yield {"type": "status", "message": f"  seed error: {item}"}
                        continue
                    persona, new_s, rejects = item
                    for s in new_s:
                        yield {"type": "scenario_added", "scenario_id": s.scenario_id,
                               "contract": s.contract_name, "persona_id": persona.id}
                    for r in rejects:
                        yield {"type": "scenario_rejected", "reason": r}

                total_seeded = len(pool.active()) if pool else 0
                yield {"type": "status", "message": f"Seeded {total_seeded} scenarios across {len(PERSONAS)} personas."}

            # ── Phase 2: Run all scenarios against the original hook ──────────────
            all_findings: List[Dict[str, Any]] = []
            coverage: Dict[str, Any] = {p.id: {"label": p.label, "passed": 0, "failed": 0,
                                                 "total": 0, "pass_rate": 0.0, "failures": []}
                                          for p in PERSONAS}

            if pool is not None:
                active_scenarios = [
                    {"contract": s.contract_name, "scenario_id": s.scenario_id}
                    for s in pool.active()
                ]
                if active_scenarios:
                    yield {"type": "status",
                           "message": f"Running {len(active_scenarios)} scenarios against hook..."}
                    result = await harness.test(
                        hook_source,
                        {"id": "persona-runner", "label": "Persona Runner"},
                        scenarios=active_scenarios,
                    )
                    raw_per = result["metrics"].get("per_scenario", {})
                    yield {"type": "status",
                           "message": f"Forge returned {len(raw_per)} test results, {result['metrics'].get('tests_passed',0)} passed, {result['metrics'].get('tests_failed',0)} failed"}
                    if result.get("findings"):
                        for f in result["findings"][:3]:
                            yield {"type": "status", "message": f"  harness: {f}"}
                    coverage = self._build_coverage_matrix(raw_per, pool)
                    # Surface findings from coverage failures
                    for pid, data in coverage.items():
                        for failure in data["failures"]:
                            finding = {"agent_id": pid, "text": failure["text"],
                                       "persona_id": pid, "generation": 1}
                            all_findings.append(finding)
                            all_persona_failures[pid].append(failure["text"])
                            yield {"type": "finding", "agent_id": pid,
                                   "text": failure["text"], "persona_id": pid}
                    yield {"type": "coverage_matrix", "coverage": _coverage_summary(coverage)}

            # ── Phase 3: Follow-up rounds for failing personas ────────────────────
            for round_num in range(1, MAX_FOLLOWUP_ROUNDS + 1):
                if time.monotonic() >= deadline - 60:
                    break
                if proposer is None or harness.mode != "foundry":
                    break

                weak = [p for p in PERSONAS
                        if coverage[p.id]["total"] > 0
                        and coverage[p.id]["pass_rate"] < FOLLOWUP_THRESHOLD]
                if not weak:
                    yield {"type": "status", "message": "All personas above threshold."}
                    break

                yield {"type": "status",
                       "message": f"Follow-up round {round_num}: {len(weak)} personas below {FOLLOWUP_THRESHOLD:.0%} pass rate."}
                new_all: List = []
                for persona in weak:
                    if time.monotonic() >= deadline - 45:
                        break
                    recent = all_persona_failures.get(persona.id, [])[-8:]
                    new_s, rejects = await proposer.propose_for_persona(
                        hook_source, persona,
                        count=FOLLOWUP_SCENARIOS,
                        recent_findings=recent,
                        skill_md=skill_md,
                        timeout=min(120.0, max(15.0, deadline - time.monotonic() - 30)),
                    )
                    for s in new_s:
                        yield {"type": "scenario_added", "scenario_id": s.scenario_id,
                               "contract": s.contract_name, "persona_id": persona.id}
                        new_all.append(s)
                    for r in rejects:
                        yield {"type": "scenario_rejected", "reason": r}

                if new_all and time.monotonic() < deadline - 30:
                    followup_active = [{"contract": s.contract_name, "scenario_id": s.scenario_id}
                                       for s in new_all]
                    result = await harness.test(
                        hook_source,
                        {"id": "persona-runner", "label": "Persona Runner"},
                        scenarios=followup_active,
                    )
                    new_cov = self._build_coverage_matrix(
                        result["metrics"].get("per_scenario", {}), pool
                    )
                    coverage = _merge_coverage(coverage, new_cov)
                    for pid, data in new_cov.items():
                        for failure in data["failures"]:
                            finding = {"agent_id": pid, "text": failure["text"],
                                       "persona_id": pid, "generation": round_num + 1}
                            all_findings.append(finding)
                            all_persona_failures[pid].append(failure["text"])
                            yield {"type": "finding", "agent_id": pid,
                                   "text": failure["text"], "persona_id": pid}
                    yield {"type": "coverage_update",
                           "coverage": _coverage_summary(coverage), "round": round_num}

            # ── Report + Export ────────────────────────────────────────────────────
            report_md: Optional[str] = None
            if time.monotonic() < deadline - 20:
                yield {"type": "status", "message": "Synthesising ecosystem coverage report..."}
                hook_name = github_url.rstrip("/").split("/")[-1]
                report_md = await self.reporter.generate(
                    hook_name=hook_name,
                    hook_source=hook_source,
                    coverage=coverage,
                    personas=PERSONAS,
                    timeout=min(120.0, deadline - time.monotonic()),
                )
                if report_md:
                    yield {"type": "status", "message": "Coverage report synthesised."}

            # Persist cross-run learning
            scenario_stats = {
                s.contract_name: {
                    "runs": len(s.gas_samples),
                    "pass": sum(s.pass_samples),
                    "fail": sum(s.fail_samples),
                    "gas_samples": s.gas_samples[-10:],
                }
                for s in (pool.all() if pool else [])
            }
            finding_texts = [f["text"] for f in all_findings if isinstance(f, dict)]
            self.knowledge.record_run(github_url, [], finding_texts, 0.0, scenario_stats)
            self.knowledge.save()

            yield {"type": "status", "message": "Generating Obsidian vault..."}
            vault_url = await self.exporter.export(
                hook_source=hook_source,
                github_url=github_url,
                coverage=coverage,
                personas=PERSONAS,
                scenarios=(pool.all() if pool else []),
                report_md=report_md,
            )

            elapsed = time.monotonic() - start
            total_pass = sum(c["passed"] for c in coverage.values())
            total_tests = sum(c["total"] for c in coverage.values())
            yield {
                "type": "complete",
                "total_findings": len(all_findings),
                "total_scenarios": total_tests,
                "total_passed": total_pass,
                "coverage": _coverage_summary(coverage),
                "elapsed_seconds": round(elapsed, 2),
                "llm_backend": self.llm.backend,
                "llm_model": self.llm.model,
                "harness_mode": harness.mode,
                "vault_url": vault_url,
            }

        except Exception as e:
            import traceback
            yield {"type": "error", "message": str(e), "traceback": traceback.format_exc()}

    def _build_coverage_matrix(
        self,
        per_scenario: Dict[str, Dict],
        pool: ScenarioPool,
    ) -> Dict[str, Any]:
        matrix = {
            p.id: {"label": p.label, "passed": 0, "failed": 0,
                   "total": 0, "pass_rate": 0.0, "failures": []}
            for p in PERSONAS
        }
        for key, rec in per_scenario.items():
            # key looks like "test/scenarios/Scenario_Foo.t.sol:Scenario_Foo::test_Bar"
            # .split(".t.sol")[0] gives "Scenario_Foo" regardless of whether forge
            # appends ":ContractName" after the filename.
            contract = key.split("::")[0].rsplit("/", 1)[-1].split(".t.sol")[0]
            scenario = pool.get_by_contract_name(contract)
            if scenario is None:
                continue
            pid = scenario.persona_id or "security-auditor"  # seed/baseline → security by default
            if pid not in matrix:
                continue
            matrix[pid]["total"] += 1
            gas = int(rec.get("gas", 0))
            if rec.get("status") == "success":
                matrix[pid]["passed"] += 1
                pool.record_result(scenario.scenario_id, gas, 1, 0)
            else:
                matrix[pid]["failed"] += 1
                pool.record_result(scenario.scenario_id, gas, 0, 1)
                matrix[pid]["failures"].append({
                    "test": key.split("::")[-1] if "::" in key else key,
                    "gas": gas,
                    "text": f"{contract}: {key.split('::')[-1] if '::' in key else 'failed'}",
                })
        for pid in matrix:
            t = matrix[pid]["total"]
            matrix[pid]["pass_rate"] = round(matrix[pid]["passed"] / t, 3) if t else 0.0
        return matrix


def _merge_coverage(base: Dict, new: Dict) -> Dict:
    """Add new coverage results into base (non-destructive merge)."""
    merged = {k: dict(v) for k, v in base.items()}
    for pid, data in new.items():
        if pid not in merged:
            merged[pid] = dict(data)
            continue
        merged[pid]["passed"] += data["passed"]
        merged[pid]["failed"] += data["failed"]
        merged[pid]["total"] += data["total"]
        merged[pid]["failures"] = merged[pid].get("failures", []) + data.get("failures", [])
        t = merged[pid]["total"]
        merged[pid]["pass_rate"] = round(merged[pid]["passed"] / t, 3) if t else 0.0
    return merged


def _coverage_summary(coverage: Dict) -> Dict[str, str]:
    """Compact summary for streaming events: {persona_id: "6/9 (66.7%)"}."""
    return {
        pid: f"{d['passed']}/{d['total']} ({d['pass_rate']:.1%})"
        for pid, d in coverage.items()
        if d["total"] > 0
    }
