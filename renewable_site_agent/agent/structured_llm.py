"""Strands structured-output adapter used by routing, parsing, and planning."""

from __future__ import annotations

import os
import time
from typing import ClassVar, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StrandsStructuredLLM:
    """Thin adapter around Strands' validated Pydantic output API."""

    last_environment_error: ClassVar[str | None] = None

    def __init__(
        self,
        agent: object,
        *,
        provider: str = "unknown",
        model_id: str | None = None,
        max_attempts: int | None = None,
        retry_base_seconds: float | None = None,
        retry_max_seconds: float | None = None,
        min_call_interval_seconds: float | None = None,
    ) -> None:
        self.agent = agent
        self.provider = provider
        self.model_id = model_id
        self.call_count = 0
        self.max_attempts = max(1, int(max_attempts or os.getenv("GEOAGENT_MODEL_RETRY_ATTEMPTS", "3")))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds if retry_base_seconds is not None else os.getenv("GEOAGENT_MODEL_RETRY_BASE_SECONDS", "5")))
        self.retry_max_seconds = max(self.retry_base_seconds, float(retry_max_seconds if retry_max_seconds is not None else os.getenv("GEOAGENT_MODEL_RETRY_MAX_SECONDS", "30")))
        default_interval = "3" if provider == "zhipu" else "0"
        self.min_call_interval_seconds = max(0.0, float(min_call_interval_seconds if min_call_interval_seconds is not None else os.getenv("GEOAGENT_MODEL_MIN_CALL_INTERVAL_SECONDS", default_interval)))
        self._last_call_started_at: float | None = None

    @classmethod
    def from_environment(cls) -> "StrandsStructuredLLM | None":
        cls.last_environment_error = None
        configured = any(
            os.getenv(name)
            for name in (
                "GEOAGENT_MODEL_PROVIDER",
                "GEOAGENT_MODEL_API_KEY",
                "ZHIPUAI_API_KEY",
                "ZAI_API_KEY",
                "OPENAI_API_KEY",
                "OPENAI_CODEX_ACCESS_TOKEN",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "OPENROUTER_API_KEY",
                "VLLM_BASE_URL",
                "OLLAMA_HOST",
            )
        )
        if not configured:
            cls.last_environment_error = "LLM_CONFIGURATION_MISSING: no supported model provider or credential environment variable is present"
            return None
        provider = os.getenv("GEOAGENT_MODEL_PROVIDER")
        model_id = os.getenv("GEOAGENT_MODEL_ID")
        if (provider or "").lower() == "zhipu" and not any(
            os.getenv(name) for name in ("ZHIPUAI_API_KEY", "ZAI_API_KEY", "GEOAGENT_MODEL_API_KEY")
        ):
            cls.last_environment_error = (
                "ZHIPU_CREDENTIAL_MISSING: provider=zhipu, "
                f"model={model_id or 'glm-4.5-flash'}, key_present=false"
            )
            return None
        try:
            from strands import Agent

            from geoagent.core.config import GeoAgentConfig
            from geoagent.core.model import resolve_model

            if (provider or "").lower() == "zhipu" and not model_id:
                model_id = os.getenv("ZHIPUAI_MODEL", "glm-4.5-flash")
            values: dict[str, object] = {"temperature": 0.0, "max_tokens": 2048}
            if provider:
                values["provider"] = provider
            if model_id:
                values["model"] = model_id
            base_url = os.getenv("GEOAGENT_MODEL_BASE_URL")
            client_args: dict[str, object] = {
                "timeout": float(os.getenv("GEOAGENT_MODEL_TIMEOUT_SECONDS", "60")),
                "max_retries": 0,
            }
            if base_url:
                client_args["base_url"] = base_url
            values["client_args"] = client_args
            resolved_config = GeoAgentConfig.model_validate(values)
            model = resolve_model(resolved_config)
            selected_provider = str(provider or resolved_config.provider)
            return cls(Agent(model=model, callback_handler=None), provider=selected_provider, model_id=str(model_id) if model_id else None)
        except Exception as exc:
            # Do not include the exception message: third-party client errors
            # may echo request configuration.  The type is sufficient for a
            # safe, explainable deterministic fallback reason.
            cls.last_environment_error = f"LLM_INITIALIZATION_FAILED: {type(exc).__name__}"
            return None

    def generate(self, prompt: str, output_model: type[T]) -> T:
        for attempt in range(1, self.max_attempts + 1):
            self._pace_calls()
            self.call_count += 1
            try:
                result = self.agent(prompt, structured_output_model=output_model)  # type: ignore[operator]
                structured = getattr(result, "structured_output", None)
                if structured is None:
                    raise ValueError("Strands result did not contain structured_output")
                return output_model.model_validate(structured)
            except Exception as exc:
                if attempt >= self.max_attempts or not self._is_retryable(exc):
                    raise
                time.sleep(self._retry_delay(exc, attempt))
        raise RuntimeError("Structured LLM retry loop exhausted")  # pragma: no cover

    def _pace_calls(self) -> None:
        now = time.monotonic()
        if self._last_call_started_at is not None:
            remaining = self.min_call_interval_seconds - (now - self._last_call_started_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call_started_at = time.monotonic()

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        status = getattr(exc, "status_code", None)
        return bool(
            status == 429
            or (isinstance(status, int) and status >= 500)
            or any(token in name for token in ("ratelimit", "timeout", "connection", "serviceunavailable"))
            or any(token in message for token in ("rate limit", "too many requests", "timed out", "timeout", "temporarily unavailable"))
        )

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        try:
            retry_after = float(headers.get("retry-after", 0))
        except (TypeError, ValueError):
            retry_after = 0.0
        exponential = self.retry_base_seconds * (2 ** (attempt - 1))
        return min(self.retry_max_seconds, max(retry_after, exponential))
