"""Validate Closed Loop 4A candidate sampling package."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_verification import (
    validate_candidate_sampling_package,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = validate_candidate_sampling_package()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
