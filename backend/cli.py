"""
Probably Nothing — standalone CLI.

Runs the autoresearch loop without the web frontend. Streams progress to the
terminal and writes the Obsidian vault zip to the output directory.

Usage (local):
    python -m cli <github-url> [--agents N] [--skill path] [--output dir] [--json]

Usage (one-line docker — see README):
    docker run --rm -v $PWD/out:/out \
      -e PN_LLM_BACKEND=ollama \
      -e PN_LLM_ENDPOINT=http://host.docker.internal:11434 \
      --add-host=host.docker.internal:host-gateway \
      probably-nothing <github-url>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from engine.evaluator import HookEvaluator


# ANSI for humans; stripped when --json or when stdout isn't a tty.
def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty() or os.getenv("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


_VERBOSE = True  # set by main() from --quiet


# Per-archetype color so each agent family is visually distinct.
_ARCHETYPE_COLORS = {
    "gas-optimizer":    "33",   # yellow
    "mev-sentinel":     "31",   # red
    "lp-deployer":      "34",   # blue
    "swap-scenario":    "36",   # cyan
    "edge-case-hunter": "35",   # magenta
    "security-auditor": "32",   # green
}


def _agent_color(agent_id: str) -> str:
    archetype = agent_id.rsplit("-", 1)[0] if agent_id[-1].isdigit() else agent_id
    return _ARCHETYPE_COLORS.get(archetype, "37")


def _tag(agent_id: str) -> str:
    return _c(_agent_color(agent_id), f"[{agent_id:<20}]")


def _pretty(event: dict):
    """Return a rendered line, or None to suppress the event."""
    t = event.get("type")
    if t == "status":
        return _c("90", f"• {event['message']}")
    if t == "agent_spawn":
        return f"{_tag(event['agent_id'])} {_c('1','SPAWN')}  {event['label']}  [{event['direction']}]"
    if t == "variant_start":
        if not _VERBOSE:
            return None
        return (
            f"{_tag(event['agent_id'])} "
            f"{_c('90', 'WORK ')}  gen {event['gen']:02d} · variant #{event['variant_index']:02d} "
            f"· tier={event['tier']}"
        )
    if t == "variant_complete":
        score = event.get("score", 0)
        gas = event.get("gas_used")
        tp, tf = event.get("tests_passed"), event.get("tests_failed")
        gas_s = f"gas={gas:,}" if isinstance(gas, int) else ""
        test_s = f"tests={tp}/{tp + tf}" if isinstance(tp, int) and isinstance(tf, int) else ""
        extras = "  ".join(s for s in (gas_s, test_s) if s)
        return (
            f"{_tag(event['agent_id'])} {_c('1;32', 'DONE ')}  "
            f"gen {event['gen']:02d} · variant #{event['variant_index']:02d} · "
            f"score={score:.4f}  {extras}"
        )
    if t == "finding":
        if not _VERBOSE and event["text"].startswith("Re-checking:"):
            return None
        delta = event.get("score_delta", 0)
        sign = _c("32", f"{delta:+.3f}") if delta >= 0 else _c("31", f"{delta:+.3f}")
        return f"{_tag(event['agent_id'])} {_c('95', 'FIND ')}  {event['text']}  {sign}"
    if t == "generation_start":
        scen = event.get("scenarios", 0)
        extra = f" · scenarios={scen}" if scen else ""
        return _c("1;36", f"\n── gen {event['gen']:02d} [{event.get('tier','?')}] · population={event['population']}{extra} ──")
    if t == "generation_complete":
        scen = event.get("scenarios", 0)
        extra = f" · scenarios={scen}" if scen else ""
        return _c("36", f"── gen {event['gen']:02d} done · best={event['best_score']:.4f} · tested={event['variants_tested']}{extra} ──")
    if t == "scenario_added":
        author = event.get("proposer", "llm")
        return _c("94", f"+ scenario  {event['contract']:<34} (by {author}, gen {event['gen']})")
    if t == "scenario_pruned":
        return _c("90", f"- scenario  {event['scenario_id']}  (low informativeness)")
    if t == "scenario_rejected":
        reason = event.get("reason", "unknown")
        return _c("90", f"✗ scenario  rejected: {reason[:100]}")
    if t == "complete":
        scen = event.get("scenarios_active", 0)
        return _c(
            "1;32",
            f"\n✓ COMPLETE  findings={event['total_findings']}  best={event['best_score']:.4f}  "
            f"gens={event['generations']}  elapsed={event['elapsed_seconds']}s  "
            f"harness={event.get('harness_mode','?')}  scenarios={scen}  "
            f"llm={event['llm_backend']}:{event['llm_model']}\n"
            f"  vault → {event['vault_url']}"
        )
    if t == "error":
        return _c("1;31", f"✗ ERROR: {event['message']}")
    return json.dumps(event)


async def run(args: argparse.Namespace) -> int:
    skill_md = None
    if args.skill:
        p = Path(args.skill)
        if not p.exists():
            print(f"skill.md not found: {p}", file=sys.stderr)
            return 2
        skill_md = p.read_text()

    if args.output:
        os.environ["PN_VAULT_DIR"] = str(Path(args.output).resolve())
    if args.budget is not None:
        os.environ["PN_WALL_BUDGET"] = str(args.budget)

    evaluator = HookEvaluator()
    exit_code = 0
    last_vault_url: str | None = None

    async for event in evaluator.analyze(
        args.url, num_agents=args.agents, skill_md=skill_md
    ):
        if args.json:
            print(json.dumps(event), flush=True)
        else:
            line = _pretty(event)
            if line is not None:
                print(line, flush=True)
        if event.get("type") == "complete":
            last_vault_url = event.get("vault_url")
        if event.get("type") == "error":
            exit_code = 1

    # Resolve the actual filesystem path of the vault for the one-liner use case.
    if last_vault_url and not args.json:
        vault_dir = Path(os.getenv("PN_VAULT_DIR", "/tmp/probably-nothing-vaults"))
        zip_name = last_vault_url.rsplit("/", 1)[-1]
        zip_path = vault_dir / zip_name
        if zip_path.exists():
            print(f"  file:  {zip_path}")

    return exit_code


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="probably-nothing",
        description="Autonomous audit tool for Uniswap V4 hooks.",
    )
    ap.add_argument("url", help="GitHub URL of a Uniswap V4 hook repo")
    ap.add_argument("--agents", "-a", type=int, default=6, help="Agent count (default: 6)")
    ap.add_argument("--skill", "-s", help="Path to a skill.md research seed file (20KB cap)")
    ap.add_argument("--output", "-o", default="/out", help="Vault output directory (default: /out)")
    ap.add_argument("--budget", type=float, help="Wall-clock budget in seconds (default: 300)")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON events instead of pretty output")
    ap.add_argument("--quiet", "-q", action="store_true", help="Suppress per-variant WORK lines and duplicate 'Re-checking' findings")
    args = ap.parse_args()
    global _VERBOSE
    _VERBOSE = not args.quiet
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
