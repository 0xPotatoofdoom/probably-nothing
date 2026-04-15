from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
from engine.evaluator import HookEvaluator

app = FastAPI(title="Probably Nothing", description="Autonomous audit tool for Uniswap V4 hooks")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

evaluator = HookEvaluator()

@app.websocket("/ws/analyze")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_text()
    payload = json.loads(data)
    github_url = payload["url"]
    num_agents = payload.get("num_agents", 6)
    async for update in evaluator.analyze(github_url, num_agents=num_agents):
        await websocket.send_json(update)
    await websocket.close()

@app.get("/health")
async def health():
    return {"status": "ok", "project": "Probably Nothing"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
