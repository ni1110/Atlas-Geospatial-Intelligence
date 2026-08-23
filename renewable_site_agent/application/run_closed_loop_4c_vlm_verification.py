"""Run Closed Loop 4C-2 VLM remote-sensing verification."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_vlm_verification import (
    run_vlm_verification,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()

    result = run_vlm_verification(limit=args.limit, timeout_sec=args.timeout_sec)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
