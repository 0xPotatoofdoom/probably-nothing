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

        ws = self._prepare_workspace(github_url, repo_dir, hook_path)

        # If the hook was in a subdirectory of src/ (e.g. src/hooks/ClankerHook.sol),
        # its relative imports were written relative to that subdir.  Moving it to
        # src/Hook.sol changes resolution — rewrite imports so they still resolve.
        src_root = repo_dir / "src"
        if src_root.exists() and hook_path.is_relative_to(src_root):
            original_rel_dir = hook_path.parent.relative_to(src_root)
            if str(original_rel_dir) != ".":
                renamed_source = _rewrite_relative_imports(
                    renamed_source,
                    original_dir=Path("src") / original_rel_dir,
                    new_dir=Path("src"),
                )
        elif not (src_root.exists() and hook_path.is_relative_to(src_root)):
            # Hook is outside repo's src/ (e.g. hook/src/ or contracts/).
            # Rewrite imports relative to its actual parent → workspace src/.
            hook_dir = hook_path.parent
            renamed_source = _rewrite_relative_imports(
                renamed_source,
                original_dir=Path("src") / hook_dir.name,  # approximation
                new_dir=Path("src"),
            )

        # Rewrite any lib/<nested>/lib/<known-lib>/ style imports to direct lib/ paths.
        # e.g. "lib/uniswap-hooks/lib/v4-periphery/BaseHook.sol" → "lib/v4-periphery/src/BaseHook.sol"
        renamed_source = _normalize_lib_imports(renamed_source, ws)

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

    def _prepare_workspace(self, github_url: str, repo_dir: Path, hook_path: Optional[Path] = None) -> Path:
        """
        Build (or reuse) a per-URL workspace:
          1. Bootstrap from the Docker image on first call.
          2. Merge user's lib/ into workspace lib/ (additive — no overwrites).
          3. Copy user's src/ into workspace src/ so internal imports resolve.
          4. Write remappings.txt so @alias/ paths compile correctly.
          5. Fix case-sensitivity mismatches in relative imports.
        """
        WORKSPACE_CACHE.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(github_url.encode()).hexdigest()[:16]
        dest = WORKSPACE_CACHE / digest

        # Step 1: Bootstrap workspace from Docker image once.
        if not (dest / "lib").exists():
            _bootstrap_from_image(dest)

        # Step 2: Merge user's lib/ subdirectories that are absent from workspace.
        # Also redirect stale nested copies of standard libs (openzeppelin-contracts,
        # solmate, forge-std) to the user's version when the user ships a newer one —
        # this fixes repos like Clanker that need OZ 5.1+ while the workspace has 5.0.x.
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
            # For known standard libs, redirect stale nested copies to user's version.
            _redirect_nested_libs(user_lib, ws_lib)

        # Step 3: Copy user's src/ into workspace src/ so internal @alias/ imports work.
        # We copy additive-only so our scaffolding (Hook.sol) is never overwritten here
        # (it's written later by _sync). Only .sol files; skip test/script dirs.
        ws_src = dest / "src"

        def _copy_sol_dir(src_dir: Path) -> None:
            for item in src_dir.rglob("*.sol"):
                rel = item.relative_to(src_dir)
                parts = rel.parts
                if any(p in ("test", "tests", "script", "scripts") for p in parts):
                    continue
                target = ws_src / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(item), str(target))

        user_src = repo_dir / "src"
        if user_src.exists():
            _copy_sol_dir(user_src)

        # Also copy from the hook's actual parent directory when the hook lives
        # outside the repo's standard src/ (e.g. hook/src/, contracts/).
        if hook_path is not None:
            hook_src_dir = hook_path.parent
            if not (user_src.exists() and hook_path.is_relative_to(user_src)):
                _copy_sol_dir(hook_src_dir)

        # Step 4: Write remappings.txt with user's remappings merged with workspace ones.
        _write_remappings_txt(repo_dir, dest)

        # Step 5: Fix case-sensitivity mismatches in relative imports.
        # Repos developed on macOS often have import paths that differ in case from
        # the actual filenames (e.g. "IClankerLpLocker.sol" vs "IClankerLPLocker.sol").
        # On Linux these fail to resolve — create symlinks to the actual files.
        _fix_case_mismatches(dest / "src")

        return dest


# ─── import path helpers ───────────────────────────────────────────────────────

_IMPORT_PATH_RE = re.compile(r'import\s+[^"\']*["\']([^"\']+)["\']')
_IMPORT_REWRITE_RE = re.compile(r'(import\s+[^"\']*["\'])([^"\']+)(["\'])')


# Libs that are safe to unify across the workspace dep tree.
# V4 libs included so hooks that import via lib/uniswap-hooks/lib/v4-core/... get redirected.
_STANDARD_LIBS = {
    "openzeppelin-contracts", "solmate", "forge-std",
    "v4-core", "v4-periphery", "v4-template",
}

# Maps a lib directory name to where its source lives in our workspace.
# Used by _normalize_lib_imports to rewrite nested lib paths.
_LIB_SRC_ROOTS: dict[str, str] = {
    "v4-core":       "lib/v4-core/src/",
    "v4-periphery":  "lib/v4-periphery/src/",
    "forge-std":     "lib/forge-std/src/",
    "openzeppelin-contracts": "lib/openzeppelin-contracts/",
    "solmate":       "lib/solmate/src/",
}

# Regex to match one level of lib wrapping: lib/<wrapper>/lib/<known-lib>/
# Captures group 1 = known lib name, so remainder = path[match.end():]
_NESTED_LIB_RE = re.compile(
    r"lib/[^/\"']+/lib/((?:" +
    "|".join(re.escape(k) for k in _LIB_SRC_ROOTS) +
    r"))/"
)


def _normalize_lib_imports(source: str, ws_dir: Path) -> str:
    """
    Rewrite nested lib/ import paths to direct workspace paths.

    Hooks developed with git submodules often import from non-standard nested
    paths like 'lib/uniswap-hooks/lib/v4-periphery/src/BaseHook.sol'. Our
    workspace has these at the top level. Strip the nesting so forge can find
    them via our remappings.

    Falls back to a filename search in workspace lib/ for any import that still
    doesn't resolve after the regex rewrite.
    """
    ws_lib = ws_dir / "lib"

    def _rewrite(m: re.Match) -> str:
        prefix, path, suffix = m.group(1), m.group(2), m.group(3)
        if not path.startswith("lib/"):
            return m.group(0)

        # Step 1: strip nested lib/<wrapper>/lib/<known>/ to direct lib/<known>/
        nested_m = _NESTED_LIB_RE.match(path)
        if nested_m:
            lib_name = nested_m.group(1)
            remainder = path[nested_m.end():]   # everything after the lib name + /
            canonical_root = _LIB_SRC_ROOTS.get(lib_name, f"lib/{lib_name}/")
            # Only rewrite if the canonical path actually exists in the workspace.
            candidate = ws_dir / canonical_root / remainder
            if candidate.exists():
                return prefix + canonical_root + remainder + suffix
            # Try without /src/ infix (some repos omit it)
            alt_root = f"lib/{lib_name}/"
            candidate2 = ws_dir / alt_root / remainder
            if candidate2.exists():
                return prefix + alt_root + remainder + suffix

        # Step 2: if path still doesn't exist, try to find the file by name in ws lib/
        if not (ws_dir / path).exists() and ws_lib.exists():
            filename = Path(path).name
            hits = sorted(ws_lib.rglob(filename),
                          key=lambda p: len(p.parts))  # prefer shallower (more canonical)
            if hits:
                rel = str(hits[0].relative_to(ws_dir))
                return prefix + rel + suffix

        return m.group(0)

    return _IMPORT_REWRITE_RE.sub(_rewrite, source)


def _redirect_nested_libs(user_lib: Path, ws_lib: Path) -> None:
    """
    Replace stale nested copies of standard libs inside workspace libs with
    symlinks to the top-level copy. This fixes version-conflict compile errors
    (e.g. OZ 5.0 nested inside a lib that needs OZ 5.1).
    """

    for lib_name in _STANDARD_LIBS:
        user_copy = user_lib / lib_name
        if not user_copy.is_dir():
            continue
        ws_top = ws_lib / lib_name  # may not exist — that's fine
        user_file_count = sum(1 for _ in user_copy.rglob("*.sol"))

        # Walk the workspace libs looking for stale nested copies.
        for nested in ws_lib.rglob(lib_name):
            if not nested.is_dir() or nested == ws_top:
                continue  # skip the top-level copy (already merged)
            nested_file_count = sum(1 for _ in nested.rglob("*.sol"))
            # Only replace if user has meaningfully more files (newer version).
            if user_file_count <= nested_file_count:
                continue
            try:
                shutil.rmtree(str(nested))
                # Relative symlink so it works inside Docker.
                rel_target = os.path.relpath(str(ws_top if ws_top.exists() else user_copy), str(nested.parent))
                nested.symlink_to(rel_target)
            except Exception:
                pass  # best-effort


def _rewrite_relative_imports(source: str, original_dir: Path, new_dir: Path) -> str:
    """
    Rewrite relative imports so they resolve correctly from new_dir instead of
    original_dir.  Used when a hook file is moved from src/<subdir>/Hook.sol to
    src/Hook.sol — without this, '../Foo.sol' would go one level too high.

    Both original_dir and new_dir should be relative paths from the workspace root
    (e.g. Path("src/hooks") and Path("src")).
    """
    if original_dir == new_dir:
        return source

    def rewrite(m: re.Match) -> str:
        prefix, raw, suffix = m.group(1), m.group(2), m.group(3)
        if not (raw.startswith("./") or raw.startswith("../")):
            return m.group(0)  # non-relative (e.g. @uniswap/…), leave as-is
        try:
            # Resolve the import from the original location (using pure POSIX paths).
            resolved = os.path.normpath(str(original_dir / raw))
            # Express relative to the new location.
            new_rel = os.path.relpath(resolved, str(new_dir))
            if not new_rel.startswith("../"):
                new_rel = "./" + new_rel
            return prefix + new_rel + suffix
        except Exception:
            return m.group(0)

    return _IMPORT_REWRITE_RE.sub(rewrite, source)


def _fix_case_mismatches(src_dir: Path) -> None:
    """
    Create symlinks for relative imports whose filename case doesn't match the
    actual file on disk.  Repos developed on macOS (case-insensitive FS) often
    have e.g. `import "…/IClankerLpLocker.sol"` where the real file is
    `IClankerLPLocker.sol`.  On Linux this fails; a symlink fixes it silently.
    """
    if not src_dir.exists():
        return

    sol_files = list(src_dir.rglob("*.sol"))

    # Build a map: lowercase_absolute_path → actual_Path for all files.
    case_map: dict[str, Path] = {}
    for f in sol_files:
        case_map[str(f).lower()] = f

    for f in sol_files:
        try:
            content = f.read_text(errors="ignore")
        except Exception:
            continue
        for m in _IMPORT_PATH_RE.finditer(content):
            raw = m.group(1)
            if not raw.startswith("./") and not raw.startswith("../"):
                continue
            # Resolve the import path relative to the current file.
            try:
                resolved = (f.parent / raw).resolve()
            except Exception:
                continue
            if resolved.exists():
                continue
            lower = str(resolved).lower()
            actual = case_map.get(lower)
            if actual is None:
                continue
            try:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                # Use a relative symlink so it works inside Docker where the
                # workspace is mounted at a different absolute path than the host.
                rel_target = os.path.relpath(actual, resolved.parent)
                resolved.symlink_to(rel_target)
            except Exception:
                pass  # best-effort


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
