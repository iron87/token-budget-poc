"""
circuit_breaker.py — application-level circuit breaker for LLM calls

LiteLLM handles 429s from providers and routes to fallbacks.
this layer adds a client-side breaker that opens when downstream
is repeatedly failing — preventing cascading failures and giving
the model time to recover.

states:
  CLOSED  — normal operation, requests pass through
  OPEN    — failing fast, no requests sent downstream
  HALF    — one probe request allowed to test recovery

based on the classic circuit breaker pattern (Nygard, Release It!, 2007)
applied to LLM API calls where failure modes are: rate limits, budget
exhaustion, and provider outages.
"""

import asyncio
import time
import httpx
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable, Any


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5      # open after N consecutive failures
    recovery_timeout: float = 60.0  # seconds before trying HALF_OPEN
    success_threshold: int = 2      # consecutive successes to close from HALF_OPEN

    _state: State = field(default=State.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _successes: int = field(default=0, init=False)
    _opened_at: Optional[float] = field(default=None, init=False)

    @property
    def state(self) -> State:
        if self._state == State.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = State.HALF
                self._successes = 0
                print(f"  [{self.name}] circuit → HALF_OPEN (probing recovery)")
        return self._state

    def record_success(self):
        self._failures = 0
        if self._state == State.HALF:
            self._successes += 1
            if self._successes >= self.success_threshold:
                self._state = State.CLOSED
                print(f"  [{self.name}] circuit → CLOSED (recovered after {self._successes} successes)")
        elif self._state == State.CLOSED:
            pass  # normal

    def record_failure(self):
        self._failures += 1
        if self._state == State.HALF:
            self._state = State.OPEN
            self._opened_at = time.monotonic()
            print(f"  [{self.name}] circuit → OPEN (probe failed, resetting timeout)")
        elif self._failures >= self.failure_threshold:
            self._state = State.OPEN
            self._opened_at = time.monotonic()
            print(f"  [{self.name}] circuit → OPEN (threshold={self.failure_threshold} reached)")

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        if self.state == State.OPEN:
            raise RuntimeError(
                f"circuit breaker '{self.name}' is OPEN — "
                f"retry after {self.recovery_timeout - (time.monotonic() - self._opened_at):.0f}s"
            )
        try:
            result = await fn(*args, **kwargs)
            self.record_success()
            return result
        except (httpx.HTTPStatusError, RuntimeError) as e:
            # treat 429 and 500+ as failures; 400 is a client error, don't count it
            status = getattr(e, "response", None)
            if status is not None and status.status_code == 400:
                raise
            self.record_failure()
            raise


# -- example usage --

async def _example():
    breaker = CircuitBreaker(
        name="ministral",
        failure_threshold=3,
        recovery_timeout=30.0,
    )

    async with httpx.AsyncClient() as client:
        async def llm_call():
            resp = await client.post(
                "http://localhost:4000/chat/completions",
                headers={"Authorization": "Bearer sk-dev-master-key"},
                json={
                    "model": "ministral",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 10,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()

        for i in range(10):
            try:
                result = await breaker.call(llm_call)
                print(f"  [{i+1}] ok — state: {breaker.state.value}")
            except RuntimeError as e:
                print(f"  [{i+1}] blocked — {e}")
            except Exception as e:
                print(f"  [{i+1}] error — {e} — state: {breaker.state.value}")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(_example())
