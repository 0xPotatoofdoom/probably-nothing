"""
LLM adapter layer.

Env-driven selection:
  PN_LLM_BACKEND   ollama | openai          (default: ollama)
  PN_LLM_MODEL     model tag                (default: qwen3-coder-next:latest)
  PN_LLM_ENDPOINT  base URL                 (default: http://host.docker.internal:11434 for ollama,
                                                       http://localhost:8080/v1 for openai)
  PN_LLM_API_KEY   bearer token             (optional; openai-compat only)

All backends expose the same async `complete(prompt, timeout)` interface and keep network
I/O off the event loop via httpx's async client.
"""
from __future__ import annotations

import asyncio
import os
from typing import Protocol, Optional

import httpx


DEFAULT_MODEL = "qwen3-coder-next:latest"
DEFAULT_OLLAMA_ENDPOINT = "http://host.docker.internal:11434"
DEFAULT_OPENAI_ENDPOINT = "http://localhost:8080/v1"


class LLMClient(Protocol):
    backend: str
    model: str

    async def complete(self, prompt: str, timeout: float = 120.0) -> Optional[str]:
        ...


class OllamaLLM:
    backend = "ollama"

    def __init__(self, model: str, endpoint: str):
        self.model = model
        self.endpoint = endpoint.rstrip("/")

    async def complete(self, prompt: str, timeout: float = 120.0) -> Optional[str]:
        url = f"{self.endpoint}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {"temperature": 0.4, "num_predict": 8192},
        }
        try:
            async def _stream() -> str:
                chunks = []
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0)) as client:
                    async with client.stream("POST", url, json=payload) as r:
                        r.raise_for_status()
                        async for line in r.aiter_lines():
                            if not line:
                                continue
                            import json as _json
                            data = _json.loads(line)
                            chunks.append(data.get("response", ""))
                            if data.get("done"):
                                break
                return "".join(chunks)
            return await asyncio.wait_for(_stream(), timeout=timeout)
        except Exception:
            return None


class OpenAICompatLLM:
    """Works with vLLM / TGI / LM Studio / OpenAI — any /v1/chat/completions endpoint."""

    backend = "openai"

    def __init__(self, model: str, endpoint: str, api_key: Optional[str]):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key

    async def complete(self, prompt: str, timeout: float = 120.0) -> Optional[str]:
        url = f"{self.endpoint}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 8192,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except Exception:
            return None


DEFAULT_FAST_MODEL = "qwen2.5:3b"


def build_llm() -> LLMClient:
    backend = os.getenv("PN_LLM_BACKEND", "ollama").lower()
    model = os.getenv("PN_LLM_MODEL", DEFAULT_MODEL)
    if backend == "openai":
        endpoint = os.getenv("PN_LLM_ENDPOINT", DEFAULT_OPENAI_ENDPOINT)
        api_key = os.getenv("PN_LLM_API_KEY")
        return OpenAICompatLLM(model=model, endpoint=endpoint, api_key=api_key)
    endpoint = os.getenv("PN_LLM_ENDPOINT", DEFAULT_OLLAMA_ENDPOINT)
    return OllamaLLM(model=model, endpoint=endpoint)


def build_fast_llm() -> LLMClient:
    """
    Return a fast, small LLM for repair/fix passes where latency matters more
    than reasoning depth. Uses PN_LLM_FAST_MODEL (default: qwen2.5:3b).
    Falls back to the main LLM if the fast model env var points to the same
    backend or fast model isn't configured.
    """
    backend = os.getenv("PN_LLM_BACKEND", "ollama").lower()
    model = os.getenv("PN_LLM_FAST_MODEL", DEFAULT_FAST_MODEL)
    endpoint = os.getenv("PN_LLM_ENDPOINT", DEFAULT_OLLAMA_ENDPOINT)
    if backend == "openai":
        # For openai-compat backends, use the same endpoint but smaller model
        api_key = os.getenv("PN_LLM_API_KEY")
        return OpenAICompatLLM(model=model, endpoint=endpoint, api_key=api_key)
    return OllamaLLM(model=model, endpoint=endpoint)
