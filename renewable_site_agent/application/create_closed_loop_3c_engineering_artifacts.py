"""Create Closed Loop 3C engineering enhancement artifacts."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.engineering_enhancement import (
    create_engineering_enhancement_artifacts,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = create_engineering_enhancement_artifacts()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
