"""Formal FastAPI entry and execution-debug panel for GeoAgent."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from geoagent.orchestration.formal_runtime import (
    build_formal_handlers,
    formal_provider_status,
)
from geoagent.orchestration.models import GoalContract
from geoagent.orchestration.orchestrator import GeoOrchestratorAgent
from geoagent.orchestration.state import MultiAgentStateStore

router = APIRouter(tags=["GeoAgent General Orchestration"])


class InputArtifactRequest(BaseModel):
    path: str
    kind: str | None = None
    description: str | None = None


class GeoJobRequest(BaseModel):
    user_query: str = Field(min_length=3)
    input_artifacts: list[InputArtifactRequest] = Field(default_factory=list)
    region_or_aoi: dict[str, Any] | str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    use_live_llm: bool = True
    resume: bool = True


class GeoClarificationRequest(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)
    supplemental_text: str | None = None
    use_live_llm: bool = True


class OrchestrateRequest(BaseModel):
    query: str
    execute: bool = False
    use_live_llm: bool = True


@router.post("/v1/geo/jobs", status_code=status.HTTP_201_CREATED)
def create_geo_job(body: GeoJobRequest) -> dict[str, Any]:
    """Run one request through Orchestrator -> Planner -> Harness -> Review."""
    orchestrator = GeoOrchestratorAgent()
    artifact_values = [item.model_dump(mode="json") for item in body.input_artifacts]
    try:
        result = orchestrator.execute(
            body.user_query,
            use_llm=body.use_live_llm,
            handlers=build_formal_handlers(input_artifacts=artifact_values, constraints=body.constraints, region_or_aoi=body.region_or_aoi),
            resume=body.resume,
            existing_artifacts=artifact_values,
            provider_status=formal_provider_status(),
            region_or_aoi=body.region_or_aoi,
            constraints=body.constraints,
        )
        orchestrator.state_store.save_result(result)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"error_type": type(exc).__name__, "message": str(exc)}) from exc
    return {
        "job_id": result.job_id,
        "status": result.status,
        "route_mode": result.metrics.get("route_mode"),
        "plan_mode": result.metrics.get("plan_mode"),
        "status_url": f"/v1/geo/jobs/{result.job_id}",
        "result_url": f"/v1/geo/jobs/{result.job_id}/result",
    }


@router.get("/v1/geo/jobs/{job_id}")
def get_geo_job(job_id: str) -> dict[str, Any]:
    try:
        snapshot = MultiAgentStateStore().execution_snapshot(job_id)
        clarification = _clarification_payload(snapshot)
        snapshot["clarification"] = clarification
        snapshot["clarification_required"] = clarification["required"]
        snapshot["clarification_reason"] = clarification["reason"]
        snapshot["user_status"] = {
            "NEEDS_CLARIFICATION": "WAITING_FOR_USER",
            "SUCCEEDED": "SUCCEEDED",
            "FAILED": "FAILED",
        }.get(snapshot.get("status"), "RUNNING")
        return snapshot
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Geo job not found") from exc


@router.get("/v1/geo/jobs/{job_id}/result")
def get_geo_job_result(job_id: str) -> dict[str, Any]:
    snapshot = get_geo_job(job_id)
    if snapshot["status"] not in {"SUCCEEDED", "FAILED", "NEEDS_CLARIFICATION"}:
        raise HTTPException(status_code=409, detail={"status": snapshot["status"], "message": "Result is not ready"})
    return snapshot


def _clarification_item(field_name: str) -> dict[str, str]:
    normalized = field_name.strip().lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("时间", "日期", "time", "date", "temporal")):
        return {
            "field": field_name,
            "label": "遥感影像时间范围",
            "question": "你希望查询哪个时间范围内的 Sentinel-2 影像？",
            "reason": "时间范围用于限定卫星目录检索，避免返回与任务无关的影像。",
            "placeholder": "例如：2025-04-01 至 2025-10-31",
        }
    if any(token in normalized for token in ("遥感数据类型", "影像类型", "sensor", "dataset", "数据类型")):
        return {
            "field": field_name,
            "label": "遥感数据类型",
            "question": "你希望检索哪类遥感数据？",
            "reason": "需要明确目录检索的数据集，本系统不会替你虚构数据类型。",
            "placeholder": "例如：Sentinel-2 L2A",
        }
    if any(token in normalized for token in ("地形", "terrain", "坡度", "高程", "aspect")):
        return {
            "field": field_name,
            "label": "地形分析指标",
            "question": "这次地形分析需要哪些指标？",
            "reason": "指标决定 Terrain Agent 实际计算高程、坡度或坡向中的哪些结果。",
            "placeholder": "例如：高程最小/最大/平均值、平均/最大坡度和坡向",
        }
    if any(token in normalized for token in ("摘要", "报告", "格式", "summary", "report", "format")):
        return {
            "field": field_name,
            "label": "结果摘要格式",
            "question": "你希望最终摘要采用什么形式？",
            "reason": "用于确定最终结果的组织方式，不改变分析证据。",
            "placeholder": "例如：中文要点，包含结论和数据来源",
        }
    if any(token in normalized for token in ("aoi", "region", "bbox", "location", "区域", "范围", "位置")):
        return {
            "field": field_name,
            "label": "分析区域",
            "question": "请在地图上选择或用文字说明需要分析的区域。",
            "reason": "空间分析必须有明确的 AOI。",
            "placeholder": "例如：甘肃兰州附近的小范围区域",
        }
    return {
        "field": field_name,
        "label": field_name,
        "question": f"请补充“{field_name}”。",
        "reason": "该信息被 Goal/Intent 层判定为继续规划所必需。",
        "placeholder": "请输入补充信息",
    }


def _clarification_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    intent = snapshot.get("intent") or {}
    missing = [str(item) for item in intent.get("missing_fields", []) if str(item).strip()]
    waiting = snapshot.get("status") == "NEEDS_CLARIFICATION"
    return {
        "required": waiting,
        "clarification_required": waiting,
        "count": len(missing) if waiting else 0,
        "reason": (
            "分析尚未开始；这些信息会影响正式数据检索或计算范围。"
            if waiting else None
        ),
        "clarification_reason": (
            "分析尚未开始；这些信息会影响正式数据检索或计算范围。"
            if waiting else None
        ),
        "items": [_clarification_item(item) for item in missing] if waiting else [],
        "resume_url": f"/v1/geo/jobs/{snapshot.get('job_id')}/clarification" if waiting else None,
    }


@router.patch("/v1/geo/jobs/{job_id}/clarification", status_code=status.HTTP_202_ACCEPTED)
def clarify_geo_job(job_id: str, body: GeoClarificationRequest) -> dict[str, Any]:
    """Merge user answers into the existing GoalContract and resume the same job."""
    store = MultiAgentStateStore()
    try:
        snapshot = store.execution_snapshot(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Geo job not found") from exc
    if snapshot["status"] != "NEEDS_CLARIFICATION":
        raise HTTPException(status_code=409, detail={
            "status": snapshot["status"],
            "message": "Only a job waiting for user clarification can be continued.",
        })
    intent = snapshot.get("intent") or {}
    missing = [str(item) for item in intent.get("missing_fields", []) if str(item).strip()]
    answers = {
        key: value.strip()
        for key, value in body.answers.items()
        if key in missing and isinstance(value, str) and value.strip()
    }
    supplemental = (body.supplemental_text or "").strip()
    if not answers and not supplemental:
        raise HTTPException(status_code=422, detail={
            "message": "At least one requested clarification answer is required.",
            "missing_fields": missing,
        })
    prior = GoalContract.model_validate(snapshot["goal_contract"])
    answer_lines = [f"{key}: {value}" for key, value in answers.items()]
    if supplemental:
        answer_lines.append(supplemental)
    continued_query = prior.primary_goal + "\n用户补充信息：\n" + "\n".join(answer_lines)
    clarification_constraints = {
        **prior.hard_constraints,
        **answers,
        "clarification_answers": answers,
    }
    orchestrator = GeoOrchestratorAgent(state_store=store)
    try:
        result = orchestrator.execute(
            continued_query,
            job_id=job_id,
            prior_contract=prior,
            use_llm=body.use_live_llm,
            handlers=build_formal_handlers(
                constraints=clarification_constraints,
                region_or_aoi=prior.region_or_aoi,
            ),
            resume=True,
            provider_status=formal_provider_status(),
            region_or_aoi=prior.region_or_aoi,
            constraints=clarification_constraints,
        )
        store.save_result(result)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={
            "error_type": type(exc).__name__, "message": str(exc),
        }) from exc
    return {
        "job_id": result.job_id,
        "status": result.status,
        "route_mode": result.metrics.get("route_mode"),
        "plan_mode": result.metrics.get("plan_mode"),
        "status_url": f"/v1/geo/jobs/{result.job_id}",
        "result_url": f"/v1/geo/jobs/{result.job_id}/result",
    }


@router.post("/geoagent/orchestrate")
def orchestrate(body: OrchestrateRequest) -> dict[str, Any]:
    """Compatibility endpoint; the formal general entry is /v1/geo/jobs."""
    orchestrator = GeoOrchestratorAgent()
    if body.execute:
        result = orchestrator.execute(body.query, use_llm=body.use_live_llm, handlers=build_formal_handlers())
        orchestrator.state_store.save_result(result)
    else:
        result = orchestrator.plan(body.query, use_llm=body.use_live_llm)
    return result.model_dump(mode="json")


@router.get("/geoagent/demo", response_class=HTMLResponse)
def demo() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Atlas · 地理空间智能分析</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root{--ink:#14221d;--muted:#65736d;--line:#dce5e0;--paper:#fff;--wash:#eef3f0;--green:#08775d;--green2:#0d9672;--blue:#2563eb;--amber:#d97706;--red:#c2413b;--shadow:0 14px 40px rgba(25,55,44,.10)}
    *{box-sizing:border-box}html,body{height:100%;margin:0}body{font:14px/1.5 Inter,"Segoe UI",system-ui,sans-serif;background:var(--wash);color:var(--ink);overflow:hidden}
    button,textarea{font:inherit}.topbar{height:68px;background:#10251e;color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 24px;border-bottom:1px solid rgba(255,255,255,.08)}
    .brand{display:flex;align-items:center;gap:12px}.brand-mark{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#32d6a3,#08775d);display:grid;place-items:center;font-weight:800;box-shadow:0 5px 18px rgba(42,214,161,.28)}
    .brand h1{font-size:18px;line-height:1.1;margin:0}.brand p{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#a9c5bb;margin:4px 0 0}.toplinks{display:flex;gap:10px}.toplinks a{color:#cfe3dc;text-decoration:none;font-size:12px;padding:7px 10px;border:1px solid #365248;border-radius:20px}
    .workspace{height:calc(100vh - 68px);display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:0}.main{min-width:0;display:flex;flex-direction:column;padding:18px 16px 18px 20px;gap:12px}
    .composer{background:rgba(255,255,255,.97);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:11px 12px;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}
    .composer textarea{border:0;outline:0;resize:none;height:48px;padding:4px 6px;color:var(--ink);background:transparent}.composer textarea::placeholder{color:#95a19c}
    .run-button{height:44px;padding:0 19px;border:0;border-radius:12px;color:white;background:linear-gradient(135deg,var(--green2),var(--green));font-weight:700;cursor:pointer;box-shadow:0 7px 18px rgba(8,119,93,.24)}.run-button:disabled{opacity:.55;cursor:wait}
    .map-shell{position:relative;flex:1;min-height:420px;border-radius:18px;overflow:hidden;border:1px solid #cad8d1;background:#dfe8e3;box-shadow:var(--shadow)}#map{height:100%;width:100%}
    .map-title{position:absolute;z-index:500;left:14px;top:14px;background:rgba(16,37,30,.90);color:#fff;padding:9px 12px;border-radius:12px;backdrop-filter:blur(8px);pointer-events:none}.map-title strong{display:block;font-size:13px}.map-title span{font-size:11px;color:#b7d2c8}
    .map-legend{position:absolute;z-index:500;left:14px;bottom:22px;background:rgba(255,255,255,.94);border:1px solid #d9e2dd;border-radius:12px;padding:9px 11px;box-shadow:0 7px 20px rgba(20,34,29,.12);font-size:11px}.legend-row{display:flex;align-items:center;gap:7px;margin:4px 0}.swatch{width:18px;height:7px;border-radius:8px}.swatch.result{background:#0d9672}.swatch.aoi{background:#2563eb}.swatch.intersect{background:#f59e0b}.swatch.sentinel{background:#7c3aed}
    .map-empty{position:absolute;z-index:450;inset:auto 50% 24px auto;transform:translateX(50%);background:rgba(255,255,255,.92);color:#53625c;border-radius:24px;padding:8px 14px;box-shadow:0 5px 16px rgba(0,0,0,.10);white-space:nowrap;font-size:12px}
    .sidebar{background:#f8faf9;border-left:1px solid #d7e1dc;overflow:auto;padding:18px 18px 30px}.panel{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:15px;margin-bottom:12px;box-shadow:0 6px 20px rgba(20,34,29,.05)}
    .status-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.eyebrow{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:#87938e;font-weight:700}.status-main{font-size:20px;font-weight:800;margin:2px 0}.status-dot{width:10px;height:10px;border-radius:50%;background:#94a3b8;box-shadow:0 0 0 5px #eef2f1;margin-top:8px}.status-dot.running{background:var(--amber);box-shadow:0 0 0 5px #fff3d8}.status-dot.success{background:var(--green2);box-shadow:0 0 0 5px #dff7ef}.status-dot.failed{background:var(--red);box-shadow:0 0 0 5px #fde8e6}
    .badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.badge{font-size:10px;font-weight:750;letter-spacing:.03em;border-radius:20px;padding:4px 8px;background:#edf3f0;color:#496159}.badge.llm{background:#e5f1ff;color:#245ebc}.badge.live{background:#dcf8ed;color:#08775d}.badge.cache{background:#f2eaff;color:#7043a8}.badge.offline{background:#e8eefc;color:#365db3}.badge.skipped{background:#f1f3f2;color:#6b7772}
    .progress-track{height:5px;background:#e7eeea;border-radius:10px;overflow:hidden;margin-top:13px}.progress-bar{height:100%;width:0;background:linear-gradient(90deg,#20ba8c,#08775d);transition:width .35s ease}.agent-strip{margin-top:12px;display:flex;gap:5px;flex-wrap:wrap}.agent-pill{font-size:10px;padding:4px 7px;border-radius:7px;background:#f1f5f3;color:#52635c}.agent-pill.done{background:#e1f5ed;color:#08775d}
    .section-title{font-size:12px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:center}.section-title span{color:#8a9691;font-size:10px;font-weight:400}.summary-text{color:#3d4e47;margin:0;white-space:pre-line}.empty{color:#929d98;font-size:12px}.list{display:grid;gap:8px}.list-item{padding:9px 10px;background:#f6f8f7;border:1px solid #e8eeeb;border-radius:10px}.list-item strong{display:block;font-size:11px}.list-item small{color:#78857f;display:block;margin-top:2px;overflow-wrap:anywhere}
    details{background:#fff;border:1px solid var(--line);border-radius:14px;margin-bottom:10px;overflow:hidden}summary{cursor:pointer;padding:13px 14px;font-weight:700;list-style:none;display:flex;align-items:center;justify-content:space-between}summary::-webkit-details-marker{display:none}summary:after{content:'＋';color:#7a8882;font-size:16px}details[open]>summary:after{content:'−'}details[open]>summary{border-bottom:1px solid var(--line)}.detail-body{padding:13px}
    .detail-grid{display:grid;gap:9px}.intent-card{padding:10px;border-left:3px solid var(--green2);background:#f1f8f5;border-radius:0 9px 9px 0}.intent-card strong{display:block}.task{display:grid;grid-template-columns:25px 1fr auto;gap:8px;align-items:start;padding:8px 0;border-bottom:1px solid #edf1ef}.task:last-child{border:0}.task-index{width:22px;height:22px;border-radius:7px;background:#e4efe9;display:grid;place-items:center;color:#08775d;font-size:10px;font-weight:800}.task small{color:#7a8781}.tool-table{width:100%;border-collapse:collapse;font-size:11px}.tool-table th{text-align:left;color:#7c8883;padding:6px 4px;border-bottom:1px solid #e4eae7}.tool-table td{padding:7px 4px;border-bottom:1px solid #eef2f0;vertical-align:top}.raw{margin:0;max-height:360px;overflow:auto;background:#13201b;color:#d5e7df;padding:12px;border-radius:9px;font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
    .error-box{display:none;background:#fff0ee;color:#9e332e;border:1px solid #f1c7c3;padding:10px;border-radius:10px;margin-top:10px}.leaflet-control-layers{border:0!important;border-radius:11px!important;box-shadow:0 5px 18px rgba(0,0,0,.15)!important}
    .user-stage{margin-top:12px;padding:10px 11px;border-radius:10px;background:#f4f7f5;color:#52615b;font-size:12px}.user-stage div+div{margin-top:5px}.clarification-panel{border-color:#f2cf91;background:#fffdf7}.clarification-panel[hidden]{display:none}.clarification-intro{color:#66563c;margin:0 0 12px}.clarification-field{display:block;margin:10px 0}.clarification-field strong{display:block;font-size:12px}.clarification-field small{display:block;color:#7c6b4c;margin:3px 0 6px}.clarification-input{width:100%;border:1px solid #ddcda9;border-radius:9px;padding:9px 10px;background:#fff;outline:none}.clarification-input:focus{border-color:#c58a21;box-shadow:0 0 0 3px #f9ebce}.continue-button{width:100%;border:0;border-radius:10px;padding:10px 12px;background:#b66b08;color:#fff;font-weight:750;cursor:pointer}.continue-button:disabled{opacity:.55;cursor:wait}.technical-badges{margin:0 0 12px}
    @media(max-width:980px){body{overflow:auto}.workspace{height:auto;display:block}.main{height:72vh;padding:12px}.sidebar{border-left:0;border-top:1px solid var(--line);overflow:visible}.toplinks{display:none}}
    @media(max-width:560px){.topbar{padding:0 14px}.brand p{display:none}.main{height:70vh;padding:8px}.composer{grid-template-columns:1fr}.composer textarea{height:54px}.run-button{width:100%}.sidebar{padding:12px}.map-legend{display:none}}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand"><div class="brand-mark">A</div><div><h1>Atlas</h1><p>地理空间分析与新能源选址 Agent</p></div></div>
    <nav class="toplinks"><a href="/docs" target="_blank">API Docs</a><a href="/">Renewable Demo</a></nav>
  </header>
  <main class="workspace">
    <section class="main">
      <div class="composer">
        <textarea id="query" aria-label="地理空间分析请求" placeholder="用自然语言描述地点、范围和你希望完成的分析……">对甘肃小范围 AOI 的地形与遥感可用性进行综合分析，并生成带来源记录的摘要。</textarea>
        <button id="run-button" class="run-button" type="button">执行分析</button>
      </div>
      <div class="map-shell">
        <div id="map" aria-label="Atlas 结果地图"></div>
        <div class="map-title"><strong>分析地图</strong><span id="layer-count">等待结果图层</span></div>
        <div id="map-legend" class="map-legend" hidden></div>
        <div id="map-empty" class="map-empty">执行任务后，AOI 与可用 GIS Artifact 将自动显示</div>
      </div>
    </section>
    <aside class="sidebar">
      <section class="panel">
        <div class="status-head"><div><div class="eyebrow">分析状态</div><div id="status-main" class="status-main">等待任务</div><div id="job-label" class="empty">尚未创建 Geo Job</div></div><i id="status-dot" class="status-dot"></i></div>
        <div id="user-stage" class="user-stage">选择分析范围并描述你的目标后开始。</div>
        <div id="error-box" class="error-box"></div>
      </section>
      <section id="clarification-panel" class="panel clarification-panel" hidden>
        <h2 id="clarification-title" class="section-title">需要补充信息</h2>
        <p id="clarification-intro" class="clarification-intro"></p>
        <div id="clarification-fields"></div>
        <button id="continue-button" class="continue-button" type="button">继续分析</button>
      </section>
      <section class="panel"><h2 class="section-title">分析结果 <span id="review-label">等待分析</span></h2><div id="analysis-summary" class="summary-text empty">提交自然语言请求后，这里会显示面向用户的结论和审核状态。</div></section>
      <section class="panel"><h2 class="section-title">数据来源 <span id="source-count">0 项</span></h2><div id="source-list" class="list"><div class="empty">暂无来源记录</div></div></section>
      <section class="panel"><h2 class="section-title">结果文件 <span id="artifact-count">0 项</span></h2><div id="artifact-list" class="list"><div class="empty">暂无正式结果</div></div></section>
      <details id="agent-details"><summary><span>查看技术执行详情</span><span id="detail-count" class="badge">0 steps</span></summary><div class="detail-body">
        <div class="badges technical-badges"><span id="route-badge" class="badge">Route: —</span><span id="plan-badge" class="badge">Plan: —</span><span id="intent-badge" class="badge">Intent: —</span></div>
        <div id="agent-strip" class="agent-strip"><span class="agent-pill">尚未执行 Agent</span></div>
        <div id="intent-card" class="intent-card"><strong>Intent 尚未识别</strong><small>GoalContract 将在任务完成后显示</small></div>
        <h3 class="section-title" style="margin-top:14px">TaskGraph 节点</h3><div id="task-list" class="detail-grid"><div class="empty">暂无规划节点</div></div>
        <h3 class="section-title" style="margin-top:14px">实际 Tool Calls</h3><div id="tool-list" class="empty">暂无实际调用</div>
        <h3 class="section-title" style="margin-top:14px">Evidence / Review / Replan</h3><div id="governance-list" class="list"><div class="empty">暂无记录</div></div>
      </div></details>
      <details id="advanced-debug"><summary>Advanced Debug · 原始 JSON</summary><div class="detail-body"><pre id="debug-json" class="raw">{}</pre></div></details>
    </aside>
  </main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const els={query:document.getElementById('query'),run:document.getElementById('run-button'),status:document.getElementById('status-main'),dot:document.getElementById('status-dot'),job:document.getElementById('job-label'),route:document.getElementById('route-badge'),plan:document.getElementById('plan-badge'),intent:document.getElementById('intent-badge'),agents:document.getElementById('agent-strip'),stage:document.getElementById('user-stage'),clarification:document.getElementById('clarification-panel'),clarificationTitle:document.getElementById('clarification-title'),clarificationIntro:document.getElementById('clarification-intro'),clarificationFields:document.getElementById('clarification-fields'),continueButton:document.getElementById('continue-button'),analysis:document.getElementById('analysis-summary'),review:document.getElementById('review-label'),sources:document.getElementById('source-list'),sourceCount:document.getElementById('source-count'),artifacts:document.getElementById('artifact-list'),artifactCount:document.getElementById('artifact-count'),tasks:document.getElementById('task-list'),tools:document.getElementById('tool-list'),governance:document.getElementById('governance-list'),intentCard:document.getElementById('intent-card'),detailCount:document.getElementById('detail-count'),debug:document.getElementById('debug-json'),error:document.getElementById('error-box'),layerCount:document.getElementById('layer-count'),mapEmpty:document.getElementById('map-empty'),legend:document.getElementById('map-legend')};
    const map=L.map('map',{zoomControl:false,preferCanvas:true}).setView([35.7,103.8],5);L.control.zoom({position:'bottomright'}).addTo(map);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);
    const aoiLayers=L.featureGroup().addTo(map),resultLayers=L.featureGroup().addTo(map);L.control.layers(null,{'分析范围 / AOI':aoiLayers,'GIS / Artifact 结果':resultLayers},{position:'topright',collapsed:true}).addTo(map);
    let currentJobId=null,currentAoi=null;
    const acceptedGansuDemoAoi={bbox:[102.99885031030179,37.59704374600004,103.09885031030178,37.697043746000034],crs:'EPSG:4326',label:'甘肃已验收小范围 AOI'};
    currentAoi=acceptedGansuDemoAoi;
    const esc=value=>String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    const label=value=>String(value??'—').replaceAll('_',' ').replaceAll('-agent','').replace(/\b\w/g,c=>c.toUpperCase());
    const short=value=>{const text=String(value??'');return text.length>84?text.slice(0,81)+'…':text};
    const basename=value=>String(value??'').split(/[\\/]/).filter(Boolean).pop()||String(value??'');
    function requestAoi(query){return /甘肃|gansu/i.test(query)&&/(小范围|small|AOI)/i.test(query)?acceptedGansuDemoAoi:null}
    function friendlySource(value){const text=String(value??'');if(/^[A-Za-z]:[\\/]/.test(text))return `本地已验收缓存 · ${basename(text)}`;return text.replace('copernicus_glo30','Copernicus DEM GLO-30').replace('copernicus_dataspace_stac','Copernicus Data Space').replace('Copernicus Sentinel STAC','Copernicus Data Space Sentinel-2')}
    function previewOutputs(snapshot){return (snapshot.artifact_previews||[]).flatMap(item=>Array.isArray(item.outputs)?item.outputs:[])}
    function statusText(value){return ({SUCCEEDED:'分析完成',FAILED:'分析失败',RUNNING:'正在分析',PLANNING:'正在规划',PENDING:'等待执行',NEEDS_CLARIFICATION:'需要补充信息',NEEDS_HUMAN_REVIEW:'需要人工审核',REUSED:'已复用'})[value]||value||'等待任务'}
    function setBusy(busy){els.run.disabled=busy;els.run.textContent=busy?'正在分析…':'执行分析';els.continueButton.disabled=busy}
    function showAnalyzing(message='已提交任务，正在理解需求并准备正式分析。'){els.status.textContent='正在分析你的区域…';els.dot.className='status-dot running';els.stage.innerHTML=`<div>✓ 已收到你的目标和当前 AOI</div><div>→ ${esc(message)}</div>`;els.clarification.hidden=true;els.analysis.className='summary-text empty';els.analysis.textContent='分析完成后，这里会显示地形、遥感和综合结论。';els.review.textContent='正在分析'}
    function showError(message){els.error.style.display='block';els.error.textContent=message;els.status.textContent='请求失败';els.dot.className='status-dot failed'}
    function classBadge(value){const c=value==='LIVE_NETWORK_EXECUTED'?'live':value==='CACHE_REUSED'?'cache':value==='OFFLINE_LOCAL_DATA_EXECUTED'?'offline':'skipped';return `<span class="badge ${c}">${esc(value||'UNKNOWN')}</span>`}
    function extractSources(value,out=new Set(),depth=0){if(depth>7||value==null)return out;if(Array.isArray(value)){value.forEach(v=>extractSources(v,out,depth+1));return out}if(typeof value!=='object')return out;for(const [key,item] of Object.entries(value)){if(['provider','source','source_url','endpoint','dataset','collection'].includes(key)&&typeof item==='string'&&item.trim())out.add(item);else extractSources(item,out,depth+1)}return out}
    function collectGeoJSON(value,out=[],seen=new WeakSet(),depth=0){if(depth>9||value==null||typeof value!=='object'||seen.has(value))return out;seen.add(value);if(value.type==='FeatureCollection'&&Array.isArray(value.features)){out.push(value);return out}if(value.type==='Feature'&&value.geometry){out.push({type:'FeatureCollection',features:[value]});return out}if(['Point','MultiPoint','LineString','MultiLineString','Polygon','MultiPolygon','GeometryCollection'].includes(value.type)){out.push({type:'FeatureCollection',features:[{type:'Feature',properties:{},geometry:value}]});return out}for(const item of Object.values(value))collectGeoJSON(item,out,seen,depth+1);return out}
    function addAoi(snapshot){let region=snapshot?.goal_contract?.region_or_aoi||currentAoi||requestAoi(els.query.value);if(!region||typeof region!=='object')return 0;currentAoi=region;const bbox=region.bbox;if(Array.isArray(bbox)&&bbox.length===4&&bbox.every(Number.isFinite)){L.rectangle([[bbox[1],bbox[0]],[bbox[3],bbox[2]]],{color:'#2563eb',weight:2,dashArray:'7 5',fillColor:'#60a5fa',fillOpacity:.07}).bindTooltip(region.label||'当前分析范围').addTo(aoiLayers);return 1}const geos=collectGeoJSON(region);geos.forEach(g=>L.geoJSON(g,{style:{color:'#2563eb',weight:2,dashArray:'7 5',fillOpacity:.07}}).addTo(aoiLayers));return geos.length}
    function resultStyle(feature){const p=feature?.properties||{},operation=String(p.operation||p.analysis_operation||'').toLowerCase();if(p.layer_type==='sentinel_footprint')return {color:'#6d28d9',weight:2,fillColor:'#8b5cf6',fillOpacity:.14};if(operation.includes('intersect'))return {color:'#d97706',weight:3,fillColor:'#f59e0b',fillOpacity:.30};if(p.candidate_id||p.rank)return {color:'#08775d',weight:2,fillColor:'#20b486',fillOpacity:.38};return {color:'#08775d',weight:3,fillColor:'#2dd4a2',fillOpacity:.24}}
    function stacFootprints(snapshot){const features=[];for(const output of previewOutputs(snapshot)){if(output?.operation!=='search_sentinel'||!Array.isArray(output.items))continue;for(const item of output.items){let geometry=item.geometry;if(!geometry&&Array.isArray(item.bbox)&&item.bbox.length===4){const [w,s,e,n]=item.bbox;geometry={type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]}}if(geometry)features.push({type:'Feature',geometry,properties:{layer_type:'sentinel_footprint',item_id:item.id,acquisition_datetime:item.datetime,cloud_cover:item.cloud_cover,collection:item.collection,provider:output.provider||'Copernicus Data Space'}})}}return features.length?[{type:'FeatureCollection',features}]:[]}
    function updateLegend({aoi=false,results=false,sentinel=false}={}){const rows=[];if(aoi)rows.push('<div class="legend-row"><i class="swatch aoi"></i>当前分析范围 / AOI</div>');if(results)rows.push('<div class="legend-row"><i class="swatch result"></i>真实 GIS 结果图层</div>');if(sentinel)rows.push('<div class="legend-row"><i class="swatch sentinel"></i>Sentinel 目录覆盖范围</div>');els.legend.innerHTML=rows.join('');els.legend.hidden=!rows.length}
    function renderMap(snapshot={}){aoiLayers.clearLayers();resultLayers.clearLayers();const aoiCount=addAoi(snapshot);const mapOutputs=previewOutputs(snapshot).filter(output=>output?.operation!=='search_sentinel'&&(output?.geojson||['FeatureCollection','Feature','Point','MultiPoint','LineString','MultiLineString','Polygon','MultiPolygon','GeometryCollection'].includes(output?.type)));const resultGeos=collectGeoJSON(mapOutputs),sentinelGeos=stacFootprints(snapshot);resultGeos.concat(sentinelGeos).forEach((geo,index)=>{L.geoJSON(geo,{style:resultStyle,pointToLayer:(f,ll)=>L.circleMarker(ll,{radius:7,...resultStyle(f)}),onEachFeature:(f,layer)=>{const p=f.properties||{};const rows=Object.entries(p).slice(0,8).map(([k,v])=>`<b>${esc(label(k))}</b>: ${esc(typeof v==='object'?JSON.stringify(v):v)}`).join('<br>');layer.bindPopup(rows||`结果图层 ${index+1}`)}}).addTo(resultLayers)});const resultCount=resultGeos.length+sentinelGeos.length,count=aoiCount+resultCount;updateLegend({aoi:aoiCount>0,results:resultGeos.length>0,sentinel:sentinelGeos.length>0});const combined=L.featureGroup([...aoiLayers.getLayers(),...resultLayers.getLayers()]);if(combined.getLayers().length){const bounds=combined.getBounds();if(bounds.isValid())map.fitBounds(bounds,{padding:[38,38],maxZoom:14});els.mapEmpty.style.display='none'}else els.mapEmpty.style.display='block';els.layerCount.textContent=resultCount?`${resultCount} 个真实结果图层`:(aoiCount?'当前仅显示 AOI':'等待结果图层')}
    const numberText=(value,digits=1)=>Number.isFinite(Number(value))?Number(value).toFixed(digits):null;
    function domainSummaryHtml(snapshot){const outputs=previewOutputs(snapshot),blocks=[];const terrain=outputs.find(item=>Number.isFinite(item?.elevation_mean_m)||Number.isFinite(item?.slope_mean_deg));const liveRemote=outputs.find(item=>item?.operation==='search_sentinel'&&Array.isArray(item.items)),cachedRemote=outputs.flatMap(item=>item?.providers||[]).find(item=>item?.item_metadata?.item_count),remote=liveRemote||cachedRemote?.item_metadata;let terrainText=null,remoteText=null;if(terrain){const slope=Number(terrain.slope_mean_deg);terrainText=Number.isFinite(slope)?(slope<5?'区域整体较平缓，通常有利于施工组织。':slope<15?'区域以缓坡为主，局部施工需要进行坡面处理。':'区域起伏较明显，施工与场地整平成本需要重点评估。'):'本次已完成高程统计。';const elevation=[['最低',numberText(terrain.elevation_min_m)],['最高',numberText(terrain.elevation_max_m)],['平均',numberText(terrain.elevation_mean_m)]].filter(([,v])=>v).map(([k,v])=>`${k} ${v} m`).join('，');const slopes=[['平均坡度',numberText(terrain.slope_mean_deg,2)],['最大坡度',numberText(terrain.slope_max_deg,2)]].filter(([,v])=>v).map(([k,v])=>`${k} ${v}°`).join('，');const terrainProvider=(snapshot.actual_tool_calls||[]).find(item=>item.agent_id==='terrain-agent')?.provider||'copernicus_glo30';blocks.push(`<div class="list-item"><strong>地形情况</strong><small>${elevation?`高程：${esc(elevation)}。<br>`:''}${slopes?`${esc(slopes)}。<br>`:''}${esc(terrainText)}<br>数据来源：${esc(friendlySource(terrainProvider))}<br>结果形式：当前为真实统计结果；没有可绘制的地形栅格时，地图不会伪造坡度图层。</small></div>`)}if(remote){const items=liveRemote?.items||[],latest=items.map(item=>item.datetime).filter(Boolean).sort().reverse()[0],clouds=items.map(item=>Number(item.cloud_cover)).filter(Number.isFinite),dateText=latest||remote.date_range,cloudText=clouds.length?`最低云量 ${Math.min(...clouds).toFixed(2)}%`:(Number.isFinite(Number(remote.cloud_cover_lte))?`检索云量上限 ${Number(remote.cloud_cover_lte).toFixed(0)}%`:null),call=(snapshot.actual_tool_calls||[]).find(item=>item.tool==='search_sentinel'),mode=call?.execution_class==='LIVE_NETWORK_EXECUTED'?'实时目录查询':'已验收缓存',rgbSkipped=(snapshot.actual_tool_calls||[]).some(item=>item.tool==='create_rgb_chip'&&item.execution_class==='SKIPPED'),footprints=stacFootprints(snapshot).length;remoteText=`找到 ${remote.item_count??items.length} 景 Sentinel-2 目录记录`;blocks.push(`<div class="list-item"><strong>遥感可用性</strong><small>${esc(remoteText)}${dateText?`，时间 ${esc(dateText)}`:''}${cloudText?`，${esc(cloudText)}`:''}。<br>数据来源：${esc(friendlySource(liveRemote?.provider||cachedRemote?.provider||call?.provider||'Copernicus Data Space'))} · ${esc(mode)}。<br>本次仅完成卫星目录可用性检索，未下载完整卫星场景${rgbSkipped?'，因此没有生成 RGB 影像预览':''}。${footprints?'目录记录包含空间范围，已显示 footprint。':'目录记录没有 geometry/bbox，因此地图未绘制 footprint。'}</small></div>`)}if(terrainText||remoteText){const parts=[];if(terrainText)parts.push(terrainText);if(remoteText)parts.push(`${remoteText}，可用于判断指定时段的数据可用性。`);blocks.push(`<div class="list-item"><strong>综合结论</strong><small>${esc(parts.join(' '))} 所有结论均来自本次 Artifact 与 Evidence，未生成或缺失的图层不会在地图上显示。</small></div>`)}return blocks.join('')}
    function stageHtml(snapshot){const attempts=snapshot.actual_attempts||[],done=new Set(attempts.filter(a=>['SUCCEEDED','REUSED'].includes(a.status)).map(a=>a.agent_id));const stages=['<div>✓ 已理解你的需求并确认 AOI</div>'];if(done.has('data-agent'))stages.push('<div>✓ 已准备分析数据</div>');if(done.has('terrain-agent'))stages.push('<div>✓ 已完成地形指标计算</div>');if(done.has('remote_sensing-agent'))stages.push('<div>✓ 已完成 Sentinel 目录检索</div>');if(done.has('map_report-agent'))stages.push('<div>✓ 已生成综合结果</div>');return stages.join('')}
    function renderClarification(snapshot){const payload=snapshot.clarification||{},items=payload.items||[];if(snapshot.status!=='NEEDS_CLARIFICATION'){els.clarification.hidden=true;els.clarificationFields.innerHTML='';return}els.clarification.hidden=false;els.clarificationTitle.textContent=`还需要补充 ${items.length} 项信息`;els.clarificationIntro.textContent=payload.reason||'请回答下面的问题，系统会继续原任务。';els.clarificationFields.innerHTML=items.map(item=>`<label class="clarification-field"><strong>${esc(item.label||item.field)}</strong><small>${esc(item.question)} ${esc(item.reason||'')}</small><input class="clarification-input" data-field="${esc(item.field)}" placeholder="${esc(item.placeholder||'请输入补充信息')}" required></label>`).join('')}
    function renderSummary(snapshot){const attempts=snapshot.actual_attempts||[],done=attempts.filter(a=>['SUCCEEDED','REUSED'].includes(a.status)),status=snapshot.status;els.status.textContent=status==='NEEDS_CLARIFICATION'?`还需要补充 ${(snapshot.clarification?.items||[]).length} 项信息`:statusText(status);els.dot.className='status-dot '+(status==='SUCCEEDED'?'success':status==='FAILED'?'failed':'running');els.job.textContent=`Job ${snapshot.job_id}`;els.route.textContent=`Route: ${String(snapshot.route_mode||'—').toUpperCase()}`;els.route.className='badge '+(snapshot.route_mode==='llm'?'llm':'');els.plan.textContent=`Plan: ${String(snapshot.plan_mode||'—').toUpperCase()}`;els.plan.className='badge '+(snapshot.plan_mode==='llm'?'llm':'');const intent=snapshot.intent?.intent||snapshot.goal_contract?.intent||snapshot.intent||'—';els.intent.textContent=`Intent: ${label(intent)}`;const active=attempts.filter(a=>['RUNNING','IN_PROGRESS'].includes(a.status)).map(a=>a.agent_id),completed=[...new Set(done.map(a=>a.agent_id))];els.agents.innerHTML=(active.map(a=>`<span class="agent-pill">● ${esc(label(a))}</span>`).concat(completed.map(a=>`<span class="agent-pill done">✓ ${esc(label(a))}</span>`)).join(''))||'<span class="agent-pill">尚未执行 Agent</span>';
      const review=snapshot.review||{},reason=review.reason||review.summary||review.conclusion,domainSummary=domainSummaryHtml(snapshot);els.analysis.className='summary-text';if(status==='NEEDS_CLARIFICATION'){els.stage.innerHTML='<div>✓ 已理解你的需求和当前 AOI</div><div>→ 等待你补充信息，分析尚未开始。</div>';els.analysis.innerHTML='<div class="list-item"><strong>分析尚未开始</strong><small>请完成上方补充问题。提交后系统会继续同一个 Job，不需要重新描述原任务。</small></div>';els.review.textContent='等待用户';els.sourceCount.textContent='—';els.sources.innerHTML='<div class="empty">分析尚未开始，暂无数据来源记录。</div>';els.artifactCount.textContent='—';els.artifacts.innerHTML='<div class="empty">分析尚未开始，暂无结果文件。</div>'}else{els.stage.innerHTML=status==='SUCCEEDED'?stageHtml(snapshot):status==='FAILED'?'<div>分析执行失败，请查看错误信息或技术详情。</div>':'<div>→ 正在执行已规划的真实分析步骤。</div>';els.analysis.innerHTML=domainSummary||`<div class="list-item"><strong>${status==='SUCCEEDED'?'分析完成':'正在生成结果'}</strong><small>${esc(reason||'系统正在整理真实 Artifact 与 Evidence。')}</small></div>`;els.review.textContent=review.status?`审核：${review.status}`:(status==='SUCCEEDED'?'分析完成':'正在分析');const sourceValues=[...extractSources({evidence:snapshot.evidence,previews:snapshot.artifact_previews,attempts:snapshot.actual_attempts,artifacts:snapshot.artifacts})].map(friendlySource).filter((item,index,array)=>array.indexOf(item)===index);els.sourceCount.textContent=`${sourceValues.length} 项`;els.sources.innerHTML=sourceValues.length?sourceValues.slice(0,12).map((item,i)=>`<div class="list-item"><strong>${i+1}. ${esc(short(item))}</strong><small>来源记录已随分析结果保存</small></div>`).join(''):'<div class="empty">当前结果没有可提取的数据来源标签</div>';const artifacts=(snapshot.final_artifacts?.length?snapshot.final_artifacts:snapshot.artifacts)||[];els.artifactCount.textContent=`${artifacts.length} 项`;els.artifacts.innerHTML=artifacts.length?artifacts.slice(0,10).map(a=>`<div class="list-item"><strong>${esc(label(a.artifact_type||a.kind||'分析结果'))}</strong><small>${esc(label(a.producer_agent||a.metadata?.producer_agent||a.producer||'GeoAgent'))} · ${esc(label(a.task_id||a.step_id||'正式结果'))}</small></div>`).join(''):'<div class="empty">暂无正式结果文件</div>'}
    }
    function renderDetails(snapshot){const nodes=snapshot.task_graph?.nodes||[],calls=snapshot.actual_tool_calls||[],evidence=snapshot.evidence||[],replans=snapshot.replans||[],contract=snapshot.goal_contract||{},intent=snapshot.intent?.intent||contract.intent||'unknown';els.intentCard.innerHTML=`<strong>${esc(label(intent))}</strong><small>${esc(contract.primary_goal||els.query.value)}</small>`;els.detailCount.textContent=`${nodes.length} steps`;els.tasks.innerHTML=nodes.length?nodes.map((n,i)=>`<div class="task"><span class="task-index">${i+1}</span><div><strong>${esc(n.objective||n.task_id)}</strong><small>${esc(label(n.agent_role))} · ${esc((n.allowed_tools||[]).join(', ')||'No tool')}</small></div><span class="badge">${esc(n.status||'PLANNED')}</span></div>`).join(''):'<div class="empty">暂无 TaskGraph 节点</div>';els.tools.innerHTML=calls.length?`<table class="tool-table"><thead><tr><th>Agent</th><th>Tool</th><th>执行状态</th></tr></thead><tbody>${calls.map(c=>`<tr><td>${esc(label(c.agent_id))}</td><td><b>${esc(c.tool)}</b><br><small>${esc(short(c.task_id))}</small></td><td>${classBadge(c.execution_class)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">暂无实际 Tool Calls；计划调用不会在此计数。</div>';const review=snapshot.review||{};const governance=[];if(evidence.length)governance.push(`<div class="list-item"><strong>Evidence · ${evidence.length} 项</strong><small>来源、校验和与接受状态已保留</small></div>`);if(review.status)governance.push(`<div class="list-item"><strong>Review · ${esc(review.status)}</strong><small>${esc(short(review.reason||review.summary||'Reviewer 已完成检查'))}</small></div>`);if(replans.length)governance.push(`<div class="list-item"><strong>Local Replan · ${replans.length} 次</strong><small>${esc(replans.map(r=>r.reason||r.patch_id).filter(Boolean).map(short).join('；'))}</small></div>`);els.governance.innerHTML=governance.join('')||'<div class="empty">暂无 Evidence / Review / Replan 记录</div>';
      const debugPayload={user_query:els.query.value,detected_intent:snapshot.intent,route_mode:snapshot.route_mode,plan_mode:snapshot.plan_mode,clarification:snapshot.clarification,goal_contract:snapshot.goal_contract,task_graph:snapshot.task_graph,selected_agents:snapshot.selected_agents,active_agent:(snapshot.actual_attempts||[]).filter(a=>['RUNNING','IN_PROGRESS'].includes(a.status)).map(a=>a.agent_id),completed_agents:[...new Set((snapshot.actual_attempts||[]).filter(a=>['SUCCEEDED','REUSED'].includes(a.status)).map(a=>a.agent_id))],actual_attempts:snapshot.actual_attempts,actual_tool_calls:snapshot.actual_tool_calls,execution_classes:snapshot.execution_classes,artifacts:snapshot.artifacts,artifact_previews:snapshot.artifact_previews,evidence:snapshot.evidence,review:snapshot.review,replans:snapshot.replans,final_result:snapshot.final_artifacts,metrics:snapshot.metrics};els.debug.textContent=JSON.stringify(debugPayload,null,2)
    }
    function render(snapshot){currentJobId=snapshot.job_id;if(snapshot.goal_contract?.region_or_aoi)currentAoi=snapshot.goal_contract.region_or_aoi;const originalGoal=String(snapshot.goal_contract?.primary_goal||'').split('\n用户补充信息：')[0].trim();if(originalGoal)els.query.value=originalGoal;renderSummary(snapshot);renderClarification(snapshot);renderDetails(snapshot);renderMap(snapshot);document.title=`${statusText(snapshot.status)} · Atlas`}
    async function loadJob(jobId){const response=await fetch(`/v1/geo/jobs/${encodeURIComponent(jobId)}`);const payload=await response.json();if(!response.ok)throw new Error(payload.detail?.message||payload.detail||`HTTP ${response.status}`);render(payload);return payload}
    async function run(){const user_query=els.query.value.trim();if(user_query.length<3){showError('请至少输入 3 个字符的分析目标。');return}setBusy(true);els.error.style.display='none';currentAoi=requestAoi(user_query)||currentAoi;renderMap({goal_contract:{region_or_aoi:currentAoi}});showAnalyzing();try{const response=await fetch('/v1/geo/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_query,region_or_aoi:currentAoi})});const created=await response.json();if(!response.ok)throw new Error(created.detail?.message||created.detail||`HTTP ${response.status}`);currentJobId=created.job_id;history.replaceState({},'',`?job_id=${encodeURIComponent(currentJobId)}`);await loadJob(currentJobId)}catch(error){showError(error instanceof Error?error.message:String(error))}finally{setBusy(false)}}
    async function continueAnalysis(){if(!currentJobId){showError('当前没有可以继续的 Job。');return}const inputs=[...els.clarificationFields.querySelectorAll('.clarification-input')],answers={};for(const input of inputs){const value=input.value.trim();if(!value){input.focus();showError(`请先回答：${input.previousElementSibling?.previousElementSibling?.textContent||input.dataset.field}`);return}answers[input.dataset.field]=value}setBusy(true);els.error.style.display='none';showAnalyzing('已收到补充信息，正在继续原任务。');try{const response=await fetch(`/v1/geo/jobs/${encodeURIComponent(currentJobId)}/clarification`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers})});const continued=await response.json();if(!response.ok)throw new Error(continued.detail?.message||continued.detail||`HTTP ${response.status}`);if(continued.job_id!==currentJobId)throw new Error('服务端没有继续原 Job。');await loadJob(currentJobId)}catch(error){showError(error instanceof Error?error.message:String(error));await loadJob(currentJobId).catch(()=>{})}finally{setBusy(false)}}
    els.run.addEventListener('click',run);els.continueButton.addEventListener('click',continueAnalysis);els.query.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter')run()});window.addEventListener('load',async()=>{setTimeout(()=>map.invalidateSize(),80);const jobId=new URLSearchParams(location.search).get('job_id');if(jobId){setBusy(true);try{await loadJob(jobId)}catch(error){showError(`无法加载 Job ${jobId}：${error.message}`)}finally{setBusy(false)}}else{renderMap({goal_contract:{region_or_aoi:currentAoi}})}});
  </script>
</body>
</html>"""
