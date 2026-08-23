"""FastAPI control plane and local Web Demo for the canonical pipeline."""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from renewable_site_agent.config import PATHS
from renewable_site_agent.durable.store import DurableJobStore, JobStatus
from renewable_site_agent.durable.worker import CanonicalWorker
from renewable_site_agent.service import CanonicalSiteSelectionService

store = DurableJobStore()
service = CanonicalSiteSelectionService(store=store)
stop_worker = threading.Event()


def _worker_loop() -> None:
    worker = CanonicalWorker(store=store)
    while not stop_worker.is_set():
        if worker.run_once() is None:
            stop_worker.wait(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    PATHS.ensure()
    thread = None
    if os.getenv("RENEWABLE_SITE_EMBEDDED_WORKER", "1") == "1":
        stop_worker.clear()
        thread = threading.Thread(target=_worker_loop, name="renewable-site-worker", daemon=True)
        thread.start()
    yield
    stop_worker.set()
    if thread:
        thread.join(timeout=5)


app = FastAPI(
    title="Atlas API",
    version="3.0.0",
    description="Canonical general geospatial multi-agent control plane with renewable compatibility routes",
    lifespan=lifespan,
)

from geoagent.orchestration.api import router as geo_orchestrator_router

app.include_router(geo_orchestrator_router)


class CreateJobRequest(BaseModel):
    query: str = Field(min_length=3)


class ClarificationRequest(BaseModel):
    supplemental_text: str = Field(min_length=1)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "geoagent",
        "canonical_pipeline": True,
        "formal_general_entry": "/v1/geo/jobs",
        "specialized_renewable_entry": "/v1/site-selection/jobs",
        "database": str(PATHS.database),
        "free_disk_gb": PATHS.free_disk_gb(),
    }


@app.post("/v1/site-selection/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(
    body: CreateJobRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    job, reused = service.submit(body.query, idempotency_key=idempotency_key)
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "idempotency_hit": reused,
        "needs_clarification": job["status"] == JobStatus.NEEDS_CLARIFICATION.value,
        "missing_fields": (job.get("route") or {}).get("missing_fields", []),
        "status_url": f"/v1/site-selection/jobs/{job['job_id']}",
    }


@app.get("/v1/site-selection/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        job = store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    job["steps"] = store.steps(job_id)
    job["events"] = store.events(job_id)[-50:]
    job["artifacts"] = store.artifacts(job_id)
    return job


@app.patch("/v1/site-selection/jobs/{job_id}/clarification", status_code=status.HTTP_202_ACCEPTED)
def clarify_job(job_id: str, body: ClarificationRequest) -> dict[str, Any]:
    try:
        job = service.clarify(job_id, body.supplemental_text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id, "status": job["status"]}


@app.get("/v1/site-selection/jobs/{job_id}/result")
def get_result(job_id: str) -> dict[str, Any]:
    try:
        job = store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if not job.get("result"):
        raise HTTPException(status_code=409, detail={"status": job["status"], "message": "Result is not ready"})
    return job["result"]


@app.get("/v1/site-selection/jobs/{job_id}/map-data")
def get_map_data(job_id: str) -> dict[str, Any]:
    artifacts = store.artifacts(job_id)
    item = next((value for value in reversed(artifacts) if value["kind"] == "map_data"), None)
    if item is None:
        raise HTTPException(status_code=409, detail="Map data is not ready")
    path = Path(item["path"])
    if not path.is_file():
        raise HTTPException(status_code=500, detail="Map artifact is missing")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/v1/site-selection/jobs/{job_id}/events")
def get_events(job_id: str) -> list[dict[str, Any]]:
    try:
        store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return store.events(job_id)


WEB_DEMO = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atlas · 新能源选址</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{font:14px system-ui;margin:0;background:#f5f7fa;color:#17202a}.wrap{max-width:1200px;margin:auto;padding:24px}textarea{width:100%;height:88px;padding:12px;box-sizing:border-box}button{padding:10px 18px;background:#146c43;color:white;border:0;border-radius:5px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}.card{background:white;padding:16px;border-radius:8px;box-shadow:0 1px 5px #ccd}#map{height:480px}.badge{display:inline-block;padding:4px 8px;border-radius:12px;background:#e8f4ee}pre{white-space:pre-wrap;max-height:340px;overflow:auto}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style></head>
<body><div class="wrap"><h1>Atlas · 新能源选址</h1><p>异步、可恢复、可追溯的新能源选址 Demo</p>
<textarea id="q">在甘肃省兰州市附近的小范围 AOI 进行 100MW 光伏选址，最大坡度 8 度，排除水体和保护区，Top 5</textarea><br><button onclick="submitJob()">提交 Site Selection Job</button> <span id="status" class="badge">尚未提交</span>
<div class="grid"><div class="card"><h3>步骤 / Retry / Fallback</h3><pre id="events">—</pre></div><div class="card"><h3>Top-K 与证据</h3><pre id="result">—</pre></div></div><div class="card" style="margin-top:16px"><h3>Candidate Map</h3><div id="map"></div></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
let jobId=null,timer=null,map=L.map('map').setView([35,105],4),layer=null;L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors'}).addTo(map);
async function submitJob(){let r=await fetch('/v1/site-selection/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:document.getElementById('q').value})});let j=await r.json();jobId=j.job_id;document.getElementById('status').textContent=j.status;if(timer)clearInterval(timer);timer=setInterval(poll,1500);poll()}
async function poll(){if(!jobId)return;let base='/v1/site-selection/jobs/'+jobId;let j=await (await fetch(base)).json();document.getElementById('status').textContent=j.status+' · '+Math.round((j.progress||0)*100)+'% · '+(j.current_step||'');document.getElementById('events').textContent=JSON.stringify(j.events,null,2);if(['SUCCEEDED','NEEDS_HUMAN_REVIEW'].includes(j.status)){clearInterval(timer);let res=await (await fetch(base+'/result')).json();document.getElementById('result').textContent=JSON.stringify(res.top_k,null,2);let geo=await (await fetch(base+'/map-data')).json();if(layer)map.removeLayer(layer);layer=L.geoJSON(geo,{onEachFeature:(f,l)=>l.bindPopup('<b>'+f.properties.candidate_id+'</b><br>score '+f.properties.overall_score+'<br>area '+f.properties.area_km2.toFixed(2)+' km²<br>evidence '+f.properties.evidence_completeness_score+'%')}).addTo(map);map.fitBounds(layer.getBounds())}}
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def web_demo() -> str:
    return WEB_DEMO


def main() -> None:
    import uvicorn

    uvicorn.run("renewable_site_agent.api:app", host="127.0.0.1", port=int(os.getenv("RENEWABLE_SITE_PORT", "8000")), reload=False)


if __name__ == "__main__":
    main()
