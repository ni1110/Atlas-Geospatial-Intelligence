"""Resolve a user natural-language request into a request-driven Agent scope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from renewable_site_agent.agent.request_scope import parse_and_resolve
from renewable_site_agent.application.create_job import RUNTIME_ROOT


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="+", help="User natural-language request.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write resolved request scope to runtime/request_scopes/.",
    )
    args = parser.parse_args()

    raw_text = " ".join(args.text)
    result = parse_and_resolve(raw_text)

    if args.write:
        output_path = (
            RUNTIME_ROOT
            / "request_scopes"
            / f"{result['request_id']}_scope.json"
        )
        write_json(output_path, result)
        result["scope_output_path"] = str(output_path)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
