"""Select an LLM client from agents.yaml / env (Ollama or lab vLLM)."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from core.llm.base import LLMClient
from core.llm.ollama import OllamaClient, resolve_ollama_base_url
from core.llm.vllm import (
    VllmClient,
    resolve_vllm_api_key,
    resolve_vllm_api_timeout,
    resolve_vllm_base_url,
    resolve_vllm_model,
)

LLM_PROVIDER_ENV = "LLM_PROVIDER"
VALID_LLM_PROVIDERS = frozenset({"ollama", "vllm"})
DEFAULT_PROVIDER = "ollama"


def resolve_llm_provider(llm_cfg: Optional[Dict[str, Any]] = None) -> str:
    """Resolve provider: env LLM_PROVIDER overrides agents.yaml llm.provider."""
    env_provider = os.environ.get(LLM_PROVIDER_ENV, "").strip().lower()
    if env_provider:
        provider = env_provider
    else:
        provider = str((llm_cfg or {}).get("provider", DEFAULT_PROVIDER)).strip().lower() or DEFAULT_PROVIDER
    if provider not in VALID_LLM_PROVIDERS:
        allowed = ", ".join(sorted(VALID_LLM_PROVIDERS))
        raise ValueError(f"Unsupported LLM provider: {provider!r}. Choose one of: {allowed}")
    return provider


def resolve_llm_base_url(llm_cfg: Optional[Dict[str, Any]] = None) -> str:
    provider = resolve_llm_provider(llm_cfg)
    if provider == "vllm":
        return resolve_vllm_base_url(llm_cfg)
    return resolve_ollama_base_url(llm_cfg)


def resolve_llm_model(llm_cfg: Optional[Dict[str, Any]] = None) -> str:
    provider = resolve_llm_provider(llm_cfg)
    if provider == "vllm":
        return resolve_vllm_model(llm_cfg)
    return str((llm_cfg or {}).get("model", "llama3.2")) or "llama3.2"


def describe_llm_target(llm_cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """Return (provider, base_url, model) after applying env/yaml defaults."""
    cfg = llm_cfg or {}
    provider = resolve_llm_provider(cfg)
    return provider, resolve_llm_base_url(cfg), resolve_llm_model(cfg)


def build_llm_client(llm_cfg: Optional[Dict[str, Any]] = None) -> LLMClient:
    cfg = llm_cfg or {}
    provider = resolve_llm_provider(cfg)
    temperature = float(cfg.get("temperature", 0.45))
    max_tokens = int(cfg.get("max_tokens", 512))
    repeat_penalty = float(cfg.get("repeat_penalty", 1.1))
    min_p = float(cfg.get("min_p", 0.05))
    think = cfg.get("think", False)
    max_concurrency = int(cfg["max_concurrency"]) if cfg.get("max_concurrency") is not None else -1
    # Seed reaches the sampler only if it is threaded through here; recording
    # it in the run summary alone proves nothing about what was sampled.
    seed = int(cfg["seed"]) if cfg.get("seed") is not None else None

    if provider == "vllm":
        return VllmClient(
            base_url=resolve_vllm_base_url(cfg),
            model=resolve_vllm_model(cfg),
            temperature=temperature,
            max_tokens=max_tokens,
            repeat_penalty=repeat_penalty,
            min_p=min_p,
            think=think,
            api_timeout=resolve_vllm_api_timeout(cfg),
            api_key=resolve_vllm_api_key(cfg),
            max_concurrency=max_concurrency,
            seed=seed,
        )
    api_timeout = int(cfg.get("api_timeout", 10))
    return OllamaClient(
        base_url=resolve_ollama_base_url(cfg),
        model=str(cfg.get("model", "llama3.2")),
        temperature=temperature,
        max_tokens=max_tokens,
        repeat_penalty=repeat_penalty,
        repeat_last_n=int(cfg.get("repeat_last_n", 128)),
        min_p=min_p,
        think=think,
        api_timeout=api_timeout,
        max_concurrency=max_concurrency,
        seed=seed,
    )
