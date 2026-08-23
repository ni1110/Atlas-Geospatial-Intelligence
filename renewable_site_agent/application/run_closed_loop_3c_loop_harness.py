"""Run Closed Loop 3C Loop Engineering harness."""

from __future__ import annotations

import argparse
import json

from renewable_site_agent.agent.loop_engineering import run_loop_harness


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    result = run_loop_harness()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
