"""Validate product-scope decision for the current renewable-site Agent version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from renewable_site_agent.application.create_job import RUNTIME_ROOT


DEFAULT_JOB_ID = "ce5f4714-b004-435f-9876-49c99e8966ff"


class ScopeDecisionValidationError(RuntimeError):
    """Raised when scope decision is invalid."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ScopeDecisionValidationError(f"Missing scope decision file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_scope_decision(job_id: str) -> dict[str, Any]:
    scope_path = (
        RUNTIME_ROOT
        / "jobs"
        / job_id
        / "agent"
        / "scope_decision_v1.json"
    )

    scope = read_json(scope_path)

    in_scope = scope.get("in_scope_constraints", [])
    out_of_scope = scope.get("out_of_scope_constraints", [])

    if scope.get("product_scope_status") != "current_version_scope_finalized":
        raise ScopeDecisionValidationError(
            f"Unexpected product_scope_status: {scope.get('product_scope_status')}"
        )

    if scope.get("blocked_constraint_count") != 0:
        raise ScopeDecisionValidationError(
            f"blocked_constraint_count must be 0 for current scoped release: {scope.get('blocked_constraint_count')}"
        )

    if scope.get("current_version_recommendation_allowed") is not True:
        raise ScopeDecisionValidationError(
            "current_version_recommendation_allowed must be true after scope is finalized."
        )

    if scope.get("in_scope_constraint_count") != len(in_scope):
        raise ScopeDecisionValidationError(
            "in_scope_constraint_count does not match in_scope_constraints length."
        )

    if scope.get("out_of_scope_constraint_count") != len(out_of_scope):
        raise ScopeDecisionValidationError(
            "out_of_scope_constraint_count does not match out_of_scope_constraints length."
        )

    if len(out_of_scope) != 2:
        raise ScopeDecisionValidationError(
            f"Expected 2 out-of-scope constraints, got {len(out_of_scope)}."
        )

    invalid_out_of_scope = []
    for item in out_of_scope:
        if item.get("status") != "out_of_scope_by_product_decision":
            invalid_out_of_scope.append(item)
        if item.get("must_not_claim_evaluated") is not True:
            invalid_out_of_scope.append(item)

    if invalid_out_of_scope:
        raise ScopeDecisionValidationError(
            json.dumps(invalid_out_of_scope, ensure_ascii=False, indent=2)
        )

    return {
        "job_id": job_id,
        "scope_decision_validation_status": "passed",
        "scope_decision_path": str(scope_path),
        "product_scope_status": scope.get("product_scope_status"),
        "product_scope_status_zh": scope.get("product_scope_status_zh"),
        "current_version_recommendation_allowed": scope.get(
            "current_version_recommendation_allowed"
        ),
        "current_version_recommendation_label": scope.get(
            "current_version_recommendation_label"
        ),
        "in_scope_constraint_count": len(in_scope),
        "out_of_scope_constraint_count": len(out_of_scope),
        "blocked_constraint_count": scope.get("blocked_constraint_count"),
        "candidate_count": scope.get("candidate_count"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", default=DEFAULT_JOB_ID)
    args = parser.parse_args()

    result = validate_scope_decision(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
