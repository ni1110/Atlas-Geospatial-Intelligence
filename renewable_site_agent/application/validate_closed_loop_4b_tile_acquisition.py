"""Validate Closed Loop 4B tile acquisition manifest."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_tile_acquisition import (
    validate_tile_acquisition_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail validation when any tile is missing.",
    )
    args = parser.parse_args()

    result = validate_tile_acquisition_manifest(allow_missing=not args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
