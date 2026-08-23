"""Generate an available-data screening report for the site-selection job."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyogrio import set_gdal_config_options

from renewable_site_agent.application.create_job import PROJECT_ROOT, RUNTIME_ROOT
from renewable_site_agent.domain.jobs import JobStatus
from renewable_site_agent.infrastructure.job_store import SQLiteJobStore


class ReportGenerationError(RuntimeError):
    """Raised when report generation fails."""


@dataclass(frozen=True)
class ReportResult:
    job_id: str
    report_path: Path
    metadata_path: Path
    report_sha256: str
    candidate_count: int
    total_area_km2: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def relative_to_project(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def resolve_project_path(path_text: str) -> Path:
    path = Path(path_text)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReportGenerationError(f"Missing JSON file: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(".tmp.html")
    temporary_path.write_text(text, encoding="utf-8")
    temporary_path.replace(path)


def esc(value: Any) -> str:
    return html.escape(str(value))


def fmt_float(value: Any, digits: int = 6) -> str:
    return f"{float(value):,.{digits}f}"


def make_rows(rows: list[list[Any]]) -> str:
    rendered = []

    for row in rows:
        cells = "".join(f"<td>{esc(cell)}</td>" for cell in row)
        rendered.append(f"<tr>{cells}</tr>")

    return "\n".join(rendered)


def make_header(columns: list[str]) -> str:
    return "".join(f"<th>{esc(column)}</th>" for column in columns)


def request_to_dict(job: Any) -> dict[str, Any]:
    request = job.request

    if hasattr(request, "model_dump"):
        return request.model_dump(mode="json")

    if isinstance(request, dict):
        return request

    return dict(request)


def generate_available_data_report(job_id: str) -> ReportResult:
    database_path = RUNTIME_ROOT / "site_jobs.db"
    job_store = SQLiteJobStore(database_path)
    job = job_store.get(job_id)

    if job.status != JobStatus.CANDIDATES_READY:
        raise ReportGenerationError(
            "Report generation requires candidates_ready; "
            f"received {job.status.value}."
        )

    job_dir = RUNTIME_ROOT / "jobs" / job_id
    terrain_dir = job_dir / "terrain"
    protected_dir = job_dir / "protected_areas"
    final_dir = job_dir / "final"

    decision_log_path = final_dir / "screening_decision_log.json"
    slope_screening_metadata_path = terrain_dir / "slope_screening_metadata.json"
    slope_metadata_path = terrain_dir / "slope_metadata.json"
    protected_area_metadata_path = protected_dir / "protected_areas_metadata.json"
    protected_screening_metadata_path = (
        protected_dir / "protected_area_screening_metadata.json"
    )

    decision_log = read_json(decision_log_path)
    slope_screening = read_json(slope_screening_metadata_path)
    slope_metadata = read_json(slope_metadata_path)
    protected_area_metadata = read_json(protected_area_metadata_path)
    protected_screening = read_json(protected_screening_metadata_path)

    final_candidate_path = resolve_project_path(
        decision_log["latest_candidate_output"]["path"]
    )
    expected_candidate_sha256 = decision_log["latest_candidate_output"]["sha256"]

    if not final_candidate_path.is_file():
        raise ReportGenerationError(
            f"Missing final candidate file: {final_candidate_path}"
        )

    if sha256_file(final_candidate_path) != expected_candidate_sha256:
        raise ReportGenerationError(
            "Final candidate checksum does not match decision log."
        )

    set_gdal_config_options({"OGR_GEOJSON_MAX_OBJ_SIZE": "0"})
    candidates = gpd.read_file(final_candidate_path)

    if candidates.empty:
        raise ReportGenerationError("Final candidate file is empty.")

    candidate_count = int(len(candidates))
    total_area_km2 = float(candidates["area_km2"].sum())

    top_candidates = (
        candidates.sort_values("area_km2", ascending=False)
        .head(20)
        .copy()
    )

    request = request_to_dict(job)

    completed_constraints = decision_log.get("completed_constraints", [])
    skipped_constraints = decision_log.get("skipped_constraints", [])

    completed_rows = [
        [
            item.get("constraint", ""),
            item.get("status", ""),
            item.get("threshold", ""),
            item.get("description", ""),
        ]
        for item in completed_constraints
    ]

    skipped_rows = [
        [
            item.get("constraint", ""),
            item.get("status", ""),
            item.get("requested_threshold_km", item.get("requested_setting", "")),
            item.get("reason", ""),
            item.get("impact", ""),
        ]
        for item in skipped_constraints
    ]

    stage_rows = [
        [
            "Initial non-water candidates",
            slope_screening["input_candidates"]["feature_count"],
            fmt_float(slope_screening["input_candidates"]["total_area_km2"]),
            "Water bodies excluded; slope not yet applied.",
        ],
        [
            "After slope screening",
            slope_screening["output"]["feature_count"],
            fmt_float(slope_screening["output"]["total_area_km2"]),
            "Candidates intersected with slope < 8° area.",
        ],
        [
            "After protected-area exclusion",
            protected_screening["output"]["feature_count"],
            fmt_float(protected_screening["output"]["total_area_km2"]),
            "OSM protected-area exclusion applied.",
        ],
        [
            "Available-data final candidate set",
            candidate_count,
            fmt_float(total_area_km2),
            "Substation-distance constraint skipped and recorded.",
        ],
    ]

    top_rows = []

    for _, row in top_candidates.iterrows():
        top_rows.append(
            [
                int(row.get("rank", 0)),
                row.get("candidate_id", ""),
                fmt_float(row.get("area_km2", 0.0)),
                fmt_float(row.get("centroid_lon", 0.0), 6),
                fmt_float(row.get("centroid_lat", 0.0), 6),
                row.get("screening_status", ""),
            ]
        )

    evidence_rows = [
        [
            "Final candidate GeoJSON",
            relative_to_project(final_candidate_path),
            expected_candidate_sha256,
        ],
        [
            "Decision log",
            relative_to_project(decision_log_path),
            sha256_file(decision_log_path),
        ],
        [
            "Slope screening metadata",
            relative_to_project(slope_screening_metadata_path),
            sha256_file(slope_screening_metadata_path),
        ],
        [
            "Slope raster metadata",
            relative_to_project(slope_metadata_path),
            sha256_file(slope_metadata_path),
        ],
        [
            "Protected-area preparation metadata",
            relative_to_project(protected_area_metadata_path),
            sha256_file(protected_area_metadata_path),
        ],
        [
            "Protected-area screening metadata",
            relative_to_project(protected_screening_metadata_path),
            sha256_file(protected_screening_metadata_path),
        ],
    ]

    report_created_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )

    html_report = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Available-Data Renewable Site Screening Report</title>
  <style>
    body {{
      font-family: Arial, Helvetica, sans-serif;
      margin: 32px;
      line-height: 1.5;
      color: #1f2933;
      background: #ffffff;
    }}
    h1, h2, h3 {{
      color: #102a43;
    }}
    .meta {{
      color: #52606d;
      font-size: 14px;
    }}
    .warning {{
      border-left: 6px solid #c2410c;
      background: #fff7ed;
      padding: 14px 18px;
      margin: 18px 0;
    }}
    .success {{
      border-left: 6px solid #15803d;
      background: #f0fdf4;
      padding: 14px 18px;
      margin: 18px 0;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      margin: 14px 0 28px 0;
      font-size: 14px;
    }}
    th {{
      background: #e5e7eb;
      text-align: left;
    }}
    th, td {{
      border: 1px solid #cbd5e1;
      padding: 8px;
      vertical-align: top;
    }}
    code {{
      background: #f1f5f9;
      padding: 2px 4px;
      border-radius: 4px;
    }}
  </style>
</head>
<body>
  <h1>Available-Data Renewable Site Screening Report</h1>
  <p class="meta">Job ID: <code>{esc(job_id)}</code></p>
  <p class="meta">Generated at UTC: {esc(report_created_at)}</p>

  <div class="warning">
    <strong>Important status:</strong>
    This report is an available-data preliminary screening result.
    It must not be described as a final recommended site list because the
    requested 10 km substation-distance constraint was skipped and not evaluated.
  </div>

  <h2>1. User Request</h2>
  <table>
    <thead>
      <tr>{make_header(["Field", "Value"])}</tr>
    </thead>
    <tbody>
      {make_rows([[key, value] for key, value in request.items()])}
    </tbody>
  </table>

  <h2>2. Final Available-Data Result</h2>
  <div class="success">
    <p><strong>Candidate count:</strong> {esc(candidate_count)}</p>
    <p><strong>Total area:</strong> {esc(fmt_float(total_area_km2))} km²</p>
    <p><strong>Status:</strong> {esc(decision_log["result_status"])}</p>
  </div>

  <h2>3. Screening Progress Summary</h2>
  <table>
    <thead>
      <tr>{make_header(["Stage", "Candidate Count", "Area km²", "Note"])}</tr>
    </thead>
    <tbody>
      {make_rows(stage_rows)}
    </tbody>
  </table>

  <h2>4. Completed Constraints</h2>
  <table>
    <thead>
      <tr>{make_header(["Constraint", "Status", "Threshold", "Description"])}</tr>
    </thead>
    <tbody>
      {make_rows(completed_rows)}
    </tbody>
  </table>

  <h2>5. Skipped Constraints</h2>
  <table>
    <thead>
      <tr>{make_header(["Constraint", "Status", "Requested Threshold / Setting", "Reason", "Impact"])}</tr>
    </thead>
    <tbody>
      {make_rows(skipped_rows)}
    </tbody>
  </table>

  <h2>6. Key Screening Statistics</h2>
  <table>
    <thead>
      <tr>{make_header(["Metric", "Value"])}</tr>
    </thead>
    <tbody>
      {make_rows([
        ["Slope threshold", str(slope_metadata["method"]["max_slope_deg"]) + " degrees"],
        ["Slope suitable area", fmt_float(slope_metadata["statistics"]["suitable_area_km2"]) + " km²"],
        ["Area after slope screening", fmt_float(slope_screening["output"]["total_area_km2"]) + " km²"],
        ["Area removed by slope screening", fmt_float(slope_screening["output"]["removed_by_slope_area_km2"]) + " km²"],
        ["Protected-area exclusion area", fmt_float(protected_area_metadata["output"]["union_area_km2"]) + " km²"],
        ["Area removed by protected-area screening", fmt_float(protected_screening["output"]["removed_by_protected_area_km2"]) + " km²"],
      ])}
    </tbody>
  </table>

  <h2>7. Top 20 Largest Remaining Candidate Areas</h2>
  <table>
    <thead>
      <tr>{make_header(["Rank", "Candidate ID", "Area km²", "Centroid Lon", "Centroid Lat", "Status"])}</tr>
    </thead>
    <tbody>
      {make_rows(top_rows)}
    </tbody>
  </table>

  <h2>8. Evidence and Reproducibility</h2>
  <table>
    <thead>
      <tr>{make_header(["Artifact", "Path", "SHA-256"])}</tr>
    </thead>
    <tbody>
      {make_rows(evidence_rows)}
    </tbody>
  </table>

  <h2>9. Interpretation</h2>
  <p>
    The current candidate set satisfies the constraints that could be evaluated
    from available local data: Beijing boundary, water exclusion, slope below 8 degrees,
    and OSM protected-area exclusion.
  </p>
  <p>
    The substation-distance constraint was explicitly skipped because the local Beijing
    GeoPackage did not contain reliable <code>power=substation</code> or equivalent
    substation geometry data.
  </p>
  <p>
    The prime-farmland / land-use exclusion constraint was also recorded as skipped
    because the current local data workflow does not include a reliable prime farmland
    or land-use restriction layer. Therefore, the current output should be used as a
    preliminary available-data candidate set only.
  </p>
</body>
</html>
"""

    report_path = final_dir / "available_data_screening_report.html"
    metadata_path = final_dir / "available_data_screening_report.metadata.json"

    write_text_atomic(report_path, html_report)

    metadata = {
        "job_id": job_id,
        "stage": "available_data_report_generation",
        "created_at_utc": report_created_at,
        "report": {
            "path": relative_to_project(report_path),
            "sha256": sha256_file(report_path),
            "size_bytes": report_path.stat().st_size,
        },
        "candidate_summary": {
            "candidate_count": candidate_count,
            "total_area_km2": total_area_km2,
            "status": decision_log["result_status"],
        },
        "inputs": {
            "decision_log_path": relative_to_project(decision_log_path),
            "final_candidate_path": relative_to_project(final_candidate_path),
            "final_candidate_sha256": expected_candidate_sha256,
        },
    }

    write_json_atomic(metadata_path, metadata)

    return ReportResult(
        job_id=job_id,
        report_path=report_path,
        metadata_path=metadata_path,
        report_sha256=metadata["report"]["sha256"],
        candidate_count=candidate_count,
        total_area_km2=total_area_km2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()

    result = generate_available_data_report(args.job_id)

    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "report_path": str(result.report_path),
                "metadata_path": str(result.metadata_path),
                "report_sha256": result.report_sha256,
                "candidate_count": result.candidate_count,
                "total_area_km2": round(result.total_area_km2, 6),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
