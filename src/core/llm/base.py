import asyncio
import concurrent.futures
import threading
from abc import ABC, abstractmethod
from typing import Dict, Optional

# Sized for a 100-agent simultaneous round against lab vLLM (I/O-bound HTTP).
LLM_THREAD_POOL_WORKERS = 128
_thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=LLM_THREAD_POOL_WORKERS,
    thread_name_prefix="ea-llm",
)


class LLMClient(ABC):
    def __init__(self, max_concurrency: int = 0, seed: Optional[int] = None):
        self._max_concurrency = max(0, int(max_concurrency))
        # threading.Semaphore is loop-agnostic. asyncio.Semaphore created in
        # __init__ (or bound on first asyncio.run) breaks on the next step's
        # asyncio.run — "bound to a different event loop".
        self._semaphore: Optional[threading.BoundedSemaphore] = (
            threading.BoundedSemaphore(self._max_concurrency) if self._max_concurrency > 0 else None
        )
        # Sampling seed. Without it a repetition is a fresh re-roll, not a
        # controlled repeat, so "rerun with only the seed changed" is not a
        # comparison anyone can act on.
        self.seed = None if seed is None else int(seed)
        # Call accounting. Wall-clock is not a budget on a shared GPU: it
        # measures the queue behind you, not the work you asked for. Compute
        # spent is calls and tokens, and they have to be counted where they
        # happen because nothing downstream can reconstruct them.
        self._usage_lock = threading.Lock()
        self._calls = 0
        self._failed_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0

    def _record_call(
        self,
        *,
        ok: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Count one backend round trip. Safe to call from pool threads."""
        with self._usage_lock:
            self._calls += 1
            if not ok:
                self._failed_calls += 1
            self._prompt_tokens += int(prompt_tokens or 0)
            self._completion_tokens += int(completion_tokens or 0)

    def usage(self) -> Dict[str, int]:
        """Cumulative spend for this client. A failed call still costs a slot."""
        with self._usage_lock:
            return {
                "calls": self._calls,
                "failed_calls": self._failed_calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._prompt_tokens + self._completion_tokens,
            }

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Generate text from prompt. Returns empty string on error."""
        ...

    @abstractmethod
    def check_connection(self) -> bool:
        """Check if LLM backend is reachable."""
        ...

    def _generate_limited(self, prompt: str) -> str:
        if self._semaphore is None:
            return self.generate(prompt)
        with self._semaphore:
            return self.generate(prompt)

    async def generate_async(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_thread_pool, self._generate_limited, prompt)
