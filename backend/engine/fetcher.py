"""
Fetch hook source from a GitHub URL and prepare a Foundry workspace ready for
forge test runs.

Strategy: we never compile against the user's own repo (too much dep drift).
Instead we snapshot the pre-baked `probably-nothing-foundry` workspace (which
ships with v4-template + transitive submodules) into a per-URL host directory
once, then drop the user's hook source — renamed to `contract Hook` — into
`src/Hook.sol`. Subsequent runs reuse the snapshot so forge's `out/` cache
stays warm across re-runs of the same hook.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional


REPO_CACHE = Path("/tmp/pn-repos")
WORKSPACE_CACHE = Path("/tmp/pn-workspaces")
WORKSPACE_IMAGE = os.getenv("PN_FOUNDRY_IMAGE", "probably-nothing-foundry")


class HookFetcher:
    def __init__(self):
        self.last_filename: Optional[str] = None
        self.last_workspace: Optional[Path] = None
        self.last_repo_path: Optional[Path] = None
        self.last_original_contract: Optional[str] = None

    async def fetch(self, github_url: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync, github_url)

    def _sync(self, github_url: str) -> str:
        repo_dir = self._clone(github_url)
        hook_path = self._locate_hook(repo_dir)
        self.last_filename = hook_path.name
        self.last_repo_path = repo_dir
        original_source = hook_path.read_text()

        renamed_source, original_name = _rename_primary_contract_to_hook(original_source)
        self.last_original_contract = original_name

        ws = self._prepare_workspace(github_url)
        (ws / "src" / "Hook.sol").write_text(renamed_source)
        self.last_workspace = ws
        return renamed_source

    def _clone(self, github_url: str) -> Path:
        REPO_CACHE.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(github_url.encode()).hexdigest()[:16]
        dest = REPO_CACHE / digest
        if dest.exists():
            try:
                subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"],
                               check=False, capture_output=True, timeout=30)
            except Exception:
                pass
            return dest
        subprocess.run(
            ["git", "clone", "--depth=1", github_url, str(dest)],
            check=True, capture_output=True,
        )
        return dest

    def _locate_hook(self, repo: Path) -> Path:
        sol_files = list(repo.rglob("src/*.sol")) or list(repo.rglob("*.sol"))
        if not sol_files:
            raise RuntimeError(f"No .sol files found in {repo}")
        candidates = [
            f for f in sol_files
            if "Test" not in f.name and "Script" not in f.name and "Mock" not in f.name
            and "/test/" not in str(f) and "/script/" not in str(f) and "/lib/" not in str(f)
        ]
        # Prefer hook-named files; fall back to anything that imports BaseHook.
        hook_named = [f for f in candidates if "Hook" in f.name]
        if hook_named:
            return hook_named[0]
        for f in candidates:
            try:
                t = f.read_text()
                if "BaseHook" in t or "IHooks" in t or "getHookPermissions" in t:
                    return f
            except Exception:
                continue
        return (candidates or sol_files)[0]

    def _prepare_workspace(self, github_url: str) -> Path:
        """Snapshot the image's /workspace into a per-URL host dir on first call."""
        WORKSPACE_CACHE.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(github_url.encode()).hexdigest()[:16]
        dest = WORKSPACE_CACHE / digest
        if dest.exists() and (dest / "lib").exists():
            return dest

        # docker cp from a freshly-created (not started) container of the image.
        tmp_name = f"pn-init-{uuid.uuid4().hex[:8]}"
        try:
            subprocess.run(
                ["docker", "create", "--name", tmp_name, WORKSPACE_IMAGE],
                check=True, capture_output=True, timeout=60,
            )
            dest.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["docker", "cp", f"{tmp_name}:/workspace/.", str(dest)],
                check=True, capture_output=True, timeout=600,
            )
        finally:
            subprocess.run(
                ["docker", "rm", tmp_name],
                check=False, capture_output=True, timeout=60,
            )
        return dest


# ─── helpers ───────────────────────────────────────────────────────────────────

_CONTRACT_DECL = re.compile(r"\bcontract\s+([A-Z][A-Za-z0-9_]*)\b")


def _rename_primary_contract_to_hook(source: str) -> tuple[str, Optional[str]]:
    """
    Rename the user's primary hook contract to `Hook` so PNBase's
    `deployCodeTo("Hook.sol:Hook", ...)` can find it. Returns (rewritten, original_name).

    Picks the first contract whose declaration block references BaseHook,
    IHooks, or getHookPermissions. Falls back to the first contract in the file.
    """
    matches = list(_CONTRACT_DECL.finditer(source))
    if not matches:
        return source, None

    primary_name: Optional[str] = None
    for m in matches:
        # Look at the next ~600 chars after the declaration as a heuristic
        snippet = source[m.start():m.start() + 600]
        if any(needle in snippet for needle in ("BaseHook", "IHooks", "getHookPermissions(")):
            primary_name = m.group(1)
            break
    if primary_name is None:
        primary_name = matches[0].group(1)

    if primary_name == "Hook":
        return source, primary_name

    # Replace ONLY the contract declaration — leave references in code alone.
    pattern = re.compile(rf"\bcontract\s+{re.escape(primary_name)}\b")
    rewritten = pattern.sub("contract Hook", source, count=1)

    # If the contract has a self-named library/error/event reference, those will
    # break — but it's rare and intentionally out of scope for the rename.
    return rewritten, primary_name
