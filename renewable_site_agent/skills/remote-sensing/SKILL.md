# Remote Sensing

Use for current-request Sentinel-2 search and candidate verification. Require a
candidate/AOI, date range, and cloud threshold. Search Copernicus Data Space
STAC first, read only needed bands/windows, produce small chips, calculate
deterministic evidence, then optionally invoke the configured VLM. Never reuse
a historical global report as the current job result. VLM unavailability is a
degraded optional capability unless the request explicitly requires it.
