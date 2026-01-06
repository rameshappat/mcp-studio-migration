"""GitHub MCP Client for connecting to an MCP server over StreamableHTTP."""

import logging
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool as MCPTool

logger = logging.getLogger(__name__)


class GitHubMCPClient:
    """Client for interacting with a GitHub MCP server over HTTP.

    This matches the interface expected by:
    - tests in tests/test_mcp_client.py
    - the LangGraph GitHub agent in src/agents/github_agent.py
    """

    def __init__(
        self,
        mcp_url: str,
        github_token: str | None = None,
    ):
        self.mcp_url = (mcp_url or "").rstrip("/")
        self.github_token = github_token
        self._tools: list[MCPTool] = []
        self._connected = False

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    async def _with_session(self):
        # Kept as a helper so list_tools/call_tool share the same setup.
        http_client = httpx.AsyncClient(headers=self._get_headers())
        return streamable_http_client(self.mcp_url, http_client=http_client)

    async def connect(self) -> None:
        """Validate connectivity by listing tools."""
        await self.list_tools()
        self._connected = True
        print(f"✅ Connected to GitHub MCP. Found {len(self._tools)} tools.")

    async def close(self) -> None:
        """Close connection (no-op; sessions are per-call)."""
        self._connected = False

    async def list_tools(self) -> list[MCPTool]:
        """List tools available from the MCP server."""
        # If tools are already loaded (e.g., tests inject mocks), return cached.
        if self._tools:
            return self._tools

        logger.info("Listing tools from GitHub MCP server")
        async with (await self._with_session()) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                self._tools = list(tools_result.tools)
                return self._tools

    def get_tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    def get_tool_by_name(self, name: str) -> MCPTool | None:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke an MCP tool by name."""
        logger.info(f"Calling GitHub MCP tool: {tool_name}")
        async with (await self._with_session()) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)

                # Most MCP servers return JSON in a text payload; return raw if not parseable.
                if result.content:
                    texts: list[str] = []
                    for item in result.content:
                        if hasattr(item, "text") and isinstance(item.text, str):
                            texts.append(item.text)
                    if texts:
                        text = "\n".join(texts)
                        try:
                            import json

                            return json.loads(text)
                        except Exception:
                            return {"text": text}

                return result
