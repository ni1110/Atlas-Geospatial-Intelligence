"""Validate Closed Loop 4D remote-sensing evidence fusion."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_evidence_fusion import (
    validate_remote_sensing_evidence_fusion,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = validate_remote_sensing_evidence_fusion()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
