"""Select an LLM client from agents.yaml / env (Ollama or lab vLLM)."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from core.llm.base import LLMClient
from core.llm.ollama import OllamaClient, resolve_ollama_base_url
from core.llm.vllm import (
    VllmClient,
    fetch_served_model_ids,
    resolve_vllm_api_key,
    resolve_vllm_api_timeout,
    resolve_vllm_base_url,
    resolve_vllm_model,
)

logger = logging.getLogger(__name__)

LLM_PROVIDER_ENV = "LLM_PROVIDER"
SERVED_MODEL_STRICT_ENV = "EA_REQUIRE_SERVED_MODEL"
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


def build_llm_client(
    llm_cfg: Optional[Dict[str, Any]] = None, prefer_config: bool = False
) -> LLMClient:
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
            base_url=resolve_vllm_base_url(cfg, prefer_config=prefer_config),
            model=resolve_vllm_model(cfg, prefer_config=prefer_config),
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


def probe_served_model(llm_cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Ask the backend which model it will answer with, before the run spends on it.

    Returns None where the question is not meaningful: Ollama pins the tag on
    the host running it, so the requested id is the served id.

    ``status: "mismatch"`` is the case worth an early stop. VllmClient.generate
    swallows the 404 the miss produces and returns "", so the run finishes,
    writes a summary naming the model it *asked* for, and leaves the only
    evidence in ``llm_usage.failed_calls`` -- discovered, if at all, an hour of
    GPU time later.
    """
    provider = resolve_llm_provider(llm_cfg)
    if provider != "vllm":
        return None
    requested = resolve_vllm_model(llm_cfg)
    served = fetch_served_model_ids(llm_cfg)
    record: Dict[str, Any] = {"requested": requested}
    if served is None:
        record["status"] = "unknown"
        return record
    record["served"] = served
    record["status"] = "ok" if requested in served else "mismatch"
    return record


def served_model_is_strict() -> bool:
    """Whether a mismatch should stop the run rather than warn.

    Opt-in, so a sweep already in flight keeps the behaviour it started with.
    The warning below is not opt-in.
    """
    return os.environ.get(SERVED_MODEL_STRICT_ENV, "").strip().lower() in {"1", "true", "yes"}


def require_served_model(record: Optional[Dict[str, Any]]) -> None:
    """Log the probe, and raise on a mismatch when strict mode is on."""
    if not record:
        return
    status = record.get("status")
    if status == "ok":
        return
    if status == "unknown":
        logger.warning(
            "Could not confirm the served model for %r; the run will proceed "
            "and summary.llm.served.status records the gap.",
            record.get("requested"),
        )
        return
    message = (
        f"Requested model {record.get('requested')!r} is not served here; "
        f"the server offers {record.get('served')!r}. Every generate() call "
        f"will fail and return an empty string."
    )
    if served_model_is_strict():
        raise RuntimeError(message)
    logger.error("%s Set %s=1 to make this stop the run.", message, SERVED_MODEL_STRICT_ENV)
