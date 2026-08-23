# Site Selection

Use this skill for solar, wind, storage, or hybrid site-screening requests with
an explicit region/AOI and project type. It resolves real source data, applies
hard spatial constraints, creates valid candidate polygons, computes distances
and deterministic scores, validates evidence, and returns Top-K GeoJSON/JSON.

Inputs must pass `SiteSelectionRequest`. Only the tool names exposed by the
site-selection registry are allowed. The final output must preserve units,
degraded-source flags, evidence completeness, and provenance. LLM output may
route, plan, and explain; it must never calculate GIS geometry or candidate
scores. Do not call generic browser, shell, image-generation, destructive, or
unrelated Earth-observation tools.
