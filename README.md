# Probably Nothing

**An autonomous audit tool for Uniswap V4 hooks.**

Paste a GitHub link to any V4 hook. Watch a live merkle tree of research agents bloom across the screen as they test swap scenarios, LP deployments, MEV resistance, and edge cases. When done, download an Obsidian vault with everything they found.

## Quick Start

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

### Frontend
```bash
cd frontend
npm install
npm start
```

### Full stack (Docker Compose)
```bash
docker-compose up
```

### Foundry workspace image (real test runs)
Probably Nothing ships a pre-baked Foundry workspace with the V4 stack
(`v4-core`, `v4-periphery`, `solmate`, `permit2`, `forge-std`, OpenZeppelin).
Build it once:
```bash
docker build -t probably-nothing-foundry ./backend/foundry_workspace
```
This is a 10-20 minute build the first time — it clones and pre-compiles the
whole V4 dep tree. Subsequent runs are incremental.

Without this image, the harness falls back to content-hashed stubs and emits a
clear warning.

### Standalone CLI (one-line Docker)
Hook builders who just want the audit:
```bash
docker build -t probably-nothing ./backend

docker run --rm \
  -v "$PWD/out:/out" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PN_LLM_BACKEND=ollama \
  -e PN_LLM_MODEL=qwen3-coder-next:latest \
  -e PN_LLM_ENDPOINT=http://host.docker.internal:11434 \
  --add-host=host.docker.internal:host-gateway \
  probably-nothing https://github.com/your/uniswap-v4-hook \
  --agents 10 --budget 1800
```
The docker-socket mount is required so the CLI container can launch the
`probably-nothing-foundry` container per variant.

Extra flags: `--agents N`, `--skill path/to/skill.md`, `--budget 1800`, `--json`.

For a local Python run without the CLI container (foundry image still needed):
```bash
cd backend && python -m cli <github-url> --output ./out --budget 1800
```

### What happens during a run
1. **Workspace prep** — clones the hook repo, copies `Hook.sol` into the
   pre-baked workspace, parses `getHookPermissions()` to set the HookMiner flags.
2. **Scenario seeding** — LLM proposes ~20 Forge test contracts targeting
   routing, LP, MEV, edge cases, and permission boundaries. Each is
   compile-gated (`forge build`) before entering the pool.
3. **Variant + scenario co-evolution** — every generation:
   - Agents mutate the hook source (parametric → structural → LLM-assisted).
   - Each variant runs against the full scenario pool via `forge test --json`.
   - LLM proposes new scenarios targeting findings the current pool missed.
   - Low-informativeness scenarios (no gas variance across variants) are pruned.
4. **Vault export** — everything lands in an Obsidian vault:
   - `wiki/scenarios/` with `author: agent` frontmatter on every generated test.
   - Change one line (`agent` → `human`) to make a scenario survive re-runs.
5. **Re-run continuity** — Probably Nothing scans `<PN_VAULT_DIR>/by-url/<hash>/`
   for prior vaults and carries forward every `author: human` scenario as a
   mandatory baseline.

## Architecture
- **Frontend**: React + Framer Motion (SVG merkle tree animation)
- **Backend**: FastAPI + asyncio + WebSocket
- **Testing**: Foundry in Docker
- **Output**: Obsidian vault .zip

## Agent Roles
- Gas Optimizer
- MEV Sentinel
- LP Deployer
- Swap Scenario
- Edge Case Hunter
- Security Auditor

Configurable from 1–1000 agents via slider.

## Mutation Order
1. Parametric (fee tiers, uint constants, booleans)
2. Structural (hook permission flags, oracle integrations)
3. LLM-assisted (domain-aware proposals)
