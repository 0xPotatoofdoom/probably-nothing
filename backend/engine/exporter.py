"""Obsidian vault exporter."""
import zipfile, json, os, tempfile
from pathlib import Path
from datetime import datetime

class VaultExporter:
    async def export(self, results: list, findings: list, github_url: str) -> str:
        out_dir = Path("/tmp/probably-nothing-vaults")
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        vault_name = f"vault-{ts}"
        vault_path = out_dir / vault_name
        vault_path.mkdir(exist_ok=True)

        # README
        best = results[0] if results else {}
        (vault_path / "README.md").write_text(f"""# Probably Nothing — Audit Report
**Hook:** {github_url}
**Date:** {datetime.now().isoformat()}
**Best Score:** {best.get('score', 0):.4f}
**Total Findings:** {len(findings)}

## Summary
{chr(10).join(f'- {f}' for f in findings[:20])}
""")

        # Agent reports
        agents_dir = vault_path / "agents"
        agents_dir.mkdir()
        agent_findings = {}
        for r in results:
            aid = r.get("agent_id", "unknown")
            agent_findings.setdefault(aid, []).extend(r.get("findings", []))
        for aid, afindings in agent_findings.items():
            (agents_dir / f"{aid}.md").write_text(f"""# {aid}
## Findings
{chr(10).join(f'- {f}' for f in afindings)}
## Score
{next((r['score'] for r in results if r.get('agent_id') == aid), 'N/A')}
""")

        # Best variant
        best_dir = vault_path / "best-variant"
        best_dir.mkdir()
        if best.get("source"):
            (best_dir / "Hook.sol").write_text(best["source"])

        # Obsidian config
        obs_dir = vault_path / ".obsidian"
        obs_dir.mkdir()
        (obs_dir / "app.json").write_text(json.dumps({"legacyEditor": False, "livePreview": True}))

        # Zip it
        zip_path = out_dir / f"{vault_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in vault_path.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(vault_path))

        return f"/download/{vault_name}.zip"
