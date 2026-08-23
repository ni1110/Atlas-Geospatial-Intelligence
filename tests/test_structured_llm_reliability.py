from __future__ import annotations

from pydantic import BaseModel

from renewable_site_agent.agent.structured_llm import StrandsStructuredLLM


class Payload(BaseModel):
    value: str


class Result:
    structured_output = {"value": "ok"}


def test_structured_llm_retries_rate_limit(monkeypatch) -> None:
    calls = {"count": 0}
    delays: list[float] = []

    class RateLimitError(Exception):
        status_code = 429

    def agent(_prompt: str, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RateLimitError("rate limit")
        return Result()

    monkeypatch.setattr("renewable_site_agent.agent.structured_llm.time.sleep", delays.append)
    llm = StrandsStructuredLLM(
        agent,
        provider="zhipu",
        max_attempts=2,
        retry_base_seconds=2,
        retry_max_seconds=10,
        min_call_interval_seconds=0,
    )

    assert llm.generate("prompt", Payload).value == "ok"
    assert llm.call_count == 2
    assert delays == [2]


def test_structured_llm_does_not_retry_schema_error(monkeypatch) -> None:
    calls = {"count": 0}

    def agent(_prompt: str, **_kwargs):
        calls["count"] += 1
        raise ValueError("invalid structured output")

    monkeypatch.setattr("renewable_site_agent.agent.structured_llm.time.sleep", lambda _delay: None)
    llm = StrandsStructuredLLM(agent, max_attempts=3, min_call_interval_seconds=0)

    try:
        llm.generate("prompt", Payload)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("schema error must surface to the caller")
    assert calls["count"] == 1
