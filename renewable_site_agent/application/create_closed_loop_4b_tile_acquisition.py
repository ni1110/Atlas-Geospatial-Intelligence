"""Create Closed Loop 4B tile acquisition manifest."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.remote_sensing_tile_acquisition import (
    create_tile_acquisition_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-tile-dir", default=None)
    parser.add_argument("--provider-name", default="local_file")
    args = parser.parse_args()

    result = create_tile_acquisition_manifest(
        local_tile_dir=args.local_tile_dir,
        provider_name=args.provider_name,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
