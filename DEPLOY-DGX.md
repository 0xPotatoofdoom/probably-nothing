# DEPLOY-DGX

Deployment notes for running **Probably Nothing** on the DGX Spark
(GB10 Grace Blackwell, 121 GB RAM). No GPU required for Foundry — this
machine exists to host the LLM-assisted mutation tier with real parameters.

## 1. One-shot setup

```bash
# LLM — default is qwen3-coder-next:latest (already pulled on this DGX).
ollama pull qwen3-coder-next:latest      # skip if already present
curl -s http://localhost:11434/api/tags | jq '.models[].name'

# Foundry workspace image — pre-bakes the V4 dep tree.
# 10-20 min first build; cached afterwards.
docker build -t probably-nothing-foundry ./backend/foundry_workspace

# Sanity-check the workspace builds end-to-end:
docker run --rm probably-nothing-foundry build

# Build and start the stack with the DGX overlay.
docker compose -f docker-compose.yml -f docker-compose.dgx.yml up --build
```

The overlay sets `PN_LLM_BACKEND=ollama`, points the backend at
`host.docker.internal:11434`, and pins the backend container to 32 GB —
plenty of headroom for the model to coexist on the host.

## 2. Swapping to vLLM / TGI / LM Studio

Any OpenAI-compatible `/v1/chat/completions` endpoint works through the
`openai` backend. Export before `up`:

```bash
export PN_LLM_BACKEND=openai
export PN_LLM_ENDPOINT=http://localhost:8000/v1   # wherever vLLM/TGI is listening
export PN_LLM_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct
export PN_LLM_API_KEY=dummy                        # TGI/vLLM usually ignore this
docker compose -f docker-compose.yml -f docker-compose.dgx.yml up --build
```

## 3. LLM routing smoke test

Verify the adapter resolves correctly without running a full analysis:

```bash
docker compose exec backend python -c "
import asyncio
from engine.llm import build_llm

async def go():
    llm = build_llm()
    print('backend:', llm.backend, 'model:', llm.model)
    reply = await llm.complete('Reply with OK.', timeout=30)
    print('reply:', (reply or '(none)')[:200])

asyncio.run(go())
"
```

Expected output: `backend: ollama model: qwen2.5-coder:latest` followed by
a short reply. If `reply: (none)` appears, the backend returned or timed out
— verify Ollama is up and the model tag is pulled.

## 4. End-to-end demo run

Point the frontend at a known-good hook (spec recommends PointsHook) and
confirm the HUD shows `status: Parametric tier converged. Requesting
LLM-assisted mutations...` before completion. The final `complete` event
includes `llm_backend` and `llm_model` so you can confirm routing from the
message log alone.

## 5. Canonical env reference

See `backend/.env.example`. All tunables (`PN_LLM_*`, `PN_WALL_BUDGET`,
`PN_MAX_CONCURRENCY`) can be set in a `.env` file next to the compose
files and they will be picked up automatically.
