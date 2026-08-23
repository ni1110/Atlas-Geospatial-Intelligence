"""Canonical submission service shared by API, CLI, UI, and tests."""

from __future__ import annotations

from pydantic import ValidationError

from renewable_site_agent.agent.planning import Planner
from renewable_site_agent.agent.routing import IntentRouter
from renewable_site_agent.durable.store import DurableJobStore, JobStatus
from renewable_site_agent.observability import TELEMETRY
from renewable_site_agent.parser import parse_site_selection_request


class CanonicalSiteSelectionService:
    def __init__(
        self,
        store: DurableJobStore | None = None,
        router: IntentRouter | None = None,
        planner: Planner | None = None,
    ) -> None:
        self.store = store or DurableJobStore()
        self.router = router or IntentRouter()
        self.planner = planner or Planner()

    def submit(
        self, request_text: str, *, idempotency_key: str | None = None
    ) -> tuple[dict, bool]:
        with TELEMETRY.span("intent.route"):
            decision = self.router.route(request_text)
        if decision.intent != "renewable_site_selection" or decision.needs_clarification:
            return self.store.create_job(
                request_text=request_text,
                route=decision.model_dump(mode="json"),
                request=None,
                plan=None,
                status=JobStatus.NEEDS_CLARIFICATION,
                idempotency_key=idempotency_key,
            )
        try:
            with TELEMETRY.span("request.parse"):
                structured = parse_site_selection_request(request_text)
        except (ValidationError, ValueError) as exc:
            decision.needs_clarification = True
            decision.missing_fields = sorted(
                set(decision.missing_fields + ["invalid_or_missing_constraints"])
            )
            decision.reason = str(exc)
            return self.store.create_job(
                request_text=request_text,
                route=decision.model_dump(mode="json"),
                request=None,
                plan=None,
                status=JobStatus.NEEDS_CLARIFICATION,
                idempotency_key=idempotency_key,
            )
        with TELEMETRY.span("skill.select", selected_skill=decision.selected_skill):
            selected_skill = decision.selected_skill
        with TELEMETRY.span("planner.generate", selected_skill=selected_skill):
            plan = self.planner.create(structured, selected_skill)
        return self.store.create_job(
            request_text=request_text,
            route=decision.model_dump(mode="json"),
            request=structured.model_dump(mode="json"),
            plan=plan.model_dump(mode="json"),
            status=JobStatus.PENDING,
            idempotency_key=idempotency_key,
        )

    def clarify(self, job_id: str, supplemental_text: str) -> dict:
        current = self.store.get_job(job_id)
        combined = f"{current['request_text']}\n补充信息：{supplemental_text}"
        decision = self.router.route(combined)
        if decision.needs_clarification or decision.intent != "renewable_site_selection":
            raise ValueError(
                f"Clarification is still incomplete: {decision.missing_fields}"
            )
        structured = parse_site_selection_request(combined)
        plan = self.planner.create(structured, decision.selected_skill)
        return self.store.clarify_job(
            job_id,
            request_text=combined,
            route=decision.model_dump(mode="json"),
            request=structured.model_dump(mode="json"),
            plan=plan.model_dump(mode="json"),
        )
