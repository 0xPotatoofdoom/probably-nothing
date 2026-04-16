"""
Obsidian Vault Exporter — Persona Swarm Architecture
Two-author system: agent-authored findings + human-promotable synthesis.

Vault structure:
  sources/            — immutable hook source + run metadata
  coverage-matrix.md  — top-level persona pass/fail table
  personas/<id>/      — per-persona summary + their scenario files
  wiki/scenarios/     — flat scenario index (for cross-persona search + human promotion)
  wiki/synthesis/     — ReACT-generated coverage report + promotable recommendations
  CLAUDE.md           — operating instructions for future runs
"""
import zipfile
import json
import os
import hashlib
import re
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

from .persona import PersonaDef

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
- personas/<id>/ — per-persona findings. agent-authored.
- wiki/scenarios/ — all scenarios as promotable files. Promote a scenario: change `author: agent` → `author: human`.
- wiki/synthesis/ — cross-cutting conclusions. Human voice lives here.

## Compounding Protocol
After each re-run, review wiki/synthesis/recommendations.md.
If a recommendation represents your own conclusion, change `author: agent` to `author: human`.
That finding is now yours forever.
"""


def frontmatter(author: str, run_id: str, extra: dict = None) -> str:
    lines = [
        "---",
        f"author: {author}",
        f"run_id: {run_id}",
        "stale: false",
    ]
    if extra:
        for k, v in extra.items():
            if isinstance(v, (list, dict)):
                lines.append(f"{k}: {json.dumps(v)}")
            else:
                lines.append(f"{k}: {v}")
    lines.append("---\n\n")
    return "\n".join(lines)


def _slug_for_url(github_url: str) -> str:
    return hashlib.sha256(github_url.encode()).hexdigest()[:16]


_SCENARIO_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class VaultExporter:

    def load_human_scenarios(self, github_url: str) -> list:
        """Lift author:human scenarios from the most recent vault for this URL."""
        root = Path(os.getenv("PN_VAULT_DIR", "/tmp/probably-nothing-vaults"))
        link_dir = root / "by-url" / _slug_for_url(github_url)
        if not link_dir.exists():
            return []
        vaults = sorted(link_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        picked: list = []
        seen: set = set()
        for v in vaults:
            # Scan both wiki/scenarios/ (flat index) and personas/*/scenarios/
            scan_dirs = [v / "wiki" / "scenarios"]
            for persona_dir in (v / "personas").glob("*/scenarios") if (v / "personas").exists() else []:
                scan_dirs.append(persona_dir)
            for sdir in scan_dirs:
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
                    fm_block, body = m.group(1), m.group(2)
                    if "author: human" not in fm_block:
                        continue
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

    async def export(
        self,
        hook_source: str,
        github_url: str,
        coverage: Dict[str, Any],
        personas: List[PersonaDef],
        scenarios: Optional[list] = None,
        report_md: Optional[str] = None,
        run_id: str = None,
        # Legacy params — accepted but ignored
        results: Optional[list] = None,
        findings: Optional[list] = None,
    ) -> str:
        if not run_id:
            import uuid as _uuid
            run_id = datetime.now().strftime("%Y-%m-%d-%H%M%S") + "-" + _uuid.uuid4().hex[:6]

        out_dir = Path(os.getenv("PN_VAULT_DIR", "/tmp/probably-nothing-vaults"))
        out_dir.mkdir(parents=True, exist_ok=True)
        vault_name = f"vault-{run_id}"
        vault_path = out_dir / vault_name
        vault_path.mkdir(exist_ok=True)

        hook_name = github_url.rstrip("/").split("/")[-1]
        total_pass = sum(c.get("passed", 0) for c in coverage.values())
        total_tests = sum(c.get("total", 0) for c in coverage.values())
        total_fail = total_tests - total_pass

        # ── sources/ ───────────────────────────────────────────────────────────
        sources_dir = vault_path / "sources"
        sources_dir.mkdir(exist_ok=True)
        (sources_dir / "hook-source.sol").write_text(hook_source)
        (sources_dir / "run-metadata.json").write_text(json.dumps({
            "run_id": run_id,
            "github_url": github_url,
            "timestamp": datetime.now().isoformat(),
            "total_scenarios": total_tests,
            "total_passed": total_pass,
            "total_failed": total_fail,
            "personas": len(personas),
        }, indent=2))

        # ── coverage-matrix.md ─────────────────────────────────────────────────
        matrix_rows = []
        for persona in personas:
            data = coverage.get(persona.id, {})
            total = data.get("total", 0)
            passed = data.get("passed", 0)
            rate = data.get("pass_rate", 0.0)
            matrix_rows.append(
                f"| {persona.label} | {passed} | {total - passed} | {total} | {rate:.1%} |"
            )

        failure_details = []
        for persona in personas:
            data = coverage.get(persona.id, {})
            failures = data.get("failures", [])
            if failures:
                failure_details.append(f"\n### {persona.label}")
                for f in failures[:10]:
                    failure_details.append(f"- {f.get('text', f)}")

        (vault_path / "coverage-matrix.md").write_text(
            frontmatter("agent", run_id, {"hook": github_url}) +
            f"# Ecosystem Coverage Matrix — {hook_name}\n\n"
            f"| Persona | Passed | Failed | Total | Pass Rate |\n"
            f"|---------|--------|--------|-------|----------|\n" +
            "\n".join(matrix_rows) +
            f"\n\n**Total:** {total_pass}/{total_tests} scenarios passed "
            f"({total_pass/total_tests:.1%})\n\n" if total_tests else "\n\n**Total:** 0 scenarios run\n\n" +
            "## Failures Requiring Attention\n" +
            "\n".join(failure_details) if failure_details else ""
        )

        # ── personas/<id>/ ─────────────────────────────────────────────────────
        personas_dir = vault_path / "personas"
        personas_dir.mkdir(exist_ok=True)
        scenario_by_persona: Dict[str, list] = {p.id: [] for p in personas}
        for s in (scenarios or []):
            pid = getattr(s, "persona_id", "") or "security-auditor"
            scenario_by_persona.setdefault(pid, []).append(s)

        for persona in personas:
            p_dir = personas_dir / persona.id
            p_dir.mkdir(exist_ok=True)
            data = coverage.get(persona.id, {})
            total = data.get("total", 0)
            passed = data.get("passed", 0)
            rate = data.get("pass_rate", 0.0)
            failures = data.get("failures", [])

            # persona summary
            failure_block = ""
            if failures:
                failure_block = "\n## Failures\n" + "\n".join(f"- {f.get('text', f)}" for f in failures)
            (p_dir / "summary.md").write_text(
                frontmatter("agent", run_id, {"persona": persona.id, "pass_rate": f"{rate:.3f}"}) +
                f"# {persona.label}\n\n"
                f"**Role:** {persona.description}\n\n"
                f"**Coverage:** {passed}/{total} passed ({rate:.1%})\n\n"
                f"## Scenario Angles\n" +
                "\n".join(f"- {a}" for a in persona.scenario_angles) +
                failure_block
            )

            # per-persona scenario files
            scen_dir = p_dir / "scenarios"
            scen_dir.mkdir(exist_ok=True)
            for s in scenario_by_persona.get(persona.id, []):
                _write_scenario_file(scen_dir, s, run_id)

        # ── wiki/scenarios/ (flat index for cross-persona search + human promotion) ──
        wiki_scen_dir = vault_path / "wiki" / "scenarios"
        wiki_scen_dir.mkdir(parents=True, exist_ok=True)
        for s in (scenarios or []):
            _write_scenario_file(wiki_scen_dir, s, run_id)

        # ── wiki/synthesis/ ────────────────────────────────────────────────────
        synthesis_dir = vault_path / "wiki" / "synthesis"
        synthesis_dir.mkdir(parents=True, exist_ok=True)

        if report_md:
            (synthesis_dir / "ecosystem-coverage-report.md").write_text(
                frontmatter("agent", run_id,
                            {"citations": ["sources/hook-source.sol", "coverage-matrix.md"]}) +
                report_md
            )

        all_failure_texts = [
            f.get("text", str(f))
            for data in coverage.values()
            for f in data.get("failures", [])
        ]
        (synthesis_dir / "recommendations.md").write_text(
            frontmatter("agent", run_id, {"citations": ["coverage-matrix.md"]}) +
            "# Recommendations\n\n"
            "> Review this file. For any recommendation that represents YOUR conclusion, "
            "change `author: agent` to `author: human` in the frontmatter. "
            "That finding is now yours and will never be overwritten by future runs.\n\n"
            "## Failures To Address\n\n" +
            "\n".join(f"- {t}" for t in all_failure_texts[:20]) or "- No failures recorded"
        )

        # ── CLAUDE.md + README ─────────────────────────────────────────────────
        (vault_path / "CLAUDE.md").write_text(CLAUDE_MD)
        (vault_path / "README.md").write_text(
            frontmatter("agent", run_id) +
            f"# Probably Nothing — Audit Vault\n\n"
            f"**Hook:** {github_url}\n"
            f"**Run:** {run_id}\n"
            f"**Coverage:** {total_pass}/{total_tests} scenarios passed\n\n"
            "## Structure\n"
            "- `sources/` — immutable hook source\n"
            "- `coverage-matrix.md` — ecosystem coverage table (start here)\n"
            "- `personas/` — per-persona summaries and scenario files\n"
            "- `wiki/scenarios/` — flat scenario index (promote with `author: human`)\n"
            "- `wiki/synthesis/` — LLM-generated coverage report + recommendations\n"
            "- `CLAUDE.md` — operating instructions for future runs\n\n"
            "## Next Steps\n"
            "1. Open `coverage-matrix.md` — see which personas are failing\n"
            "2. Open `personas/<failing-id>/summary.md` — see what broke\n"
            "3. Read `wiki/synthesis/recommendations.md` — promote your own conclusions\n"
            "4. Re-run Probably Nothing to extend coverage\n"
        )

        # ── .obsidian/ ─────────────────────────────────────────────────────────
        obs_dir = vault_path / ".obsidian"
        obs_dir.mkdir(exist_ok=True)
        (obs_dir / "app.json").write_text(json.dumps({
            "legacyEditor": False, "livePreview": True, "defaultViewMode": "preview"
        }, indent=2))
        (obs_dir / "graph.json").write_text(json.dumps({
            "collapse-filter": False, "showTags": True, "showAttachments": False,
            "hideUnresolved": False, "showOrphans": True,
            "colorGroups": [
                {"query": "author:agent", "color": {"a": 1, "rgb": 14671839}},
                {"query": "author:human", "color": {"a": 1, "rgb": 16498611}},
            ],
            "showArrow": True, "nodeSizeMultiplier": 1,
        }, indent=2))

        # ── by-url symlink index ───────────────────────────────────────────────
        link_dir = out_dir / "by-url" / _slug_for_url(github_url)
        link_dir.mkdir(parents=True, exist_ok=True)
        link_target = link_dir / vault_name
        try:
            if not link_target.exists():
                link_target.symlink_to(vault_path.resolve(), target_is_directory=True)
        except OSError:
            link_target.with_suffix(".txt").write_text(str(vault_path.resolve()))

        # ── Zip ───────────────────────────────────────────────────────────────
        zip_path = out_dir / f"{vault_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in vault_path.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(vault_path))

        return f"/download/{vault_name}.zip"


def _write_scenario_file(directory: Path, s: Any, run_id: str) -> None:
    """Write a single scenario as a promotable .md file with solidity fence."""
    try:
        author = "human" if getattr(s, "proposer", None) == "human" else "agent"
        informativeness = getattr(s, "informativeness", float("inf"))
        samples = len(getattr(s, "gas_samples", []) or [])
        persona_id = getattr(s, "persona_id", "")
        body = (
            frontmatter(author, run_id, {
                "citations": ["sources/hook-source.sol"],
                "persona": persona_id or "unknown",
            }) +
            f"# {s.contract_name}\n\n"
            f"**Persona:** {persona_id or 'baseline'}\n"
            f"**Proposer:** {s.proposer}\n"
            f"**Samples:** {samples}\n"
            f"**Informativeness (gas variance):** "
            f"{'∞' if informativeness == float('inf') else f'{informativeness:.1f}'}\n"
            f"**Failure rate:** {s.failure_rate:.2%}\n\n"
            "## Source\n"
            "```solidity\n"
            f"{s.source}\n"
            "```\n"
        )
        (directory / f"{s.contract_name}.md").write_text(body)
    except Exception:
        pass  # don't let a bad scenario break the whole export
