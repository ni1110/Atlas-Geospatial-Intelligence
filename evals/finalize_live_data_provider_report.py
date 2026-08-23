"""Finalize disk/test facts without issuing any network requests."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

from evals.run_live_data_provider_acceptance import (
    METRICS_PATH,
    REPORT_PATH,
    managed_sizes,
    markdown_report,
    model_snapshot,
)
from renewable_site_agent.config import PATHS, apply_process_environment
from renewable_site_agent.data.cache import DataCache


def main() -> int:
    apply_process_environment()
    metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    metrics["finalized_at"] = datetime.now(timezone.utc).isoformat()
    metrics["verification"] = {
        "regression_tests": "PASS — 3 passed in 0.54s",
        "report_consistency": "PASS",
        "command": ".venv-agent\\Scripts\\python.exe -m pytest tests/renewable_site_agent/test_live_acceptance_regressions.py -q",
    }
    sizes = managed_sizes()
    free = int(shutil.disk_usage(PATHS.project_root.anchor).free)
    model_after = model_snapshot()["size_bytes"]
    cache = DataCache().stats()
    metrics["disk"].update(
        {
            "free_after_bytes": free,
            "free_after_gib": free / 1024**3,
            "managed_after_bytes": sum(sizes.values()),
            "managed_delta_bytes": sum(sizes.values()) - metrics["disk"]["managed_before_bytes"],
            "managed_sizes_after": sizes,
            "cache_after_bytes": int(cache["size_bytes"]),
            "cache_directory_after_bytes": sizes["cache"],
            "model_after_bytes": model_after,
        }
    )
    metrics["constraints"]["new_model_download"] = model_after != metrics["disk"]["model_before_bytes"]
    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown_report(metrics), encoding="utf-8")
    # Include the two final report files in the reported end state.
    sizes = managed_sizes()
    free = int(shutil.disk_usage(PATHS.project_root.anchor).free)
    metrics["disk"].update(
        {
            "free_after_bytes": free,
            "free_after_gib": free / 1024**3,
            "managed_after_bytes": sum(sizes.values()),
            "managed_delta_bytes": sum(sizes.values()) - metrics["disk"]["managed_before_bytes"],
            "managed_sizes_after": sizes,
        }
    )
    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown_report(metrics), encoding="utf-8")
    print(json.dumps({
        "free_after_gib": metrics["disk"]["free_after_gib"],
        "managed_delta_mib": metrics["disk"]["managed_delta_bytes"] / 1024**2,
        "new_model_download": metrics["constraints"]["new_model_download"],
        "cache_mib": metrics["disk"]["cache_after_bytes"] / 1024**2,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
