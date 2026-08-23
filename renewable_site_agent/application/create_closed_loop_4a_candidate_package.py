"""Create Closed Loop 4A candidate sampling and verification package."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_verification import (
    create_candidate_sampling_package,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k-per-case", type=int, default=3)
    args = parser.parse_args()

    result = create_candidate_sampling_package(top_k_per_case=args.top_k_per_case)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
