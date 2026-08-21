"""Unit tests for the OpenAI-compatible vLLM client."""

from __future__ import annotations

from core.llm.vllm import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_MODEL_LEN,
    VllmClient,
    _default_concurrency,
    parse_model_size_b,
    _CHAT_TEMPLATE_OVERHEAD_TOKENS,
    _CHARS_PER_TOKEN,
    _clamp_completion_tokens,
    _fit_prompt_to_context,
    looks_like_ollama_url,
    normalize_vllm_base_url,
    resolve_vllm_base_url,
    resolve_vllm_model,
)


def test_normalize_vllm_base_url_appends_v1():
    assert normalize_vllm_base_url("http://10.10.0.108:8000") == "http://10.10.0.108:8000/v1"
    assert normalize_vllm_base_url("http://10.10.0.108:8000/v1/") == "http://10.10.0.108:8000/v1"


def test_looks_like_ollama_url():
    assert looks_like_ollama_url("http://localhost:11434")
    assert looks_like_ollama_url("http://host.docker.internal:11434")
    assert not looks_like_ollama_url("http://10.10.0.108:8000/v1")


def test_resolve_vllm_defaults_when_yaml_still_points_at_ollama(monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    cfg = {"base_url": "http://localhost:11434", "model": "gemma4:e4b"}
    assert resolve_vllm_base_url(cfg) == DEFAULT_BASE_URL
    assert resolve_vllm_model(cfg) == "qwen3-8b"


def test_resolve_vllm_keeps_explicit_lab_endpoint(monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    cfg = {"base_url": "http://10.10.0.108:8001", "model": "qwen3-32b"}
    assert resolve_vllm_base_url(cfg) == "http://10.10.0.108:8001/v1"
    assert resolve_vllm_model(cfg) == "qwen3-32b"


def test_resolve_vllm_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("VLLM_MODEL", "qwen3-32b")
    cfg = {"base_url": "http://10.10.0.108:8000/v1", "model": "qwen3-8b"}
    assert resolve_vllm_base_url(cfg) == "http://127.0.0.1:8000/v1"
    assert resolve_vllm_model(cfg) == "qwen3-32b"


def test_vllm_generate_posts_chat_completions(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "  hello  "}}]}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    client = VllmClient(think=False, api_timeout=12)
    monkeypatch.setattr(client._session, "post", fake_post)
    assert client.generate("ping") == "hello"
    assert captured["url"] == "http://10.10.0.108:8000/v1/chat/completions"
    assert captured["json"]["model"] == "qwen3-8b"
    assert captured["json"]["messages"] == [{"role": "user", "content": "ping"}]
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "min_p" not in captured["json"]
    assert captured["headers"]["Authorization"] == "Bearer dummy"


def test_vllm_check_connection_hits_models(monkeypatch):
    class FakeResponse:
        status_code = 200

    client = VllmClient()
    monkeypatch.setattr(client._session, "get", lambda *args, **kwargs: FakeResponse())
    assert client.check_connection() is True


def test_vllm_generate_returns_empty_on_error(monkeypatch):
    client = VllmClient()
    monkeypatch.setattr(
        client._session,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert client.generate("ping") == ""


def test_vllm_8b_default_concurrency_covers_hundred_agents():
    client = VllmClient(model="qwen3-8b")
    assert client._max_concurrency == 100


def test_model_size_is_read_at_a_digit_boundary():
    # The "7b" inside "27b" must not be read as a 7B: a digit precedes it.
    assert parse_model_size_b("qwen3.8-27b-uncensored") == 27.0
    assert parse_model_size_b("qwen3-8b") == 8.0
    assert parse_model_size_b("qwen3-32b") == 32.0
    assert parse_model_size_b("qwen2.5:14b") == 14.0
    assert parse_model_size_b("unlabelled-model") is None


def test_27b_is_not_handed_the_8b_concurrency_cap():
    # Regression: substring matching gave this model a 100-way in-flight cap
    # on a shared GPU because "27b" contains "7b".
    assert _default_concurrency("qwen3.8-27b-uncensored") == 32
    assert VllmClient(model="qwen3.8-27b-uncensored")._max_concurrency == 32


def test_known_model_caps_are_unchanged_by_the_size_rewrite():
    assert _default_concurrency("qwen3-8b") == 100
    assert _default_concurrency("qwen3-32b") == 32
    assert _default_concurrency("qwen2.5:14b") == 64
    assert _default_concurrency("llama-3.1-70b") == 16
    # No size in the id: fall back rather than guess.
    assert _default_concurrency("unlabelled-model") == 64


def test_vllm_sessions_are_thread_local():
    import threading

    client = VllmClient()
    # Hold the sessions, not their ids: a thread-local session is collected
    # when its thread dies, and CPython hands the next allocation the same
    # address often enough to fail this test at random.
    sessions: list[object] = []
    barrier = threading.Barrier(2)

    def grab() -> None:
        barrier.wait()
        sessions.append(client._session)

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


def test_fit_prompt_keeps_short_prompts():
    assert _fit_prompt_to_context("ping", max_tokens=768) == "ping"


def test_fit_prompt_keeps_head_and_tail(monkeypatch):
    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", "2048")
    prompt = "HEAD" + ("x" * 20_000) + "TAIL"
    fitted = _fit_prompt_to_context(prompt, max_tokens=128)
    assert fitted.startswith("HEAD")
    assert fitted.endswith("TAIL")
    assert "truncated to fit context" in fitted
    assert len(fitted) < len(prompt)
    assert DEFAULT_MAX_MODEL_LEN == 32768


def test_fit_prompt_allows_budget_below_old_char_floor(monkeypatch):
    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", "1024")
    fitted = _fit_prompt_to_context("x" * 5000, max_tokens=768)
    assert len(fitted) < 1024
    assert len(fitted) <= max(
        0, (1024 - 768 - _CHAT_TEMPLATE_OVERHEAD_TOKENS) * _CHARS_PER_TOKEN
    )


def test_fit_prompt_does_not_restore_full_string_when_tail_is_zero(monkeypatch):
    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", "400")
    prompt = "HEAD" + ("x" * 20_000) + "TAIL"
    fitted = _fit_prompt_to_context(prompt, max_tokens=128)
    budget_chars = (400 - 128 - _CHAT_TEMPLATE_OVERHEAD_TOKENS) * _CHARS_PER_TOKEN
    assert len(fitted) <= budget_chars
    assert fitted != prompt
    assert not fitted.endswith("TAIL")


def test_clamp_completion_tokens_leaves_prompt_room(monkeypatch):
    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", "1024")
    clamped = _clamp_completion_tokens(768)
    assert clamped < 768
    assert clamped + 1 + _CHAT_TEMPLATE_OVERHEAD_TOKENS <= 1024


def test_vllm_generate_clamps_output_when_window_is_tight(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", "1024")
    client = VllmClient(max_tokens=768)
    monkeypatch.setattr(client._session, "post", fake_post)
    assert client.generate("x" * 5000) == "ok"
    max_tokens = captured["json"]["max_tokens"]
    prompt = captured["json"]["messages"][0]["content"]
    prompt_tokens = (len(prompt) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
    assert max_tokens + prompt_tokens + _CHAT_TEMPLATE_OVERHEAD_TOKENS <= 1024
