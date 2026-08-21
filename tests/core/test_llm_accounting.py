"""Spend accounting and seed plumbing on the LLM clients.

These exist because the failure they guard against is silent. A run whose
every call 404s still finishes and still writes a full set of artifacts; the
only thing that says so is ``llm_usage.failed_calls``. And a seed recorded in
the summary but never sent to the sampler makes repetitions look controlled
when they are independent draws.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from core.llm.factory import build_llm_client
from core.llm.ollama import OllamaClient
from core.llm.vllm import VllmClient


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], *, fail: bool = False):
        self._payload = payload
        self._fail = fail

    def raise_for_status(self) -> None:
        if self._fail:
            raise RuntimeError("404 Client Error")

    def json(self) -> Dict[str, Any]:
        return self._payload


def _vllm_body(prompt_tokens: int = 11, completion_tokens: int = 7) -> Dict[str, Any]:
    return {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


# --- vLLM -----------------------------------------------------------------

def test_vllm_counts_calls_and_tokens(monkeypatch):
    client = VllmClient()
    monkeypatch.setattr(
        type(client), "_session",
        property(lambda self: type("S", (), {"post": lambda *a, **k: _FakeResponse(_vllm_body())})()),
    )

    client.generate("a")
    client.generate("b")

    assert client.usage() == {
        "calls": 2,
        "failed_calls": 0,
        "prompt_tokens": 22,
        "completion_tokens": 14,
        "total_tokens": 36,
    }


def test_vllm_counts_a_failed_call_once(monkeypatch):
    client = VllmClient()
    monkeypatch.setattr(
        type(client), "_session",
        property(lambda self: type("S", (), {"post": lambda *a, **k: _FakeResponse({}, fail=True)})()),
    )

    assert client.generate("a") == ""

    usage = client.usage()
    assert usage["calls"] == 1
    assert usage["failed_calls"] == 1
    assert usage["total_tokens"] == 0


def test_vllm_sends_seed_only_when_set(monkeypatch):
    captured: Dict[str, Any] = {}

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        captured.update(json or {})
        return _FakeResponse(_vllm_body())

    for seed, expected in ((None, False), (7, True)):
        captured.clear()
        client = VllmClient(seed=seed)
        monkeypatch.setattr(
            type(client), "_session",
            property(lambda self: type("S", (), {"post": post})()),
        )
        client.generate("a")
        assert ("seed" in captured) is expected
        if expected:
            assert captured["seed"] == 7


# --- Ollama ---------------------------------------------------------------

def test_ollama_counts_calls_and_tokens(monkeypatch):
    body = {"response": "ok", "prompt_eval_count": 5, "eval_count": 3}
    monkeypatch.setattr(
        "core.llm.ollama.requests.post",
        lambda *a, **k: _FakeResponse(body),
    )
    client = OllamaClient()

    client.generate("a")

    assert client.usage()["calls"] == 1
    assert client.usage()["total_tokens"] == 8


def test_ollama_sends_seed_in_options(monkeypatch):
    captured: Dict[str, Any] = {}

    def post(url, json=None, timeout=None):  # noqa: A002
        captured.update(json or {})
        return _FakeResponse({"response": "ok"})

    monkeypatch.setattr("core.llm.ollama.requests.post", post)
    OllamaClient(seed=13).generate("a")

    assert captured["options"]["seed"] == 13


# --- factory --------------------------------------------------------------

@pytest.mark.parametrize("provider", ["ollama", "vllm"])
def test_factory_threads_seed_through(monkeypatch, provider):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = build_llm_client({"provider": provider, "seed": 42})
    assert client.seed == 42


@pytest.mark.parametrize("provider", ["ollama", "vllm"])
def test_factory_leaves_seed_unset_by_default(monkeypatch, provider):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = build_llm_client({"provider": provider})
    assert client.seed is None
