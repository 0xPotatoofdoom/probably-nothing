"""Fetch hook source from GitHub URL."""
import asyncio, subprocess, tempfile, os, re
from pathlib import Path

class HookFetcher:
    def __init__(self):
        self.last_filename = None

    async def fetch(self, github_url: str) -> str:
        """Clone repo, find primary Hook.sol, return source."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch_sync, github_url)

    def _fetch_sync(self, github_url: str) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["git", "clone", "--depth=1", github_url, tmpdir],
                check=True, capture_output=True
            )
            sol_files = list(Path(tmpdir).rglob("src/*.sol"))
            if not sol_files:
                sol_files = list(Path(tmpdir).rglob("*.sol"))
            # Prefer files with "Hook" in the name
            hook_files = [f for f in sol_files if "Hook" in f.name and "Test" not in f.name]
            target = hook_files[0] if hook_files else sol_files[0]
            self.last_filename = target.name
            return target.read_text()
