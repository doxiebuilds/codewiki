"""
llm.py — local LLM transport (OpenAI-compatible chat completions) + health checks.

codewiki's only LLM dependency is a local OpenAI-compatible server (LM Studio, Ollama's OpenAI
shim, vLLM, etc.) reachable at ``CODEWIKI_LLM_BASE_URL``. No API key is required or sent.

Everything here is stdlib-only so the generator has no third-party HTTP client dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = os.environ.get("CODEWIKI_MODEL", "")
LMSTUDIO_BASE_URL = os.environ.get("CODEWIKI_LLM_BASE_URL", "http://localhost:1234/v1")

# A local reasoning model's <think> pass is pure overhead for bounded summarization and, at a
# small max_tokens, can eat the whole budget before the JSON answer is emitted (empty summary).
# "none" disables it where the backend supports the field; override for backends needing
# "low"/"minimal"/etc., or leave "none" if the backend ignores unknown fields.
REASONING_EFFORT = os.environ.get("CODEWIKI_REASONING_EFFORT", "none")


def lmstudio_up(timeout: float = 2.0) -> bool:
    """True if the local OpenAI-compatible server answers GET /models."""
    try:
        with urllib.request.urlopen(f"{LMSTUDIO_BASE_URL}/models", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def model_available(model: str = DEFAULT_MODEL, timeout: float = 2.0) -> bool:
    """True if `model` is listed by the local server's /models endpoint."""
    try:
        with urllib.request.urlopen(f"{LMSTUDIO_BASE_URL}/models", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return any(item.get("id") == model for item in data.get("data", []))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def lmstudio_chat(prompt: str, *, model: str = DEFAULT_MODEL, system: str = "",
                  max_tokens: int = 512, timeout: int = 120) -> tuple[str, dict]:
    """One chat completion against the local OpenAI-compatible server. Returns (text, usage)."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "reasoning_effort": REASONING_EFFORT,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(f"{LMSTUDIO_BASE_URL}/chat/completions", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = data["choices"][0]
    usage = dict(data.get("usage", {}))
    usage["finish_reason"] = choice.get("finish_reason", "")
    return choice["message"]["content"], usage
