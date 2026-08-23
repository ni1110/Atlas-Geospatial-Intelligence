"""Official Strands MCPClient over a local stdio FastMCP server."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from renewable_site_agent.agent.skills import SkillRegistry
from renewable_site_agent.config import PATHS
from renewable_site_agent.schemas import ErrorCode, ToolResult


class SkillScopedMCPClient:
    def __init__(self, skill: str = "site-selection") -> None:
        self.skill = skill
        self.allowed = list(SkillRegistry().allowed_tools(skill))
        self._client: Any = None

    def _build(self) -> Any:
        from mcp import StdioServerParameters, stdio_client
        from strands.tools.mcp import MCPClient

        environment = dict(os.environ)
        environment.update(PATHS.process_environment())
        root_text = str(PATHS.project_root)
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            root_text if not existing_pythonpath else root_text + os.pathsep + existing_pythonpath
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "renewable_site_agent.mcp.server"],
            cwd=str(PATHS.project_root),
            env=environment,
        )
        return MCPClient(
            lambda: stdio_client(parameters),
            startup_timeout=30,
            tool_filters={"allowed": self.allowed},
        )

    def __enter__(self) -> "SkillScopedMCPClient":
        self._client = self._build()
        self._client.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._client is not None:
            self._client.__exit__(exc_type, exc, traceback)
            self._client = None

    @property
    def strands_provider(self) -> Any:
        """Return the live official Strands MCPClient for Agent construction."""
        if self._client is None:
            raise RuntimeError("MCP client must be used inside a context manager")
        return self._client

    def list_tool_names(self) -> list[str]:
        if self._client is None:
            raise RuntimeError("MCP client must be used inside a context manager")
        tools = self._client.list_tools_sync(tool_filters={"allowed": self.allowed})
        return [str(getattr(tool, "tool_name", getattr(tool, "name", ""))) for tool in tools]

    def call(self, name: str, arguments: dict[str, Any], timeout_seconds: float = 60) -> ToolResult:
        if name not in self.allowed:
            return ToolResult.fail(ErrorCode.INVALID_ARGUMENT, f"Tool not allowed for {self.skill}: {name}")
        if self._client is None:
            raise RuntimeError("MCP client must be used inside a context manager")
        result = self._client.call_tool_sync(
            tool_use_id=f"mcp-{uuid4()}",
            name=name,
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        )
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        if structured is None:
            structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = (
                result.get("structured_content")
                if isinstance(result, dict)
                else getattr(result, "structured_content", None)
            )
        if isinstance(structured, dict):
            return ToolResult.model_validate(structured)
        content = result.get("content", []) if isinstance(result, dict) else getattr(result, "content", [])
        for item in reversed(list(content or [])):
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if isinstance(text, str):
                try:
                    return ToolResult.model_validate(json.loads(text))
                except Exception:
                    continue
        return ToolResult.fail(
            ErrorCode.INVALID_OUTPUT,
            "MCP tool did not return the standardized ToolResult contract",
            metadata={"raw_type": type(result).__name__},
        )
