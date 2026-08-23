"""Create a dynamic Agent plan from a request-scope file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from renewable_site_agent.agent.dynamic_plan_builder import (
    build_dynamic_plan_from_scope_file,
)
from renewable_site_agent.application.create_job import RUNTIME_ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scope_path",
        help="Path to request scope JSON, such as runtime/request_scopes/<request_id>_scope.json.",
    )
    args = parser.parse_args()

    scope_path = Path(args.scope_path)
    output_dir = RUNTIME_ROOT / "request_plans"

    result = build_dynamic_plan_from_scope_file(scope_path, output_dir)

    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
