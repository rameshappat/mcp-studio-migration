"""Mermaid MCP Client for rendering Mermaid diagrams via a local MCP server (stdio).

This client is intended to work with the `mcp-mermaid` npm package.
"""

import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class MermaidMCPClient:
    """Client for interacting with a Mermaid MCP server over stdio."""

    def __init__(
        self,
        command: str = "node",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = command

        # mcp-mermaid (and some stdio MCP servers) may print startup logs to stdout.
        # The MCP stdio protocol requires stdout to be JSON-RPC only, otherwise the
        # client will emit noisy parse errors.
        wrapper = str(
            Path(__file__).resolve().parents[2] / "scripts" / "mcp_mermaid_stdio_wrapper.mjs"
        )
        self.args = args or [wrapper]
        self.env = env or {**os.environ}
        self._tools: list[dict[str, Any]] = []

    def _get_server_params(self) -> StdioServerParameters:
        return StdioServerParameters(command=self.command, args=self.args, env=self.env)

    @asynccontextmanager
    async def _get_session(self):
        server_params = self._get_server_params()
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools:
            return self._tools

        async with self._get_session() as session:
            tools_result = await session.list_tools()
            self._tools = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                }
                for tool in tools_result.tools
            ]
            return self._tools

    def get_tool_names(self) -> list[str]:
        return [t["name"] for t in self._tools]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self._get_session() as session:
            result = await session.call_tool(tool_name, arguments)

            if result.content:
                content = result.content[0]
                if hasattr(content, "text"):
                    try:
                        return json.loads(content.text)
                    except Exception:
                        return {"text": content.text}

            return result

    async def render_mermaid_to_file(
        self,
        mermaid: str,
        output_path: str,
        theme: str | None = None,
        background_color: str | None = None,
    ) -> dict[str, Any]:
        """Render Mermaid diagram and write it to output_path.

        This tries to be compatible with the common `generate_mermaid_diagram` schema:
        {"mermaid": "...", "outputType": "file", "theme": "...", "backgroundColor": "..."}
        """

        tools = await self.list_tools()
        tool_name = _pick_mermaid_generate_tool(tools)
        if not tool_name:
            raise RuntimeError(
                "No Mermaid render tool found on Mermaid MCP server. "
                "Expected a tool named like 'generate_mermaid_diagram'."
            )

        requested_path = Path(output_path)
        requested_path.parent.mkdir(parents=True, exist_ok=True)

        args: dict[str, Any] = {
            "mermaid": mermaid,
            "outputType": "file",
        }
        if theme:
            args["theme"] = theme
        if background_color:
            args["backgroundColor"] = background_color

        # Many servers accept `outputType=file` and return the file path;
        # some accept/require `outputFile`. We support both.
        args["outputFile"] = str(requested_path)

        result = await self.call_tool(tool_name, args)

        actual_path = _extract_output_file_path(result)
        copied = False
        if actual_path:
            actual = Path(actual_path)
            if not actual.is_absolute():
                actual = (Path.cwd() / actual).resolve()

            try:
                # If the server ignored outputFile and wrote elsewhere, copy into the requested location.
                if actual.exists() and actual.resolve() != requested_path.resolve():
                    shutil.copyfile(actual, requested_path)
                    copied = True
            except Exception as e:
                logger.warning(f"Failed to relocate Mermaid output from {actual} to {requested_path}: {e}")

        return {
            "tool": tool_name,
            "requested_output": str(requested_path),
            "actual_output": actual_path,
            "copied_to_requested": copied,
            "result": result,
        }


def _extract_output_file_path(result: Any) -> str | None:
    """Best-effort extraction of an output file path from MCP tool result."""
    if isinstance(result, dict):
        for key in ("outputFile", "file", "filePath", "path"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        text = result.get("text")
        if isinstance(text, str):
            # Common format: "saved to file: /path/to/file.png"
            match = re.search(r"saved to file:\s*(.+)$", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(1).strip().strip("\"'")

            # Sometimes it's just a path.
            if text.strip().endswith((".png", ".svg")):
                return text.strip().strip("\"'")

    if isinstance(result, str):
        candidate = result.strip()
        if candidate:
            match = re.search(r"saved to file:\s*(.+)$", candidate, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                return match.group(1).strip().strip("\"'")
            if candidate.endswith((".png", ".svg")):
                return candidate.strip("\"'")

    return None


def _pick_mermaid_generate_tool(tools: list[dict[str, Any]]) -> str | None:
    """Pick a likely diagram generation tool name."""
    names = [t.get("name", "") for t in tools]

    # Best guess: exact match
    for preferred in (
        "generate_mermaid_diagram",
        "mermaid_generate_mermaid_diagram",
        "mcp_mermaid_generate_mermaid_diagram",
    ):
        if preferred in names:
            return preferred

    # Heuristic: contains both words
    lowered = [(n, n.lower()) for n in names]
    for n, low in lowered:
        if "mermaid" in low and "generate" in low:
            return n

    return None
