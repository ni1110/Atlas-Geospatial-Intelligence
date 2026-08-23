"""Budget, idempotency, circuit breaker, timeout, retry, and fallback."""

from __future__ import annotations

import hashlib
import json
import random
import socket
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol

from renewable_site_agent.config import load_config
from renewable_site_agent.schemas import ErrorCode, ToolResult


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class ExecutionBudget:
    max_steps: int = 20
    max_tool_calls: int = 30
    max_llm_calls: int = 4
    max_total_runtime_seconds: float = 1800
    max_retries_per_step: int = 3
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    tool_calls: int = 0
    llm_calls: int = 0

    @classmethod
    def configured(cls) -> "ExecutionBudget":
        values = load_config("reliability.yaml").get("execution_budget", {})
        return cls(**values)

    def check(self, *, step: bool = False, tool: bool = False, llm: bool = False) -> None:
        if time.monotonic() - self.started_at > self.max_total_runtime_seconds:
            raise BudgetExceeded("max_total_runtime exceeded")
        if step:
            self.steps += 1
        if tool:
            self.tool_calls += 1
        if llm:
            self.llm_calls += 1
        if self.steps > self.max_steps:
            raise BudgetExceeded("max_steps exceeded")
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded("max_tool_calls exceeded")
        if self.llm_calls > self.max_llm_calls:
            raise BudgetExceeded("max_llm_calls exceeded")


class BudgetExceeded(RuntimeError):
    pass


class LoopDetected(RuntimeError):
    pass


@dataclass
class LoopDetector:
    threshold: int = 3
    records: list[tuple[str, str, str, str]] = field(default_factory=list)

    def record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        state: Any,
    ) -> None:
        current = (
            tool_name,
            stable_hash(arguments),
            stable_hash(result.model_dump(mode="json")),
            stable_hash(state),
        )
        self.records.append(current)
        if len(self.records) >= self.threshold:
            tail = self.records[-self.threshold :]
            if len(set(tail)) == 1:
                raise LoopDetected(f"Repeated identical tool call without state change: {tool_name}")


class HealthBackend(Protocol):
    def get_provider_health(self, provider: str) -> dict[str, Any] | None: ...
    def set_provider_health(self, provider: str, value: dict[str, Any]) -> None: ...


@dataclass
class CircuitBreaker:
    provider: str
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 60
    backend: HealthBackend | None = None
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    recovery_attempts: int = 0

    def __post_init__(self) -> None:
        if self.backend:
            stored = self.backend.get_provider_health(self.provider)
            if stored:
                self.state = CircuitState(stored.get("state", "CLOSED"))
                self.consecutive_failures = int(stored.get("consecutive_failures", 0))
                self.opened_at = stored.get("opened_at")
                self.recovery_attempts = int(stored.get("recovery_attempts", 0))

    def _persist(self) -> None:
        if self.backend:
            self.backend.set_provider_health(
                self.provider,
                {
                    "state": self.state.value,
                    "consecutive_failures": self.consecutive_failures,
                    "opened_at": self.opened_at,
                    "recovery_attempts": self.recovery_attempts,
                },
            )

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and time.time() - self.opened_at >= self.recovery_timeout_seconds:
                self.state = CircuitState.HALF_OPEN
                self.recovery_attempts += 1
                self._persist()
                return True
            return False
        return True

    def success(self) -> None:
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.opened_at = None
        self._persist()

    def failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == CircuitState.HALF_OPEN or self.consecutive_failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
        self._persist()


def classify_exception(exc: BaseException, provider: str | None = None) -> ToolResult:
    text = str(exc).lower()
    if isinstance(exc, (TimeoutError, FutureTimeout, socket.timeout)):
        code, retryable = ErrorCode.TIMEOUT, True
    elif isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        code, retryable = ErrorCode.RATE_LIMIT, True
    elif isinstance(exc, urllib.error.HTTPError) and exc.code >= 500:
        code, retryable = ErrorCode.PROVIDER_UNAVAILABLE, True
    elif isinstance(exc, (urllib.error.URLError, ConnectionError, socket.error)):
        code, retryable = ErrorCode.TRANSIENT_NETWORK_ERROR, True
    elif "auth" in text or "401" in text or "403" in text:
        code, retryable = ErrorCode.AUTH_ERROR, False
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        code, retryable = ErrorCode.INVALID_ARGUMENT, False
    else:
        code, retryable = ErrorCode.FATAL_ERROR, False
    return ToolResult.fail(
        code,
        f"{type(exc).__name__}: {exc}",
        retryable=retryable,
        metadata={"provider": provider},
    )


AttemptHook = Callable[[dict[str, Any]], None]


class ReliableToolExecutor:
    def __init__(
        self,
        *,
        budget: ExecutionBudget | None = None,
        loop_detector: LoopDetector | None = None,
        health_backend: HealthBackend | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        attempt_hook: AttemptHook | None = None,
    ) -> None:
        config = load_config("reliability.yaml")
        self.retry_config = config.get("retry", {})
        self.breaker_config = config.get("circuit_breaker", {})
        self.budget = budget or ExecutionBudget.configured()
        self.loop_detector = loop_detector or LoopDetector(
            int(config.get("loop_detection", {}).get("identical_call_threshold", 3))
        )
        self.health_backend = health_backend
        self.sleep = sleep
        self.random_uniform = random_uniform
        self.attempt_hook = attempt_hook
        self.breakers: dict[str, CircuitBreaker] = {}

    def _breaker(self, provider: str) -> CircuitBreaker:
        if provider not in self.breakers:
            self.breakers[provider] = CircuitBreaker(
                provider=provider,
                failure_threshold=int(self.breaker_config.get("failure_threshold", 3)),
                recovery_timeout_seconds=float(self.breaker_config.get("recovery_timeout_seconds", 60)),
                backend=self.health_backend,
            )
        return self.breakers[provider]

    def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[..., ToolResult],
        provider: str = "local",
        timeout_seconds: float = 30,
        max_attempts: int | None = None,
        fallback: Callable[..., ToolResult] | None = None,
        state: Any = None,
    ) -> ToolResult:
        try:
            self.budget.check(step=True)
        except BudgetExceeded as exc:
            return ToolResult.fail(ErrorCode.BUDGET_EXCEEDED, str(exc))
        breaker = self._breaker(provider)
        if not breaker.allow():
            if fallback:
                return self._run_fallback(tool_name, arguments, fallback, provider, "circuit open")
            return ToolResult.fail(
                ErrorCode.CIRCUIT_OPEN, f"Circuit open for {provider}",
                metadata={"provider": provider, "circuit_state": breaker.state.value},
            )
        attempts = int(max_attempts or self.retry_config.get("max_attempts", 3))
        attempts = min(attempts, self.budget.max_retries_per_step + 1)
        last = ToolResult.fail(ErrorCode.FATAL_ERROR, "tool did not execute")
        for attempt in range(1, attempts + 1):
            try:
                self.budget.check(tool=True)
                started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="geo-tool") as pool:
                    result = pool.submit(operation, **arguments).result(timeout=timeout_seconds)
                if not isinstance(result, ToolResult):
                    result = ToolResult.model_validate(result)
                latency = (time.perf_counter() - started) * 1000
                result.metrics.update({"attempt": attempt, "latency_ms": latency})
            except BaseException as exc:
                result = classify_exception(exc, provider)
                result.metrics["attempt"] = attempt
            if self.attempt_hook:
                self.attempt_hook(
                    {
                        "tool_name": tool_name, "provider": provider, "attempt": attempt,
                        "status": result.status, "error_code": result.error_code.value if result.error_code else None,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
            last = result
            if result.status in {"success", "degraded"}:
                breaker.success()
                try:
                    self.loop_detector.record(tool_name, arguments, result, state)
                except LoopDetected as exc:
                    return ToolResult.fail(ErrorCode.LOOP_DETECTED, str(exc))
                return result
            if result.retryable:
                breaker.failure()
            if not result.retryable or attempt >= attempts:
                break
            delay = min(
                float(self.retry_config.get("max_delay_seconds", 8)),
                float(self.retry_config.get("base_delay_seconds", 0.5)) * (2 ** (attempt - 1)),
            )
            delay += self.random_uniform(0, float(self.retry_config.get("jitter_seconds", 0.25)))
            self.sleep(delay)
        if fallback:
            return self._run_fallback(tool_name, arguments, fallback, provider, last.message)
        return last

    def _run_fallback(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        fallback: Callable[..., ToolResult],
        provider: str,
        reason: str,
    ) -> ToolResult:
        try:
            result = fallback(**arguments)
            if not isinstance(result, ToolResult):
                result = ToolResult.model_validate(result)
        except BaseException as exc:
            return classify_exception(exc, provider)
        if result.status != "failed":
            result.status = "degraded"
            result.degraded = True
            result.original_provider = provider
            result.fallback_provider = str(result.metadata.get("provider", "fallback"))
            result.fallback_reason = reason
        return result
