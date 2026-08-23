"""Export tile input template for Closed Loop 4B.

This tells the user exactly which real remote-sensing image tiles are required.
It does not create fake imagery.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from renewable_site_agent.application.create_job import RUNTIME_ROOT


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    manifest_path = (
        RUNTIME_ROOT
        / "remote_sensing"
        / "closed_loop_4a_tile_request_manifest_v1.json"
    )
    manifest = read_json(manifest_path)

    output_dir = RUNTIME_ROOT / "remote_sensing" / "tile_inputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "tile_input_template.csv"
    json_path = output_dir / "tile_input_template.json"

    rows = []

    for task in manifest.get("tile_tasks", []):
        sample_id = task["sample_id"]
        centroid = task.get("centroid_wgs84", {})
        bbox = task.get("bbox_wgs84", {})

        row = {
            "sample_id": sample_id,
            "expected_filename_png": f"{sample_id}.png",
            "expected_filename_jpg": f"{sample_id}.jpg",
            "expected_filename_tif": f"{sample_id}.tif",
            "project_type": task.get("project_type"),
            "scope_kind": task.get("scope_kind"),
            "center_lon": centroid.get("lon"),
            "center_lat": centroid.get("lat"),
            "min_lon": bbox.get("min_lon"),
            "min_lat": bbox.get("min_lat"),
            "max_lon": bbox.get("max_lon"),
            "max_lat": bbox.get("max_lat"),
            "suggested_zoom_levels": ",".join(map(str, task.get("suggested_zoom_levels", []))),
            "put_file_under": str(output_dir),
        }
        rows.append(row)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    json_path.write_text(
        json.dumps(
            {
                "tile_input_template_status": "created",
                "do_not_fake_imagery": True,
                "instruction_zh": (
                    "请将真实遥感影像切片放入本目录。文件名建议使用 sample_id.png、"
                    "sample_id.jpg 或 sample_id.tif。系统会自动登记这些真实图像。"
                ),
                "tile_input_dir": str(output_dir),
                "tile_count": len(rows),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "tile_input_template_status": "created",
        "tile_input_dir": str(output_dir),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "tile_count": len(rows),
        "next_step_zh": "把真实遥感图片按 sample_id 命名后放入 tile_input_dir，再重新运行 4B acquisition。",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
