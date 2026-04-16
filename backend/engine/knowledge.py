"""
Local knowledge graph — cross-run learning.

Stores findings, patterns, and scenario effectiveness across runs so each
new audit benefits from everything seen before. Backed by a single JSON file
at ~/.probably-nothing/knowledge.json (or PN_KNOWLEDGE_PATH env override).

No external services. Pure local.

Schema:
  hooks:           per-URL history (patterns, best score, key findings)
  pattern_findings: what we've seen on hooks with given flag/pattern combinations
  scenario_stats:  which scenario types find bugs vs. which are noise
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime


DEFAULT_PATH = Path.home() / ".probably-nothing" / "knowledge.json"

_SCHEMA_VERSION = 2


def _default_graph() -> dict:
    return {
        "version": _SCHEMA_VERSION,
        "hooks": {},
        "pattern_findings": {},
        "scenario_stats": {},
    }


class KnowledgeGraph:
    """
    Persistent cross-run knowledge store.

    Usage:
        kg = KnowledgeGraph()
        ctx = kg.get_prior_context(github_url, patterns=["beforeSwap", "dynamic_fee"])
        # ... run audit ...
        kg.record_run(github_url, patterns, findings, best_score, scenario_stats)
        kg.save()
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(os.getenv("PN_KNOWLEDGE_PATH", str(path or DEFAULT_PATH)))
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                if raw.get("version") == _SCHEMA_VERSION:
                    return raw
            except Exception:
                pass
        return _default_graph()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))

    # ── write ──────────────────────────────────────────────────────────────────

    def record_run(
        self,
        github_url: str,
        patterns: List[str],
        findings: List[str],
        best_score: float,
        scenario_stats: Optional[Dict[str, dict]] = None,
    ) -> None:
        """Persist what we learned from a completed run."""
        url_key = github_url.rstrip("/")

        # Per-hook record
        hook = self._data["hooks"].setdefault(url_key, {
            "url": url_key,
            "runs": 0,
            "best_score": 0.0,
            "patterns": [],
            "key_findings": [],
            "last_run": None,
        })
        hook["runs"] += 1
        hook["last_run"] = datetime.now().isoformat()
        hook["best_score"] = max(hook["best_score"], best_score)
        hook["patterns"] = list(set(hook["patterns"]) | set(patterns))
        # Keep the top 20 most informative findings (deduplicated)
        all_findings = list(dict.fromkeys(hook["key_findings"] + findings))
        hook["key_findings"] = all_findings[:20]

        # Pattern → findings index
        for pattern in patterns:
            pf = self._data["pattern_findings"].setdefault(pattern, [])
            for f in findings:
                if f not in pf:
                    pf.append(f)
            self._data["pattern_findings"][pattern] = pf[:30]  # cap per pattern

        # Scenario effectiveness stats
        if scenario_stats:
            for name, stats in scenario_stats.items():
                ss = self._data["scenario_stats"].setdefault(name, {
                    "runs": 0, "total_pass": 0, "total_fail": 0, "gas_samples": []
                })
                ss["runs"] += stats.get("runs", 0)
                ss["total_pass"] += stats.get("pass", 0)
                ss["total_fail"] += stats.get("fail", 0)
                samples = stats.get("gas_samples", [])
                ss["gas_samples"] = (ss["gas_samples"] + samples)[-50:]  # keep last 50

    # ── read ───────────────────────────────────────────────────────────────────

    def get_prior_context(self, github_url: str, patterns: List[str]) -> str:
        """
        Return a markdown context block summarising what we know about:
          - this specific hook (prior runs)
          - hooks that share the same flag/pattern profile

        Injected into the ScenarioProposer prompt so it targets angles we
        haven't already exhausted and avoids re-discovering known facts.
        """
        lines: List[str] = []

        # Prior runs on this exact URL
        url_key = github_url.rstrip("/")
        hook = self._data["hooks"].get(url_key)
        if hook and hook["runs"] > 0:
            lines.append(f"## Prior runs on this hook ({hook['runs']} run(s), best score {hook['best_score']:.4f})")
            for f in hook["key_findings"][:10]:
                lines.append(f"- {f}")

        # Pattern-level cross-hook learning
        seen_patterns = []
        for pattern in patterns:
            pf = self._data["pattern_findings"].get(pattern, [])
            if pf:
                seen_patterns.append((pattern, pf[:5]))

        if seen_patterns:
            lines.append("\n## Known findings on hooks with similar patterns")
            for pattern, pfindings in seen_patterns:
                lines.append(f"\n### Pattern: `{pattern}`")
                for f in pfindings:
                    lines.append(f"- {f}")

        # High-signal scenarios (bug-finding rate > 20%)
        effective = [
            (name, s) for name, s in self._data["scenario_stats"].items()
            if s["runs"] >= 3 and s["total_fail"] / max(s["runs"], 1) > 0.2
        ]
        if effective:
            lines.append("\n## High-signal scenario types (historically find bugs)")
            for name, s in sorted(effective, key=lambda x: x[1]["total_fail"], reverse=True)[:5]:
                lines.append(f"- {name}: {s['total_fail']} failures across {s['runs']} runs")

        if not lines:
            return ""

        return "# Prior Knowledge\n\n" + "\n".join(lines)

    def get_scenario_effectiveness(self) -> Dict[str, float]:
        """Return scenario_name → bug_find_rate for ranking."""
        result = {}
        for name, s in self._data["scenario_stats"].items():
            if s["runs"] > 0:
                result[name] = s["total_fail"] / s["runs"]
        return result

    def total_runs(self) -> int:
        return sum(h.get("runs", 0) for h in self._data["hooks"].values())
