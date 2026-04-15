"""
Fetch hook source from a GitHub URL and prepare a Foundry workspace ready for
forge test runs.

Strategy: snapshot the pre-baked `probably-nothing-foundry` workspace (which
ships with v4-template + transitive submodules) into a per-URL host directory,
then merge the user's own lib/ and remappings on top so hooks that pull in
external deps (openzeppelin-contracts, solmate, etc.) still compile. The hook
source itself is renamed to `contract Hook` and written to src/Hook.sol.
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

        ws = self._prepare_workspace(github_url, repo_dir)
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
                # Hydrate any submodules that weren't checked out on the cached clone.
                subprocess.run(
                    ["git", "-C", str(dest), "submodule", "update",
                     "--init", "--recursive", "--depth=1"],
                    check=False, capture_output=True, timeout=180,
                )
            except Exception:
                pass
            return dest
        # Clone with submodules so user's lib/ is fully populated.
        subprocess.run(
            ["git", "clone", "--depth=1",
             "--recurse-submodules", "--shallow-submodules",
             github_url, str(dest)],
            check=True, capture_output=True,
            timeout=300,  # submodule hydration can be slow
        )
        return dest

    def _locate_hook(self, repo: Path) -> Path:
        # Search the repo's own src/ tree first (handles nested layouts like src/contracts/hooks/).
        src_dir = repo / "src"
        if src_dir.exists():
            sol_files = [f for f in src_dir.rglob("*.sol") if "/lib/" not in str(f)]
        else:
            sol_files = [f for f in repo.rglob("*.sol") if "/lib/" not in str(f) and "/test/" not in str(f)]
        if not sol_files:
            raise RuntimeError(f"No .sol files found in {repo}")
        # Exclude obvious non-hook files.
        _NON_HOOK = {"Test", "Script", "Mock", "Router", "Manager", "Factory",
                     "Lens", "Library", "Interface", "Helper", "Utils", "Base"}
        candidates = [
            f for f in sol_files
            if not any(x in f.name for x in _NON_HOOK)
            and "/test/" not in str(f) and "/script/" not in str(f) and "/lib/" not in str(f)
        ]

        # Tier 1: file that *defines* getHookPermissions — strongest signal.
        for f in candidates:
            try:
                t = f.read_text()
                if "getHookPermissions" in t:
                    return f
            except Exception:
                continue

        # Tier 2: file that *inherits* BaseHook directly.
        for f in candidates:
            try:
                t = f.read_text()
                if re.search(r"\bcontract\s+\w+\s+is\s+[^\{]*BaseHook", t):
                    return f
            except Exception:
                continue

        # Tier 3: file named *Hook*.
        hook_named = [f for f in candidates if "Hook" in f.name]
        if hook_named:
            return hook_named[0]

        # Tier 4: anything that mentions BaseHook (last resort — may include routers).
        for f in candidates:
            try:
                t = f.read_text()
                if "BaseHook" in t or "IHooks" in t:
                    return f
            except Exception:
                continue

        return (candidates or sol_files)[0]

    def _prepare_workspace(self, github_url: str, repo_dir: Path) -> Path:
        """
        Build (or reuse) a per-URL workspace:
          1. Bootstrap from the Docker image on first call.
          2. Merge user's lib/ into workspace lib/ (additive — no overwrites).
          3. Copy user's src/ into workspace src/ so internal imports resolve.
          4. Write remappings.txt so @alias/ paths compile correctly.
        """
        WORKSPACE_CACHE.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(github_url.encode()).hexdigest()[:16]
        dest = WORKSPACE_CACHE / digest

        # Step 1: Bootstrap workspace from Docker image once.
        if not (dest / "lib").exists():
            _bootstrap_from_image(dest)

        # Step 2: Merge user's lib/ subdirectories that are absent from workspace.
        user_lib = repo_dir / "lib"
        if user_lib.exists():
            ws_lib = dest / "lib"
            for entry in sorted(user_lib.iterdir()):
                target = ws_lib / entry.name
                if not target.exists() and entry.is_dir():
                    try:
                        shutil.copytree(str(entry), str(target))
                    except Exception:
                        pass  # best-effort

        # Step 3: Copy user's src/ into workspace src/ so internal @alias/ imports work.
        # We copy additive-only so our scaffolding (Hook.sol) is never overwritten here
        # (it's written later by _sync). Only .sol files; skip test/script dirs.
        user_src = repo_dir / "src"
        if user_src.exists():
            ws_src = dest / "src"
            for item in user_src.rglob("*.sol"):
                rel = item.relative_to(user_src)
                # Skip test/script subtrees from user's repo.
                parts = rel.parts
                if any(p in ("test", "tests", "script", "scripts") for p in parts):
                    continue
                target = ws_src / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(item), str(target))

        # Step 4: Write remappings.txt with user's remappings merged with workspace ones.
        _write_remappings_txt(repo_dir, dest)

        return dest


# ─── workspace bootstrap ───────────────────────────────────────────────────────

def _bootstrap_from_image(dest: Path) -> None:
    """docker create + docker cp to snapshot the pre-baked workspace."""
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


# ─── remapping helpers ─────────────────────────────────────────────────────────

_REMAP_TOML_ARRAY = re.compile(r'remappings\s*=\s*\[(.*?)\]', re.DOTALL)
_REMAP_QUOTED = re.compile(r'"([^"]+)"')


def _write_remappings_txt(repo_dir: Path, ws_dir: Path) -> None:
    """
    Build a remappings.txt in the workspace by merging:
      - workspace's existing remappings.txt (if any)
      - user repo's remappings (from remappings.txt or foundry.toml array)
    Forge respects remappings.txt alongside foundry.toml. We never override
    existing workspace keys — our resolution paths are authoritative.
    """
    # Collect workspace's existing remappings (keys we must not override).
    ws_remap_txt = ws_dir / "remappings.txt"
    existing_lines: list[str] = []
    existing_keys: set[str] = set()
    if ws_remap_txt.exists():
        for ln in ws_remap_txt.read_text().splitlines():
            ln = ln.strip()
            if ln and "=" in ln:
                existing_lines.append(ln)
                existing_keys.add(ln.split("=")[0])

    # Read user's remappings.
    user_remaps = _read_remappings(repo_dir)

    # Add new remappings that don't conflict with existing workspace keys.
    new_entries = [r for r in user_remaps if r.split("=")[0] not in existing_keys]
    if not new_entries:
        return

    all_lines = existing_lines + new_entries
    ws_remap_txt.write_text("\n".join(all_lines) + "\n")


def _read_remappings(repo_dir: Path) -> list[str]:
    """Return 'key=value' remapping strings from remappings.txt or foundry.toml array."""
    # Prefer remappings.txt — it's unambiguous.
    remap_txt = repo_dir / "remappings.txt"
    if remap_txt.exists():
        lines = remap_txt.read_text().splitlines()
        return [ln.strip() for ln in lines if "=" in ln and not ln.strip().startswith("#")]

    # Fall back to foundry.toml remappings array.
    toml = repo_dir / "foundry.toml"
    if toml.exists():
        m = _REMAP_TOML_ARRAY.search(toml.read_text())
        if m:
            return [e for e in _REMAP_QUOTED.findall(m.group(1)) if "=" in e]

    return []


# ─── contract rename ───────────────────────────────────────────────────────────

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
        snippet = source[m.start():m.start() + 600]
        if any(needle in snippet for needle in ("BaseHook", "IHooks", "getHookPermissions(")):
            primary_name = m.group(1)
            break
    if primary_name is None:
        primary_name = matches[0].group(1)

    if primary_name == "Hook":
        return source, primary_name

    pattern = re.compile(rf"\bcontract\s+{re.escape(primary_name)}\b")
    rewritten = pattern.sub("contract Hook", source, count=1)
    return rewritten, primary_name
