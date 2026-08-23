"""Runtime helpers for the Solara GeoAgent web UI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable, Sequence

from geoagent import (
    GeoAgent,
    GeoAgentConfig,
    auto_approve_all,
    auto_approve_safe_only,
)
from geoagent.core.config import _default_provider_from_env
from geoagent.core.safety import ConfirmCallback, ConfirmRequest
from renewable_site_agent.tools.site_selection_map import site_selection_map_tools

PROVIDER_NAMES: tuple[str, ...] = (
    "openai-codex",
    "openai",
    "anthropic",
    "gemini",
    "bedrock",
    "litellm",
    "openrouter",
    "ollama",
    "vllm",
)

DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "openai-codex": "",
    "openai": "",
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-3.1-pro-preview",
    "bedrock": "us.anthropic.claude-sonnet-4-6",
    "litellm": "",
    "openrouter": "deepseek/deepseek-chat",
    "ollama": "qwen3.5:4b",
    "vllm": "",
}

MAX_CONTEXT_MESSAGES = 12
MAX_CONTEXT_CHARS = 12000


def default_provider() -> str:
    """Return the provider id derived from environment for UI initialization.

    Mirrors :func:`geoagent.core.config._default_provider_from_env` so the UI
    starts on a provider whose credentials are already configured instead of
    forcing the user onto the package-wide default.
    """
    candidate = _default_provider_from_env()
    return candidate if candidate in PROVIDER_NAMES else PROVIDER_NAMES[0]


@dataclass
class UiMapBinding:
    """A map object and the GeoAgent factory needed to bind it."""

    map_obj: Any
    map_library: str
    factory: Callable[..., GeoAgent]


def default_model_for_provider(provider: str | None) -> str:
    """Return the UI model id, preferring environment configuration."""
    provider_id = str(provider or "")

    env_name_by_provider = {
        "openai-codex": "OPENAI_CODEX_MODEL",
        "openai": "OPENAI_MODEL",
        "litellm": "LITELLM_MODEL",
        "openrouter": "OPENROUTER_MODEL",
        "vllm": "VLLM_MODEL_ID",
    }

    env_name = env_name_by_provider.get(provider_id)
    if env_name:
        configured_model = os.getenv(env_name, "").strip()
        if configured_model:
            return configured_model

    return DEFAULT_MODEL_BY_PROVIDER.get(provider_id, "")


def confirmation_callback(auto_approve: bool) -> ConfirmCallback:
    """Return the confirmation policy selected in the UI."""
    return auto_approve_all if auto_approve else auto_approve_safe_only


def confirmation_preview(auto_approve: bool, tool_name: str = "preview") -> bool:
    """Return whether the current confirmation setting would approve a tool."""
    callback = confirmation_callback(auto_approve)
    return callback(ConfirmRequest(tool_name=tool_name, args={}))


def _map_from_anymap() -> UiMapBinding:
    """Create an anymap-backed UI binding."""
    import anymap
    from geoagent import for_anymap

    return UiMapBinding(
        map_obj=anymap.Map(),
        map_library="anymap",
        factory=for_anymap,
    )


def _map_from_leafmap() -> UiMapBinding:
    """Create a leafmap-backed UI binding."""
    import leafmap
    from geoagent import for_leafmap

    return UiMapBinding(
        map_obj=leafmap.Map(),
        map_library="leafmap",
        factory=for_leafmap,
    )


def create_ui_map_binding() -> UiMapBinding:
    """Create the first available web map binding, preferring anymap."""
    errors: list[str] = []
    for create in (_map_from_anymap, _map_from_leafmap):
        try:
            return create()
        except Exception as exc:
            errors.append(f"{create.__name__}: {exc}")
    details = "; ".join(errors) if errors else "no map libraries were tried"
    raise RuntimeError(
        "GeoAgent UI needs a web map package. Install `GeoAgent[anymap,ui]` "
        "or `GeoAgent[leafmap,ui]` and restart the UI. "
        f"Details: {details}"
    )


def create_bound_agent(
    binding: UiMapBinding,
    *,
    provider: str,
    model_id: str | None = None,
    fast: bool = False,
    auto_approve: bool = False,
) -> GeoAgent:
    """Create a map-bound GeoAgent from current UI controls."""
    model = (model_id or "").strip() or None
    config = GeoAgentConfig(
        provider=provider,  # type: ignore[arg-type]
        model=model,
    )
    return binding.factory(
        binding.map_obj,
        config=config,
        fast=bool(fast),
        extra_tools=site_selection_map_tools(binding.map_obj),
        confirm=confirmation_callback(bool(auto_approve)),
    )


def _compact_value(
    value: Any,
    *,
    per_string: int = 200,
    list_limit: int = 10,
    dict_limit: int = 20,
) -> Any:
    """Shrink a value structurally so its repr stays small.

    Truncates long strings, caps list and dict sizes, and recurses into nested
    containers so the final stringification cannot accidentally serialize a
    multi-megabyte tool-call payload.
    """
    if isinstance(value, str):
        if len(value) <= per_string:
            return value
        return value[:per_string] + "...[truncated]"
    if isinstance(value, dict):
        items = list(value.items())[:dict_limit]
        compact = {
            k: _compact_value(
                v,
                per_string=per_string,
                list_limit=list_limit,
                dict_limit=dict_limit,
            )
            for k, v in items
        }
        if len(value) > dict_limit:
            compact["..."] = f"+{len(value) - dict_limit} more keys"
        return compact
    if isinstance(value, (list, tuple)):
        head = [
            _compact_value(
                v,
                per_string=per_string,
                list_limit=list_limit,
                dict_limit=dict_limit,
            )
            for v in list(value)[:list_limit]
        ]
        if len(value) > list_limit:
            head.append(f"...[+{len(value) - list_limit} more]")
        return head
    return value


def compact_tool_call(call: dict[str, Any], *, max_chars: int = 1200) -> str:
    """Return a compact human-readable tool-call summary.

    Args and results are first compacted structurally so a single oversized
    payload cannot allocate a multi-megabyte intermediate string before the
    final character cap kicks in.
    """
    name = str(call.get("name") or "tool")
    args = call.get("args")
    result = call.get("result")
    exception = call.get("exception")
    parts = [name]
    if args:
        parts.append(f"args={_compact_value(args)}")
    if exception:
        parts.append(f"error={_compact_value(exception)}")
    elif result is not None:
        parts.append(f"result={_compact_value(result)}")
    text = " | ".join(parts)
    if len(text) > max_chars:
        return text[: max_chars - 16].rstrip() + " ... [truncated]"
    return text


def build_prompt_with_context(
    history: Sequence[dict[str, Any]],
    prompt: str,
    *,
    max_messages: int = MAX_CONTEXT_MESSAGES,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Prepend recent transcript so multi-turn requests retain context.

    Mirrors the QGIS chat dock so follow-up turns like "remove the layer you
    just added" can resolve against earlier messages even though each send
    creates a fresh agent in the web UI.
    """
    history_lines: list[str] = []
    for msg in list(history)[-max_messages:]:
        body = str(msg.get("text") or "").strip()
        if not body:
            continue
        role = "User" if msg.get("role") == "user" else "Assistant"
        history_lines.append(f"{role}: {body}")
    if not history_lines:
        return prompt
    transcript = "\n\n".join(history_lines)
    if len(transcript) > max_chars:
        transcript = "[Earlier history truncated]\n" + transcript[-max_chars:]
    return (
        "Use the recent conversation history for context. The current user "
        "request is the authoritative request to answer now.\n\n"
        f"Recent conversation:\n{transcript}\n\n"
        f"Current user request:\n{prompt}"
    )


def format_response_message(response: Any) -> dict[str, Any]:
    """构建面向用户的自然语言回复，工具详情单独保留在 Tool Calls 区域。"""
    success = bool(getattr(response, "success", True))
    answer_text = str(getattr(response, "answer_text", "") or "").strip()
    executed = list(getattr(response, "executed_tools", None) or [])
    cancelled = list(getattr(response, "cancelled_tools", None) or [])
    error_message = getattr(response, "error_message", None)

    if success:
        text = answer_text or "操作已经完成。"
    else:
        details: list[str] = []

        if cancelled:
            details.append("Cancelled tools: " + ", ".join(cancelled))

        if error_message:
            details.append(str(error_message))

        if answer_text and details:
            text = answer_text + "\n\n" + "\n".join(details)
        else:
            text = answer_text or "\n".join(details) or "操作未完成。"

    return {
        "role": "assistant",
        "text": text,
        "status": "ok" if success else "error",
        "executed_tools": executed,
        "cancelled_tools": cancelled,
    }

def dispatch_prompt(
    text: str,
    *,
    history: Sequence[dict[str, Any]],
    binding: "UiMapBinding | None",
    binding_error: str = "",
    provider: str,
    model_id: str | None,
    fast: bool,
    auto_approve: bool,
    create_agent: Callable[..., GeoAgent] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one UI send and return ``(next_history, tool_calls)``.

    Pure helper extracted from ``WorkspacePage`` so the send pipeline is
    unit-testable without a Solara render.
    """
    factory = create_agent or create_bound_agent
    next_history: list[dict[str, Any]] = [
        *history,
        {"role": "user", "text": text},
    ]
    try:
        if binding is None:
            raise RuntimeError(binding_error or "Map binding unavailable.")
        prompt = build_prompt_with_context(history, text)
        agent = factory(
            binding,
            provider=provider,
            model_id=model_id,
            fast=fast,
            auto_approve=auto_approve,
        )
        response = agent.chat(prompt)
        message = format_response_message(response)
        return [*next_history, message], list(
            getattr(response, "tool_calls", None) or []
        )
    except Exception as exc:
        return (
            [*next_history, {"role": "assistant", "text": str(exc), "status": "error"}],
            [],
        )


__all__ = [
    "DEFAULT_MODEL_BY_PROVIDER",
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_MESSAGES",
    "PROVIDER_NAMES",
    "UiMapBinding",
    "build_prompt_with_context",
    "compact_tool_call",
    "confirmation_callback",
    "confirmation_preview",
    "create_bound_agent",
    "create_ui_map_binding",
    "default_model_for_provider",
    "default_provider",
    "dispatch_prompt",
    "format_response_message",
]


# --- RenewableSite enterprise UI integration ---

def is_enterprise_site_selection_request(prompt: str) -> bool:
    """Use the canonical LLM-first router; keywords exist only inside its fallback."""
    text = str(prompt or "").strip()
    if not text:
        return False
    from renewable_site_agent.agent.routing import IntentRouter

    decision = IntentRouter().route(text)
    return decision.intent == "renewable_site_selection"


def _enterprise_api_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_sec: int = 900,
) -> dict[str, Any]:
    """Call the local enterprise API and return a JSON object."""
    import json
    import urllib.error
    import urllib.request

    body = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Enterprise API HTTP {exc.code}: {error_body[:1000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"无法连接企业 API，请确认 18080 服务正在运行：{exc}"
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("Enterprise API did not return a JSON object.")
    return result


def _run_enterprise_site_selection(
    prompt: str,
    *,
    timeout_sec: int = 900,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run request-driven GIS screening and retrieve map-ready GeoJSON."""
    import os
    import time
    import urllib.parse

    base_url = os.getenv(
        "GEOAGENT_ENTERPRISE_API_URL",
        "http://127.0.0.1:8000",
    ).rstrip("/")

    created = _enterprise_api_json(
        "POST",
        f"{base_url}/v1/site-selection/jobs",
        payload={"query": prompt},
        timeout_sec=30,
    )
    job_id = created.get("job_id")
    if not job_id:
        raise RuntimeError("选址 API 未返回 job_id。")
    if created.get("needs_clarification"):
        raise RuntimeError("请求需要补充信息：" + str(created.get("missing_fields")))

    encoded_job_id = urllib.parse.quote(str(job_id), safe="")
    deadline = time.monotonic() + timeout_sec
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = _enterprise_api_json(
            "GET", f"{base_url}/v1/site-selection/jobs/{encoded_job_id}", timeout_sec=30
        )
        if job.get("status") in {
            "SUCCEEDED", "NEEDS_HUMAN_REVIEW", "FAILED",
            "BUDGET_EXCEEDED", "LOOP_DETECTED",
        }:
            break
        time.sleep(1)
    if job.get("status") not in {"SUCCEEDED", "NEEDS_HUMAN_REVIEW"}:
        raise RuntimeError("选址 Job 未成功完成：" + str(job.get("error") or job.get("status")))
    result = _enterprise_api_json(
        "GET", f"{base_url}/v1/site-selection/jobs/{encoded_job_id}/result", timeout_sec=30
    )
    geojson = _enterprise_api_json(
        "GET", f"{base_url}/v1/site-selection/jobs/{encoded_job_id}/map-data", timeout_sec=30
    )
    coordinates = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry") or {}
        stack = [geometry.get("coordinates")]
        while stack:
            value = stack.pop()
            if isinstance(value, list) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
                coordinates.append(value[:2])
            elif isinstance(value, list):
                stack.extend(value)
    bbox = None
    center = None
    if coordinates:
        xs, ys = [value[0] for value in coordinates], [value[1] for value in coordinates]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        center = {"lng": (bbox[0] + bbox[2]) / 2, "lat": (bbox[1] + bbox[3]) / 2}
    analysis = {
        "service_status": "completed",
        "dynamic_job": {"job_id": job_id},
        "request_scope": {"parsed_request": job.get("request") or {}},
        "execution": {"final_candidate_count": len(geojson.get("features", []))},
        "result": result,
    }
    map_data = {
        "map_data_status": "ready", "job_id": job_id,
        "geojson": geojson, "candidate_count": len(geojson.get("features", [])),
        "feature_count": len(geojson.get("features", [])), "bbox": bbox, "center": center,
    }
    return analysis, map_data


def _fit_enterprise_map(
    map_obj: Any,
    *,
    bbox: Any,
    center: Any,
) -> str:
    """Move the web map to the returned candidate extent."""
    if isinstance(bbox, list) and len(bbox) == 4:
        west, south, east, north = [float(value) for value in bbox]
        fit_bounds = getattr(map_obj, "fit_bounds", None)

        if callable(fit_bounds):
            candidate_bounds = (
                [[west, south], [east, north]],
                [west, south, east, north],
                [[south, west], [north, east]],
            )
            for bounds in candidate_bounds:
                try:
                    fit_bounds(bounds)
                    return "fit_bounds"
                except Exception:
                    continue

        zoom_to_bounds = getattr(map_obj, "zoom_to_bounds", None)
        if callable(zoom_to_bounds):
            try:
                zoom_to_bounds(west, south, east, north)
                return "zoom_to_bounds"
            except Exception:
                pass

    if isinstance(center, dict):
        lng = center.get("lng")
        lat = center.get("lat")
        fly_to = getattr(map_obj, "fly_to", None)

        if callable(fly_to) and lng is not None and lat is not None:
            try:
                fly_to(lat=float(lat), lon=float(lng), zoom=8)
                return "fly_to"
            except Exception:
                pass

    return "map_view_unchanged"


def _render_enterprise_candidate_layer(
    binding: UiMapBinding,
    map_data: dict[str, Any],
) -> dict[str, Any]:
    """Render request-scoped candidate polygons on the current web map."""
    map_obj = binding.map_obj
    geojson = map_data.get("geojson")

    if not isinstance(geojson, dict):
        raise RuntimeError("map-data API did not return GeoJSON.")

    if geojson.get("type") != "FeatureCollection":
        raise RuntimeError("Candidate GeoJSON is not a FeatureCollection.")

    add_geojson = getattr(map_obj, "add_geojson", None)
    if not callable(add_geojson):
        raise RuntimeError(
            f"{type(map_obj).__name__} does not support add_geojson."
        )

    job_id = str(map_data.get("job_id") or "unknown-job")
    layer_name = f"request-candidates-{job_id[-8:]}"

    known_layers = set(
        getattr(map_obj, "_enterprise_candidate_layer_names", set())
    )
    layer_added = False

    style = map_data.get("recommended_layer_style") or {}
    paint = {
        "fill-color": style.get("fill_color", "#3b82f6"),
        "fill-opacity": float(style.get("fill_opacity", 0.35)),
        "fill-outline-color": style.get("stroke_color", "#2563eb"),
    }

    if layer_name not in known_layers:
        add_geojson(
            geojson,
            layer_type="fill",
            paint=paint,
            name=layer_name,
            fit_bounds=False,
            visible=True,
            opacity=1.0,
        )
        known_layers.add(layer_name)
        setattr(map_obj, "_enterprise_candidate_layer_names", known_layers)
        layer_added = True

    map_action = _fit_enterprise_map(
        map_obj,
        bbox=map_data.get("bbox"),
        center=map_data.get("center"),
    )

    return {
        "layer_name": layer_name,
        "layer_added": layer_added,
        "map_action": map_action,
        "candidate_count": map_data.get("candidate_count"),
        "feature_count": map_data.get("feature_count"),
        "bbox": map_data.get("bbox"),
        "center": map_data.get("center"),
    }



def _candidate_geometry_stats(geojson: Any) -> dict[str, int]:
    """统计候选区 GeoJSON 的要素、独立面和内部孔洞数量。"""
    feature_count = 0
    polygon_part_count = 0
    interior_ring_count = 0

    if not isinstance(geojson, dict):
        return {
            "feature_count": 0,
            "polygon_part_count": 0,
            "interior_ring_count": 0,
        }

    features = geojson.get("features")
    if not isinstance(features, list):
        return {
            "feature_count": 0,
            "polygon_part_count": 0,
            "interior_ring_count": 0,
        }

    feature_count = len(features)

    for feature in features:
        if not isinstance(feature, dict):
            continue

        geometry = feature.get("geometry") or {}
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates") or []

        if geometry_type == "Polygon":
            polygon_part_count += 1
            interior_ring_count += max(0, len(coordinates) - 1)

        elif geometry_type == "MultiPolygon":
            polygon_part_count += len(coordinates)
            for polygon in coordinates:
                if isinstance(polygon, list):
                    interior_ring_count += max(0, len(polygon) - 1)

    return {
        "feature_count": feature_count,
        "polygon_part_count": polygon_part_count,
        "interior_ring_count": interior_ring_count,
    }


def _constraint_descriptions(analysis: dict[str, Any]) -> list[str]:
    """Create concise Chinese descriptions of resolved request constraints."""
    scope = analysis.get("request_scope") or {}
    parsed = scope.get("parsed_request") or {}
    descriptions: list[str] = []

    if parsed.get("exclude_water") is True:
        descriptions.append("避开水体")

    if parsed.get("exclude_protected_areas") is True:
        descriptions.append("避开保护区")

    slope = parsed.get("max_slope_deg")
    if slope is not None:
        descriptions.append(f"坡度不超过 {slope:g} 度")

    capacity = parsed.get("capacity_mw")
    if capacity is not None:
        descriptions.append(f"装机容量 {capacity:g} MW")

    return descriptions


def dispatch_enterprise_site_selection_prompt(
    prompt: str,
    *,
    history: Sequence[dict[str, Any]],
    binding: UiMapBinding | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute the enterprise site-selection chain and update the web map."""
    updated_history = [
        *history,
        {
            "role": "user",
            "text": prompt,
            "status": "ok",
        },
    ]

    if binding is None:
        updated_history.append(
            {
                "role": "assistant",
                "text": "当前地图后端不可用，无法展示候选区。",
                "status": "error",
            }
        )
        return updated_history, []

    try:
        analysis, map_data = _run_enterprise_site_selection(prompt)
        render_result = _render_enterprise_candidate_layer(binding, map_data)

        scope = analysis.get("request_scope") or {}
        parsed = scope.get("parsed_request") or {}
        execution = analysis.get("execution") or {}

        project_type_zh = (
            parsed.get("project_type_zh")
            or parsed.get("project_type")
            or "新能源"
        )
        region = parsed.get("region") or "目标区域"
        constraints = _constraint_descriptions(analysis)
        constraints_text = "、".join(constraints) if constraints else "已识别约束"

        candidate_count = int(execution.get("candidate_count") or 0)

        geometry_stats = _candidate_geometry_stats(map_data.get("geojson"))
        feature_count = geometry_stats["feature_count"]
        polygon_part_count = geometry_stats["polygon_part_count"]
        interior_ring_count = geometry_stats["interior_ring_count"]

        if polygon_part_count == 1:
            result_summary = "系统生成了 **1 个连通候选区域**"
            if interior_ring_count > 0:
                result_summary += (
                    f"，内部包含 **{interior_ring_count} 个"
                    "被当前筛选规则排除的孔洞**"
                )
            result_summary += "。"
        elif polygon_part_count > 1:
            result_summary = (
                f"系统生成了 **{feature_count} 个候选要素**，"
                f"共包含 **{polygon_part_count} 个相互分离的候选片区**。"
            )
        else:
            result_summary = f"系统共得到 **{candidate_count} 个候选要素**。"
        job_id = (
            analysis.get("dynamic_job", {}).get("job_id")
            or map_data.get("job_id")
        )
        request_status = scope.get("request_status")
        out_of_scope = scope.get("requested_out_of_scope_constraints") or []

        answer_parts = [
            (
                f"已经完成本次{project_type_zh}选址分析。"
                f"系统在{region}范围内按照“{constraints_text}”执行了真实 GIS 筛选，"
                f"{result_summary}"
            ),
            "候选区已经以蓝色图层显示在地图上，地图也已自动移动到结果范围。",
            f"本次任务编号为 `{job_id}`。",
        ]

        if request_status == "partially_supported" or out_of_scope:
            names = []
            for item in out_of_scope:
                if isinstance(item, dict):
                    names.append(
                        str(
                            item.get("constraint_zh")
                            or item.get("constraint")
                            or item.get("name")
                            or "未接入约束"
                        )
                    )
                else:
                    names.append(str(item))
            if names:
                answer_parts.append(
                    "以下要求当前仅作范围记录，尚未参与 GIS 计算："
                    + "、".join(names)
                    + "。"
                )

        answer_parts.append(
            "这些区域属于辅助选址候选结果，仍需要结合业务数据和人工复核，"
            "不代表已经获得建设审批。"
        )

        updated_history.append(
            {
                "role": "assistant",
                "text": "\n\n".join(answer_parts),
                "status": "ok",
                "job_id": job_id,
                "candidate_count": candidate_count,
            }
        )

        tool_calls = [
            {
                "name": "enterprise_site_selection_api",
                "args": {"query": prompt},
                "result": {
                    "job_id": job_id,
                    "request_status": request_status,
                    "execution_status": execution.get(
                        "dynamic_execution_status"
                    ),
                    "candidate_count": candidate_count,
                },
            },
            {
                "name": "render_candidate_geojson",
                "args": {"job_id": job_id},
                "result": render_result,
            },
        ]

        return updated_history, tool_calls

    except Exception as exc:
        updated_history.append(
            {
                "role": "assistant",
                "text": (
                    "本次选址任务执行失败，没有生成或展示候选区。\n\n"
                    f"错误信息：`{type(exc).__name__}: {exc}`"
                ),
                "status": "error",
            }
        )
        return updated_history, [
            {
                "name": "enterprise_site_selection_api",
                "args": {"query": prompt},
                "exception": f"{type(exc).__name__}: {exc}",
            }
        ]
