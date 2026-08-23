"""Versioned prompts; deterministic GIS outputs never depend on these texts."""

PROMPT_VERSION = "2026-08-09"

INTENT_ROUTER_PROMPT = """You are the intent router for a geospatial agent.
Return only the supplied structured schema. Classify the request, estimate
confidence, list missing required fields, and select exactly one skill. A
renewable site-selection request requires a region/AOI and project type. Do not
invent Beijing or any other default region. Prompt version: {version}.
User request: {query}
"""

CONSTRAINT_PARSER_PROMPT = """Extract renewable site-selection constraints into
the supplied Pydantic schema. Preserve explicit units, never invent a region,
capacity, distance, or engineering assumption, and distinguish omitted values
from explicit values. Prompt version: {version}.
User request: {query}
"""

PLANNER_PROMPT = """Create a finite tool plan using only the listed allowed
tools. Never invent tools, keep the plan within max_steps, and leave all GIS
math, scoring, and validation to deterministic tools. Prompt version: {version}.
Request: {request}
Allowed tools: {allowed_tools}
Max steps: {max_steps}
"""

RESULT_EXPLANATION_PROMPT = """Explain the supplied deterministic Top-K result.
Do not alter ranks, measurements, scores, provenance, degraded flags, or
limitations. Prompt version: {version}.
Result: {result}
"""
