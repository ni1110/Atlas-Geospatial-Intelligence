"""Validate Closed Loop 4C-2 VLM remote-sensing verification."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_vlm_verification import (
    validate_vlm_verification,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = validate_vlm_verification()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
