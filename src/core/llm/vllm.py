"""OpenAI-compatible client for the lab vLLM server.

The GPU box (gpu-sv-008) serves on the lab LAN or via VPN; not a public
address. See https://github.com/hirototamura/vllm_server .

Which model sits behind which port is decided at the server, not here, and it
changes faster than this file does: :8000 went qwen3-8b -> qwen3.8-27b-uncensored
on 2026-08-19 and back to Qwen/Qwen3-8B on 2026-08-22, with :8001 down
throughout. Ask the server rather than trusting this file -- ``GET
{base_url}/models`` returns the ids actually being served. Code that assumes a
port implies a model will run the wrong arm of an experiment and say nothing
about it.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

from core.llm.base import LLMClient

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://10.10.0.108:8000/v1"
DEFAULT_MODEL = "qwen3-8b"
DEFAULT_API_KEY = "dummy"
VLLM_BASE_URL_ENV = "VLLM_BASE_URL"
VLLM_API_KEY_ENV = "VLLM_API_KEY"
VLLM_API_TIMEOUT_ENV = "VLLM_API_TIMEOUT"
VLLM_MAX_MODEL_LEN_ENV = "VLLM_MAX_MODEL_LEN"
API_TIMEOUT = 300
CONNECTION_CHECK_TIMEOUT = 5
# Must match vllm-server/scripts/serve_small.sh --max-model-len.
DEFAULT_MAX_MODEL_LEN = 32768
_CHAT_TEMPLATE_OVERHEAD_TOKENS = 256
# Conservative chars/token so we stay under the server cap without a tokenizer.
_CHARS_PER_TOKEN = 3

# Lab server: 8B is 6-way replicated (theoretical ~384); 32B is capped at 32.
#
# Keyed on parameter count, not on substrings of the model id. Substring
# matching reads "qwen3.8-27b-uncensored" as a 7B/8B because "27b" contains
# "7b", and hands a 27B model a 100-way in-flight cap on a shared GPU. Sizes
# are matched at a digit boundary and compared numerically instead, so an id
# nobody anticipated still lands in the right bucket.
_MODEL_CONCURRENCY_BY_SIZE_B = [
    (70, 16),
    (24, 32),   # 27B and 32B belong together; the old table had no 27B entry
    (12, 64),
    (0, 100),
]
_CONCURRENCY_FALLBACK = 64

# "8b" / "27b" / "3.8b", but not the "3.8" in "qwen3.8-27b" (not followed by b)
# and not the "7b" inside "27b" (a digit precedes it).
_SIZE_IN_MODEL_ID = re.compile(r"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*b\b")


def parse_model_size_b(model: str) -> Optional[float]:
    """Parameter count in billions read off a served-model id, or None.

    Returns the largest match, so an id carrying more than one size settles on
    the bigger one rather than on whichever appears first.
    """
    matches = _SIZE_IN_MODEL_ID.findall((model or "").lower())
    if not matches:
        return None
    return max(float(m) for m in matches)


def _default_concurrency(model: str) -> int:
    size = parse_model_size_b(model)
    if size is None:
        return _CONCURRENCY_FALLBACK
    for threshold, limit in _MODEL_CONCURRENCY_BY_SIZE_B:
        if size >= threshold:
            return limit
    return _CONCURRENCY_FALLBACK


def normalize_vllm_base_url(url: str) -> str:
    """Ensure the OpenAI-compatible root ends with /v1."""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return DEFAULT_BASE_URL
    if cleaned.endswith("/v1"):
        return cleaned
    return f"{cleaned}/v1"


def looks_like_ollama_url(url: str) -> bool:
    lowered = (url or "").lower()
    return ":11434" in lowered or "/api/" in lowered


def resolve_vllm_base_url(llm_cfg: Optional[Dict[str, Any]] = None) -> str:
    """Resolve vLLM URL: env VLLM_BASE_URL overrides yaml; Ollama URLs fall back to lab default."""
    env_url = os.environ.get(VLLM_BASE_URL_ENV, "").strip()
    if env_url:
        return normalize_vllm_base_url(env_url)
    cfg_url = str((llm_cfg or {}).get("base_url", "")).strip()
    if not cfg_url or looks_like_ollama_url(cfg_url):
        return DEFAULT_BASE_URL
    return normalize_vllm_base_url(cfg_url)


def resolve_vllm_model(llm_cfg: Optional[Dict[str, Any]] = None) -> str:
    """Use yaml/env model when it looks like a vLLM id; otherwise the lab 8B default."""
    env_model = os.environ.get("VLLM_MODEL", "").strip()
    if env_model:
        return env_model
    cfg_model = str((llm_cfg or {}).get("model", "")).strip()
    if not cfg_model or ":" in cfg_model:
        # Ollama tags look like gemma4:e4b / qwen3.5:9b — not vLLM served-model ids.
        return DEFAULT_MODEL
    return cfg_model


def resolve_vllm_api_key(llm_cfg: Optional[Dict[str, Any]] = None) -> str:
    env_key = os.environ.get(VLLM_API_KEY_ENV, "").strip()
    if env_key:
        return env_key
    cfg_key = str((llm_cfg or {}).get("api_key", "")).strip()
    return cfg_key or DEFAULT_API_KEY


def resolve_vllm_api_timeout(llm_cfg: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """vLLM HTTP timeout in seconds.

    YAML ``api_timeout`` is Ollama-oriented (often 10–20s) and is ignored so
    parallel lab rounds keep the 300s client default. Override with
    ``VLLM_API_TIMEOUT``.
    """
    _ = llm_cfg
    env_raw = os.environ.get(VLLM_API_TIMEOUT_ENV, "").strip()
    if not env_raw:
        return None
    return int(env_raw)


def vllm_auth_headers(llm_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    return {"Authorization": f"Bearer {resolve_vllm_api_key(llm_cfg)}"}


def resolve_vllm_max_model_len() -> int:
    env_raw = os.environ.get(VLLM_MAX_MODEL_LEN_ENV, "").strip()
    if env_raw:
        return int(env_raw)
    return DEFAULT_MAX_MODEL_LEN


def _clamp_completion_tokens(max_tokens: int) -> int:
    """Leave at least one prompt token whenever the window is larger than overhead."""
    requested = max(1, int(max_tokens))
    room = resolve_vllm_max_model_len() - _CHAT_TEMPLATE_OVERHEAD_TOKENS
    if room <= 1:
        return max(1, room)
    return min(requested, room - 1)


def _fit_prompt_to_context(prompt: str, max_tokens: int) -> str:
    """Keep instructions (head) and recent context (tail) under the server window."""
    budget_tokens = max(
        0,
        resolve_vllm_max_model_len() - int(max_tokens) - _CHAT_TEMPLATE_OVERHEAD_TOKENS,
    )
    budget_chars = budget_tokens * _CHARS_PER_TOKEN
    if len(prompt) <= budget_chars:
        return prompt
    marker = "\n\n[... truncated to fit context ...]\n\n"
    if budget_chars <= len(marker):
        return prompt[:budget_chars]
    head = min(12_000, budget_chars // 4)
    tail = budget_chars - head - len(marker)
    if tail <= 0:
        return prompt[:budget_chars]
    logger.warning(
        "VllmClient truncating prompt from %s to %s chars (max_model_len=%s)",
        len(prompt),
        head + len(marker) + tail,
        resolve_vllm_max_model_len(),
    )
    return prompt[:head] + marker + prompt[-tail:]


class VllmClient(LLMClient):
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.7,
        max_tokens: int = 200,
        repeat_penalty: float = 1.1,
        min_p: float = 0.05,
        max_concurrency: int = -1,
        think: Optional[bool] = None,
        api_timeout: Optional[int] = None,
        api_key: str = DEFAULT_API_KEY,
        seed: Optional[int] = None,
    ):
        resolved = _default_concurrency(model) if max_concurrency == -1 else max_concurrency
        super().__init__(max_concurrency=resolved, seed=seed)
        self.base_url = normalize_vllm_base_url(base_url)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.repeat_penalty = repeat_penalty
        self.min_p = min_p
        self.think = think
        self.api_timeout = api_timeout or API_TIMEOUT
        self.api_key = api_key or DEFAULT_API_KEY
        self.api_url = f"{self.base_url}/chat/completions"
        self._pool_size = max(8, int(resolved) if resolved else 8)
        self._local = threading.local()

    @property
    def _session(self) -> requests.Session:
        # requests.Session is not thread-safe; parallel generate_async workers
        # each get their own session / connection pool.
        session = getattr(self._local, "session", None)
        if session is None:
            session = _build_http_session(self._pool_size)
            self._local.session = session
        return session

    def generate(self, prompt: str) -> str:
        max_tokens = _clamp_completion_tokens(self.max_tokens)
        if max_tokens != self.max_tokens:
            logger.warning(
                "VllmClient clamping max_tokens from %s to %s (max_model_len=%s)",
                self.max_tokens,
                max_tokens,
                resolve_vllm_max_model_len(),
            )
        prompt = _fit_prompt_to_context(prompt, max_tokens)
        recorded = False
        try:
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                "repetition_penalty": self.repeat_penalty,
            }
            if self.seed is not None:
                payload["seed"] = self.seed
            # vLLM MTP / speculative decoding rejects min_p and logit_bias.
            if self.think is not None:
                payload["chat_template_kwargs"] = {"enable_thinking": bool(self.think)}
            response = self._session.post(
                self.api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.api_timeout,
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage") or {}
            self._record_call(
                ok=True,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
            recorded = True
            message = (body.get("choices") or [{}])[0].get("message") or {}
            content = message.get("content") or ""
            return str(content).strip()
        except Exception as e:
            if not recorded:
                self._record_call(ok=False)
            detail = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    detail = (resp.text or "")[:500]
                except Exception:
                    detail = ""
            if detail:
                logger.error("VllmClient.generate error: %s | %s", e, detail)
            else:
                logger.error("VllmClient.generate error: %s", e)
            return ""

    def check_connection(self) -> bool:
        try:
            response = self._session.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=CONNECTION_CHECK_TIMEOUT,
            )
            return response.status_code == 200
        except Exception:
            return False


def _build_http_session(pool_size: int) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
