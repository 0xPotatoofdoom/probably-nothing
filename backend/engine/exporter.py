"""
Obsidian Vault Exporter — Modified Karpathy Method
Two-author architecture: agent-authored findings + human-promotable synthesis
"""
import zipfile, json, os, tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

CLAUDE_MD = """# CLAUDE.md — Probably Nothing Vault

## The Two-Author Rule
- Files with `author: agent` may be updated, extended, or contradicted by future runs
- Files with `author: human` are READ ONLY. Never modify, never overwrite.
- When a new run contradicts an agent-authored file, set `stale: true` and add a contradiction note. Do not delete.

## Vault Operations
- `/research [angle]` — spin up agents across a new angle (contrarian, historical, edge-case focused)
- `/ingest [source]` — process a new source (URL, Solidity file, audit report)
- `/query [question]` — read indexes first, drill into articles, produce cited synthesis
- `/lint` — check for broken citations, stale markers, orphan pages
- `/export [type]` — generate audit brief, security report, or PR description from wiki content

## Folder Rules
- sources/ — immutable. Never write here.
- wiki/concepts/ — abstract patterns that apply across hooks
- wiki/entities/ — specific hook instances, variants, agents
- wiki/synthesis/ — cross-cutting conclusions. Human voice lives here.
- generations/ — historical record of the evolutionary search

## Compounding Protocol
After each re-run, review wiki/synthesis/recommendations.md.
If a recommendation represents your own conclusion, change `author: agent` to `author: human`.
That finding is now yours forever.
"""

def frontmatter(author: str, confidence: float, run_id: str, citations: list = None, contradicts: list = None) -> str:
    return f"""---
author: {author}
confidence: {confidence:.4f}
run_id: {run_id}
citations:
{chr(10).join(f'  - {c}' for c in (citations or []))}
contradicts: {json.dumps(contradicts or [])}
stale: false
---

"""

import hashlib
import re


def _slug_for_url(github_url: str) -> str:
    return hashlib.sha256(github_url.encode()).hexdigest()[:16]


_SCENARIO_FM = re.compile(
    r"^---\n(.*?)\n---\n(.*)$", re.DOTALL
)


class VaultExporter:
    # ── Re-run continuity (Milestone D) ──
    #
    # Vaults are written under `PN_VAULT_DIR/vault-<timestamp>/`. For a given
    # hook URL we also maintain `PN_VAULT_DIR/by-url/<sha>/` with symlinks to
    # every historical run of that URL. When a new run starts we scan the most
    # recent vault for `author: human` scenarios and lift them into the new
    # run so humans' promoted work carries across reruns.
    def load_human_scenarios(self, github_url: str) -> list:
        root = Path(os.getenv("PN_VAULT_DIR", "/tmp/probably-nothing-vaults"))
        link_dir = root / "by-url" / _slug_for_url(github_url)
        if not link_dir.exists():
            return []
        # Most recent first
        vaults = sorted(link_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        picked: list = []
        seen: set = set()
        for v in vaults:
            sdir = v / "wiki" / "scenarios"
            if not sdir.exists():
                continue
            for f in sdir.glob("*.md"):
                try:
                    text = f.read_text()
                except Exception:
                    continue
                m = _SCENARIO_FM.match(text)
                if not m:
                    continue
                frontmatter_block, body = m.group(1), m.group(2)
                if "author: human" not in frontmatter_block:
                    continue
                # Extract solidity from ```solidity fences in the body.
                fence = re.search(r"```solidity\s*(.*?)```", body, re.DOTALL | re.IGNORECASE)
                if not fence:
                    continue
                source = fence.group(1).strip()
                key = hashlib.sha256(source.encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                picked.append({"source": source, "source_file": str(f)})
        return picked

    async def export(self, results: list, findings: list, github_url: str,
                     scenarios: Optional[list] = None, run_id: str = None) -> str:
        if not run_id:
            import uuid as _uuid
            run_id = datetime.now().strftime("%Y-%m-%d-%H%M%S") + "-" + _uuid.uuid4().hex[:6]

        out_dir = Path(os.getenv("PN_VAULT_DIR", "/tmp/probably-nothing-vaults"))
        out_dir.mkdir(parents=True, exist_ok=True)
        vault_name = f"vault-{run_id}"
        vault_path = out_dir / vault_name
        vault_path.mkdir(exist_ok=True)

        best = results[0] if results else {}
        best_score = best.get("score", 0)

        # --- sources/ (immutable raw inputs) ---
        sources_dir = vault_path / "sources"
        sources_dir.mkdir(exist_ok=True)
        if best.get("source"):
            (sources_dir / "hook-source.sol").write_text(best["source"])
        (sources_dir / "run-metadata.json").write_text(json.dumps({
            "run_id": run_id,
            "github_url": github_url,
            "timestamp": datetime.now().isoformat(),
            "total_findings": len(findings),
            "best_score": best_score,
            "variants_tested": len(results)
        }, indent=2))

        # --- wiki/concepts/ ---
        concepts_dir = vault_path / "wiki" / "concepts"
        concepts_dir.mkdir(parents=True)

        gas_findings = [f for f in findings if "gas" in f.get("text", "").lower() or "Gas" in f.get("text", "")]
        mev_findings = [f for f in findings if "mev" in f.get("text", "").lower() or "MEV" in f.get("text", "") or "sandwich" in f.get("text", "").lower()]

        (concepts_dir / "gas-optimization.md").write_text(
            frontmatter("agent", best_score, run_id, citations=["sources/run-metadata.json"]) +
            f"# Gas Optimization Findings\n\n" +
            "\n".join(f"- {f.get('text', f)}" for f in gas_findings) or "- No gas anomalies detected\n"
        )
        (concepts_dir / "mev-resistance.md").write_text(
            frontmatter("agent", best_score, run_id, citations=["sources/run-metadata.json"]) +
            f"# MEV Resistance Findings\n\n" +
            "\n".join(f"- {f.get('text', f)}" for f in mev_findings) or "- No MEV vulnerabilities detected\n"
        )
        (concepts_dir / "hook-permissions.md").write_text(
            frontmatter("agent", best_score, run_id) +
            "# Hook Permission Analysis\n\nPermission flags identified during static analysis.\n"
        )

        # --- wiki/entities/ ---
        entities_dir = vault_path / "wiki" / "entities"
        entities_dir.mkdir(parents=True)

        hook_name = github_url.rstrip("/").split("/")[-1]
        (entities_dir / f"{hook_name}.md").write_text(
            frontmatter("agent", best_score, run_id,
                citations=["sources/hook-source.sol", "sources/run-metadata.json"]) +
            f"# {hook_name}\n\n**Source:** {github_url}\n**Best Score:** {best_score:.4f}\n**Findings:** {len(findings)}\n\n" +
            "## Key Findings\n" +
            "\n".join(f"- {f.get('text', f)}" for f in findings[:20])
        )

        # Per-agent entity files
        agents_dir = entities_dir / "agents"
        agents_dir.mkdir(exist_ok=True)
        agent_findings = {}
        for f in findings:
            aid = f.get("agent_id", "unknown") if isinstance(f, dict) else "unknown"
            agent_findings.setdefault(aid, []).append(f)

        agent_files = []
        for aid, afindings in agent_findings.items():
            fname = f"{aid}.md"
            agent_score = next((r["score"] for r in results if r.get("agent_id") == aid), best_score)
            (agents_dir / fname).write_text(
                frontmatter("agent", agent_score, run_id,
                    citations=[f"sources/run-metadata.json"]) +
                f"# Agent: {aid}\n\n**Score:** {agent_score:.4f}\n\n## Findings\n" +
                "\n".join(f"- {f.get('text', f) if isinstance(f, dict) else f}" for f in afindings)
            )
            agent_files.append(f"entities/agents/{fname}")

        # Variants
        variants_dir = entities_dir / "variants"
        variants_dir.mkdir(exist_ok=True)
        for i, r in enumerate(results[:10]):  # top 10
            (variants_dir / f"variant-{i+1:03d}.md").write_text(
                frontmatter("agent", r.get("score", 0), run_id,
                    citations=["sources/run-metadata.json"]) +
                f"# Variant {i+1}\n\n**Score:** {r.get('score', 0):.4f}\n\n## Findings\n" +
                "\n".join(f"- {f}" for f in r.get("findings", []))
            )

        # --- wiki/synthesis/ (where human promotion happens) ---
        synthesis_dir = vault_path / "wiki" / "synthesis"
        synthesis_dir.mkdir(exist_ok=True)

        all_finding_texts = [f.get("text", str(f)) if isinstance(f, dict) else str(f) for f in findings]

        (synthesis_dir / "overall-score.md").write_text(
            frontmatter("agent", best_score, run_id,
                citations=["sources/run-metadata.json"] + agent_files[:3]) +
            f"# Overall Score: {best_score:.4f}\n\n" +
            f"**Total findings:** {len(findings)}\n"
            f"**Variants tested:** {len(results)}\n"
            f"**Top finding:** {all_finding_texts[0] if all_finding_texts else 'None'}\n\n"
            "## Score Breakdown\n"
            "- Gas efficiency (40%): see wiki/concepts/gas-optimization.md\n"
            "- MEV resistance (30%): see wiki/concepts/mev-resistance.md\n"
            "- Liquidity quality (20%): data in agents/lp-deployer.md\n"
            "- Code simplicity (10%): data in sources/hook-source.sol\n"
        )

        (synthesis_dir / "recommendations.md").write_text(
            frontmatter("agent", best_score, run_id,
                citations=["wiki/synthesis/overall-score.md"]) +
            "# Recommendations\n\n"
            "> Review this file. For any recommendation that represents YOUR conclusion, "
            "change `author: agent` to `author: human` in the frontmatter. "
            "That finding is now yours and will never be overwritten by future runs.\n\n"
            "## Agent Recommendations\n\n" +
            "\n".join(f"- {t}" for t in all_finding_texts[:10])
        )

        # --- generations/ ---
        gen_dir = vault_path / "generations"
        gen_dir.mkdir(exist_ok=True)
        (gen_dir / "gen-001.md").write_text(
            frontmatter("agent", best_score, run_id) +
            f"# Generation 1\n\n**Best score:** {best_score:.4f}\n**Variants:** {len(results)}\n\n" +
            "## Survivors\n" +
            "\n".join(f"- Score {r.get('score', 0):.4f}: {r.get('agent_id', 'unknown')}" for r in results[:3])
        )

        # --- wiki/scenarios/ (Milestone D) ---
        # Every scenario the pool touched — LLM-authored, seed, or human-promoted — lands
        # here as a standalone .md file with a ```solidity fence. Authors can change
        # `author: agent` → `author: human` to make a scenario survive future re-runs.
        if scenarios:
            scen_dir = vault_path / "wiki" / "scenarios"
            scen_dir.mkdir(parents=True, exist_ok=True)
            for s in scenarios:
                author = "human" if getattr(s, "proposer", None) == "human" else "agent"
                informativeness = getattr(s, "informativeness", float("inf"))
                samples = len(getattr(s, "gas_samples", []) or [])
                body = (
                    frontmatter(author, best_score, run_id,
                                citations=["sources/hook-source.sol"]) +
                    f"# {s.contract_name}\n\n"
                    f"**Proposer:** {s.proposer}\n"
                    f"**Generation created:** {s.gen_created}\n"
                    f"**Samples:** {samples}\n"
                    f"**Informativeness (gas variance):** "
                    f"{'∞' if informativeness == float('inf') else f'{informativeness:.1f}'}\n"
                    f"**Failure rate:** {s.failure_rate:.2%}\n\n"
                    "## Source\n"
                    "```solidity\n"
                    f"{s.source}\n"
                    "```\n"
                )
                (scen_dir / f"{s.contract_name}.md").write_text(body)

        # --- best-variant/ ---
        best_dir = vault_path / "best-variant"
        best_dir.mkdir(exist_ok=True)
        if best.get("source"):
            (best_dir / "Hook.sol").write_text(best["source"])

        # --- CLAUDE.md ---
        (vault_path / "CLAUDE.md").write_text(CLAUDE_MD)

        # --- README ---
        (vault_path / "README.md").write_text(
            frontmatter("agent", best_score, run_id) +
            f"# Probably Nothing — Audit Vault\n\n"
            f"**Hook:** {github_url}\n"
            f"**Run:** {run_id}\n"
            f"**Best Score:** {best_score:.4f}\n"
            f"**Total Findings:** {len(findings)}\n\n"
            "## Structure\n"
            "- `sources/` — immutable raw inputs\n"
            "- `wiki/concepts/` — abstract patterns\n"
            "- `wiki/entities/` — hook + variant profiles\n"
            "- `wiki/synthesis/` — conclusions (promote to `author: human` after review)\n"
            "- `generations/` — evolutionary history\n"
            "- `best-variant/` — highest-scoring Hook.sol\n"
            "- `CLAUDE.md` — operating instructions for future runs\n\n"
            "## Next Steps\n"
            "1. Open in Obsidian for graph view\n"
            "2. Review `wiki/synthesis/recommendations.md`\n"
            "3. Promote your conclusions: change `author: agent` → `author: human`\n"
            "4. Re-run Probably Nothing to extend the knowledge base\n"
        )

        # --- .obsidian/ ---
        obs_dir = vault_path / ".obsidian"
        obs_dir.mkdir(exist_ok=True)
        (obs_dir / "app.json").write_text(json.dumps({
            "legacyEditor": False,
            "livePreview": True,
            "defaultViewMode": "preview"
        }, indent=2))
        (obs_dir / "graph.json").write_text(json.dumps({
            "collapse-filter": False,
            "search": "",
            "showTags": True,
            "showAttachments": False,
            "hideUnresolved": False,
            "showOrphans": True,
            "collapse-color-groups": False,
            "colorGroups": [
                {"query": "author:agent", "color": {"a": 1, "rgb": 14671839}},
                {"query": "author:human", "color": {"a": 1, "rgb": 16498611}}
            ],
            "collapse-display": False,
            "showArrow": True,
            "textFadeMultiplier": 0,
            "nodeSizeMultiplier": 1,
            "lineSizeMultiplier": 1,
            "collapse-forces": False,
            "centerStrength": 0.518713248970312,
            "repelStrength": 10,
            "linkStrength": 1,
            "linkDistance": 30,
            "scale": 1,
            "close": False
        }, indent=2))

        # --- by-url index so future re-runs can find this vault ---
        link_dir = out_dir / "by-url" / _slug_for_url(github_url)
        link_dir.mkdir(parents=True, exist_ok=True)
        link_target = link_dir / vault_name
        try:
            if not link_target.exists():
                link_target.symlink_to(vault_path.resolve(), target_is_directory=True)
        except OSError:
            # Filesystems without symlink support (rare) fall back to a pointer file.
            link_target.with_suffix(".txt").write_text(str(vault_path.resolve()))

        # Zip
        zip_path = out_dir / f"{vault_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in vault_path.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(vault_path))

        return f"/download/{vault_name}.zip"
