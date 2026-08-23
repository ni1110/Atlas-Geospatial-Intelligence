"""Structured JSON logging plus OpenTelemetry spans with local D-drive export."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from renewable_site_agent.config import PATHS


trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        values = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "job_id": getattr(record, "job_id", None),
            "goal_id": getattr(record, "goal_id", None),
            "task_id": getattr(record, "task_id", None),
            "step_id": getattr(record, "step_id", None),
            "agent_id": getattr(record, "agent_id", None),
            "agent_role": getattr(record, "agent_role", None),
            "trace_id": getattr(record, "trace_id", None) or trace_id_var.get() or None,
            "tool": getattr(record, "tool", None),
            "provider": getattr(record, "provider", None),
            "workspace": getattr(record, "workspace", None),
            "attempt": getattr(record, "attempt", None),
            "latency_ms": getattr(record, "latency_ms", None),
            "artifact": getattr(record, "artifact", None),
            "policy_decision": getattr(record, "policy_decision", None),
            "replan_reason": getattr(record, "replan_reason", None),
            "event": getattr(record, "event", record.getMessage()),
            "error_code": getattr(record, "error_code", None),
        }
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def get_logger() -> logging.Logger:
    PATHS.ensure()
    logger = logging.getLogger("renewable_site_agent")
    if not logger.handlers:
        handler = logging.FileHandler(PATHS.logs / "renewable_site.jsonl", encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class Telemetry:
    def __init__(self) -> None:
        PATHS.ensure()
        self.local_path = PATHS.logs / "traces.jsonl"
        self.tracer = None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

            provider = TracerProvider(resource=Resource.create({"service.name": "renewable-site-agent"}))
            if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            elif os.getenv("GEOAGENT_OTEL_CONSOLE") == "1":
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            try:
                trace.set_tracer_provider(provider)
            except Exception:
                pass
            self.tracer = trace.get_tracer("renewable_site_agent")
        except ImportError:
            self.tracer = None

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[str]:
        trace_id = trace_id_var.get() or uuid.uuid4().hex
        token = trace_id_var.set(trace_id)
        started = time.perf_counter()
        status = "success"
        error = None
        otel_context = self.tracer.start_as_current_span(name) if self.tracer else contextlib.nullcontext()
        try:
            with otel_context as span:
                if span is not None and hasattr(span, "set_attribute"):
                    for key, value in attributes.items():
                        if value is not None:
                            span.set_attribute(key, value)
                yield trace_id
        except BaseException as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trace_id": trace_id,
                "span": name,
                "status": status,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "error": error,
                **attributes,
            }
            with self.local_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            trace_id_var.reset(token)


TELEMETRY = Telemetry()
LOGGER = get_logger()
