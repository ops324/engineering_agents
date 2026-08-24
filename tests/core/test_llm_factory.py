"""Tests for Ollama vs vLLM provider selection."""

from __future__ import annotations

import pytest

from core.llm.factory import (
    build_llm_client,
    describe_llm_target,
    probe_served_model,
    require_served_model,
    resolve_llm_provider,
)
from core.llm.ollama import OllamaClient
from core.llm.vllm import VllmClient


def test_resolve_provider_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert resolve_llm_provider({}) == "ollama"
    assert resolve_llm_provider({"provider": "vllm"}) == "vllm"


def test_resolve_provider_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    assert resolve_llm_provider({"provider": "ollama"}) == "vllm"


def test_resolve_provider_rejects_unknown(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        resolve_llm_provider({"provider": "llamacpp"})


def test_build_client_ollama_by_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    client = build_llm_client(
        {"base_url": "http://localhost:11434", "model": "gemma4:e4b"}
    )
    assert isinstance(client, OllamaClient)
    assert client.model == "gemma4:e4b"


def test_build_client_vllm_replaces_ollama_yaml_defaults(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    monkeypatch.delenv("VLLM_API_TIMEOUT", raising=False)
    client = build_llm_client(
        {
            "provider": "vllm",
            "base_url": "http://localhost:11434",
            "model": "gemma4:e4b",
            "think": False,
        }
    )
    assert isinstance(client, VllmClient)
    assert client.base_url == "http://10.10.0.108:8000/v1"
    assert client.model == "qwen3-8b"
    assert client.think is False
    assert client._max_concurrency == 100
    assert client.api_timeout == 300


def test_build_client_vllm_ignores_short_ollama_api_timeout(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_API_TIMEOUT", raising=False)
    client = build_llm_client({"provider": "vllm", "api_timeout": 20})
    assert isinstance(client, VllmClient)
    assert client.api_timeout == 300


def test_build_client_vllm_honors_timeout_env(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("VLLM_API_TIMEOUT", "90")
    client = build_llm_client({"provider": "vllm", "api_timeout": 20})
    assert isinstance(client, VllmClient)
    assert client.api_timeout == 90


def test_build_client_ollama_keeps_model_cap_without_yaml_concurrency(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = build_llm_client({"provider": "ollama", "model": "qwen3.5:9b"})
    assert isinstance(client, OllamaClient)
    assert client._max_concurrency == 8


def test_build_client_honors_max_concurrency(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    client = build_llm_client({"provider": "vllm", "max_concurrency": 48})
    assert isinstance(client, VllmClient)
    assert client._max_concurrency == 48


def test_describe_llm_target_vllm(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    provider, url, model = describe_llm_target({"provider": "vllm"})
    assert provider == "vllm"
    assert url.endswith("/v1")
    assert model == "qwen3-8b"


class _ModelsResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _serving(monkeypatch, *ids):
    monkeypatch.setattr(
        "core.llm.vllm.requests.get",
        lambda *a, **k: _ModelsResponse({"data": [{"id": i} for i in ids]}),
    )


def test_probe_served_model_skips_ollama(monkeypatch):
    """Ollama pins the tag on the host, so requested and served cannot diverge."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert probe_served_model({"provider": "ollama", "model": "gemma4:e4b"}) is None


def test_probe_served_model_confirms_a_match(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    _serving(monkeypatch, "qwen3.5-9b", "Qwen/Qwen3-8B")
    record = probe_served_model({"provider": "vllm", "model": "qwen3.5-9b"})
    assert record["status"] == "ok"
    assert record["requested"] == "qwen3.5-9b"
    assert "qwen3.5-9b" in record["served"]


def test_probe_served_model_catches_the_port_that_moved(monkeypatch):
    """:8000 went qwen3-8b -> 27b and back inside four days; the run must say so."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    _serving(monkeypatch, "qwen3.8-27b-uncensored")
    record = probe_served_model({"provider": "vllm", "model": "qwen3.5-9b"})
    assert record["status"] == "mismatch"
    assert record["served"] == ["qwen3.8-27b-uncensored"]


def test_probe_served_model_reports_an_unreachable_server_as_unknown(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)

    def boom(*args, **kwargs):
        raise ConnectionError("VPN down")

    monkeypatch.setattr("core.llm.vllm.requests.get", boom)
    record = probe_served_model({"provider": "vllm", "model": "qwen3.5-9b"})
    assert record["status"] == "unknown"
    assert "served" not in record


def test_require_served_model_warns_by_default(monkeypatch):
    """A sweep already in flight keeps the behaviour it started with."""
    monkeypatch.delenv("EA_REQUIRE_SERVED_MODEL", raising=False)
    require_served_model({"requested": "a", "served": ["b"], "status": "mismatch"})


def test_require_served_model_stops_the_run_when_strict(monkeypatch):
    monkeypatch.setenv("EA_REQUIRE_SERVED_MODEL", "1")
    with pytest.raises(RuntimeError, match="not served here"):
        require_served_model({"requested": "a", "served": ["b"], "status": "mismatch"})


def test_require_served_model_never_stops_on_unknown(monkeypatch):
    """Unreachable is not evidence of a wrong model; generate() will say so."""
    monkeypatch.setenv("EA_REQUIRE_SERVED_MODEL", "1")
    require_served_model({"requested": "a", "status": "unknown"})
