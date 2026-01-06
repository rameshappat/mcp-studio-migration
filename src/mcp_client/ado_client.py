"""Azure DevOps MCP Client for connecting to the Azure DevOps MCP Server via stdio.

Includes a small REST fallback for Test Plan creation when the MCP server's
create-plan tool is unable to pass the project name correctly.
"""

import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class AzureDevOpsMCPClient:
    """Client for interacting with Azure DevOps via MCP Server (stdio).
    
    Uses the @azure-devops/mcp npm package via stdio transport.
    """

    def __init__(
        self,
        organization: str,
        project: str | None = None,
        domains: list[str] | None = None,
        auth_type: str = "interactive",
    ):
        """Initialize the Azure DevOps MCP Client.

        Args:
            organization: Azure DevOps organization name.
            project: Azure DevOps project name (optional).
            domains: List of domains to enable (e.g., ["core", "work", "work-items"]).
            auth_type: Authentication type ("interactive", "azcli", "env", "envvar").
        """
        self.organization = (organization or "").strip()
        self.project = (project or "").strip() or None
        self.domains = domains or ["core", "work", "work-items", "test-plans"]
        self.auth_type = auth_type
        self._tools: list[dict] = []
        self._connected = False
        self._pat: str = ""
        
        # Map various PAT env var names to ADO_MCP_AUTH_TOKEN for the MCP server
        pat = (
            os.environ.get("ADO_MCP_AUTH_TOKEN")
            or os.environ.get("AZURE_DEVOPS_EXT_PAT")
            or os.environ.get("AZURE_DEVOPS_PAT")
            or os.environ.get("AZURE_DEVOPS_TOKEN", "")
        )
        if pat:
            self._pat = pat
            os.environ["ADO_MCP_AUTH_TOKEN"] = pat
            # Auto-switch to envvar auth if PAT is available and auth_type is default
            if self.auth_type == "interactive":
                self.auth_type = "envvar"

    def _get_server_params(self) -> StdioServerParameters:
        """Get stdio server parameters for the ADO MCP server."""
        args = ["-y", "@azure-devops/mcp", self.organization, "-a", self.auth_type, "-d"] + self.domains
        
        # Build environment with PAT for envvar auth
        env = {**os.environ}
        if self._pat:
            env["ADO_MCP_AUTH_TOKEN"] = self._pat
        
        return StdioServerParameters(
            command="npx",
            args=args,
            env=env,
        )

    @asynccontextmanager
    async def _get_session(self):
        """Get an MCP session as an async context manager."""
        server_params = self._get_server_params()
        
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def connect(self) -> None:
        """Test connection to the Azure DevOps MCP server and list tools."""
        logger.info(f"Connecting to Azure DevOps MCP server for org: {self.organization}")

        try:
            async with self._get_session() as session:
                # List available tools
                tools_result = await session.list_tools()
                self._tools = [
                    {"name": tool.name, "description": tool.description, "inputSchema": tool.inputSchema}
                    for tool in tools_result.tools
                ]
                self._connected = True
                
                logger.info(f"Connected! Found {len(self._tools)} ADO tools")
                print(f"✅ Connected to Azure DevOps MCP ({self.organization}). Found {len(self._tools)} tools.")
        except Exception as e:
            logger.error(f"Failed to connect to Azure DevOps MCP: {e}")
            self._connected = False
            raise

    async def close(self) -> None:
        """Close the connection (no-op for session-per-call model)."""
        self._connected = False
        logger.info("Azure DevOps MCP connection closed")

    def get_tools(self) -> list[dict]:
        """Get list of available tools."""
        return self._tools

    def get_tool_names(self) -> list[str]:
        """Get list of available tool names."""
        return [tool["name"] for tool in self._tools]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on the Azure DevOps MCP server.

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.

        Returns:
            The result from the tool execution.
        """
        # Add project to arguments if not present and we have one
        if "project" not in arguments and self.project:
            arguments["project"] = self.project

        logger.info(f"Calling ADO tool: {tool_name}")

        async with self._get_session() as session:
            result = await session.call_tool(tool_name, arguments)
            
            # Parse the result content
            if result.content:
                content = result.content[0]
                if hasattr(content, 'text'):
                    try:
                        return json.loads(content.text)
                    except json.JSONDecodeError:
                        return {"text": content.text}
            
            return result

    async def _call_first_available_tool(
        self, tool_names: list[str], arguments: dict[str, Any]
    ) -> Any:
        """Call the first tool name that succeeds.

        The @azure-devops/mcp package has used different naming conventions across versions.
        We keep small fallbacks to avoid breaking callers.
        """
        last_error: Exception | None = None
        for name in tool_names:
            try:
                return await self.call_tool(name, dict(arguments))
            except Exception as e:
                last_error = e
                continue
        raise RuntimeError(
            f"None of the candidate ADO tools worked: {tool_names}. Last error: {last_error}"
        )

    async def create_test_plan(
        self,
        name: str,
        iteration: str,
        description: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        area_path: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Create an Azure DevOps Test Plan.

        Note: Test Plan support depends on the enabled MCP domains (include 'test-plans').
        """

        args: dict[str, Any] = {
            "project": project or self.project,
            "name": name,
            "iteration": iteration,
        }
        if description:
            args["description"] = description
        if start_date:
            args["startDate"] = start_date
        if end_date:
            args["endDate"] = end_date
        if area_path:
            args["areaPath"] = area_path

        # Common tool names seen in the wild.
        # Your repo already calls work-item tools as 'wit_create_work_item', so we prefer that style first.
        result = await self._call_first_available_tool(
            [
                "testplan_create_test_plan",
                "testplan_create_testplan",
                "mcp_ado_testplan_create_test_plan",
            ],
            args,
        )

        # Known issue: some versions of @azure-devops/mcp incorrectly pass an empty
        # project name to the underlying ADO command, yielding TF200001.
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            text = result["text"]
            text_lower = text.lower()
            if "tf200001" in text_lower and "projectname" in text_lower and "empty" in text_lower:
                logger.warning(
                    "MCP test plan create returned TF200001 (empty projectName); falling back to REST API."
                )
                return await self._create_test_plan_via_rest(
                    name=name,
                    iteration=iteration,
                    description=description,
                    start_date=start_date,
                    end_date=end_date,
                    area_path=area_path,
                    project=project or self.project,
                )

        return result

    async def _create_test_plan_via_rest(
        self,
        name: str,
        iteration: str,
        description: str | None,
        start_date: str | None,
        end_date: str | None,
        area_path: str | None,
        project: str | None,
    ) -> dict[str, Any]:
        project = (project or "").strip()
        if not project:
            raise ValueError("Azure DevOps project is required to create a Test Plan")
        if not self._pat:
            raise RuntimeError(
                "ADO PAT not available for REST fallback. Set ADO_MCP_AUTH_TOKEN (or AZURE_DEVOPS_EXT_PAT)."
            )

        # ADO uses Basic auth with PAT as the password.
        token = base64.b64encode(f":{self._pat}".encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "name": name,
            "iteration": iteration,
        }
        if description:
            payload["description"] = description
        if start_date:
            payload["startDate"] = start_date
        if end_date:
            payload["endDate"] = end_date
        if area_path:
            payload["areaPath"] = area_path

        # API versions vary by tenant; try a couple.
        api_versions = ["7.1-preview.1", "7.0", "6.0"]
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for api_version in api_versions:
                url = (
                    f"https://dev.azure.com/{self.organization}/{project}"
                    f"/_apis/testplan/plans?api-version={api_version}"
                )
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code >= 400:
                        last_error = f"HTTP {resp.status_code}: {resp.text}"
                        continue
                    return resp.json()
                except Exception as e:
                    last_error = str(e)
                    continue

        # Keep the pipeline best-effort: return an error-shaped payload instead of
        # raising, so callers can display actionable output.
        return {"text": f"REST fallback failed to create Test Plan: {last_error}"}

    async def create_test_suite(
        self,
        plan_id: int,
        parent_suite_id: int,
        name: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Create a test suite under a test plan."""
        args: dict[str, Any] = {
            "project": project or self.project,
            "planId": plan_id,
            "parentSuiteId": parent_suite_id,
            "name": name,
        }
        return await self._call_first_available_tool(
            [
                "testplan_create_test_suite",
                "mcp_ado_testplan_create_test_suite",
            ],
            args,
        )

    async def create_test_case(
        self,
        title: str,
        steps: str | None = None,
        priority: int | None = None,
        area_path: str | None = None,
        iteration_path: str | None = None,
        tests_work_item_id: int | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Create a test case work item (test management domain)."""

        # ADO APIs typically expect area/iteration paths like:
        #   testingmcp\\Sprint 1
        # while other parts of the pipeline may normalize them with a leading "\\".
        # Strip leading backslashes so we don't end up with invalid paths.
        def _sanitize_ado_path_for_api(value: str | None) -> str | None:
            if value is None:
                return None
            s = str(value).strip().replace("/", "\\")
            if not s:
                return None
            s = s.lstrip("\\")

            # In some ADO APIs/tooling, classification paths may be shown as
            #   <project>\\Iteration\\Sprint 1
            # but the Test Plans tooling expects
            #   <project>\\Sprint 1
            # Normalize that form to avoid silent failures.
            project_name = (project or self.project or "").strip()
            if project_name:
                parts = s.split("\\")
                if len(parts) >= 3 and parts[0].lower() == project_name.lower() and parts[1].lower() == "iteration":
                    s = "\\".join([parts[0], *parts[2:]])

            return s

        area_path_api = _sanitize_ado_path_for_api(area_path)
        iteration_path_api = _sanitize_ado_path_for_api(iteration_path)

        def _looks_like_error_text(value: object) -> bool:
            if not isinstance(value, str):
                return False
            s = value.lower()
            return any(
                token in s
                for token in (
                    "tf",
                    "argumentexception",
                    "not authorized",
                    "unauthorized",
                    "forbidden",
                    "error",
                    "exception",
                )
            )

        def _has_any_id(obj: object) -> bool:
            if obj is None:
                return False
            if isinstance(obj, int):
                return True
            if isinstance(obj, dict):
                for k in ("id", "workItemId", "testCaseId"):
                    v = obj.get(k)
                    if isinstance(v, int):
                        return True
                    if isinstance(v, str) and v.isdigit():
                        return True
                for v in obj.values():
                    if _has_any_id(v):
                        return True
            if isinstance(obj, list):
                return any(_has_any_id(x) for x in obj)
            return False

        async def _create_test_case_via_wit() -> dict[str, Any]:
            # Best-effort fallback: create a Test Case work item via the Work Items domain.
            # This avoids the flaky/broken testplan create-test-case tool in some tenants.
            fields: list[dict[str, Any]] = [
                {"name": "System.Title", "value": title},
            ]
            # Put steps into description so the test case is still useful even if we can't
            # populate the native Steps field.
            if steps:
                safe_steps = str(steps).replace("<", "&lt;").replace(">", "&gt;")
                fields.append(
                    {
                        "name": "System.Description",
                        "value": f"<pre>{safe_steps}</pre>",
                        "format": "Html",
                    }
                )
            if priority is not None:
                # The MCP schema for wit_create_work_item expects field values to be strings.
                fields.append({"name": "Microsoft.VSTS.Common.Priority", "value": str(int(priority))})
            if area_path_api:
                fields.append({"name": "System.AreaPath", "value": area_path_api})
            if iteration_path_api:
                fields.append({"name": "System.IterationPath", "value": iteration_path_api})

            return await self._call_first_available_tool(
                [
                    "wit_create_work_item",
                    "mcp_ado_wit_create_work_item",
                ],
                {
                    "project": project or self.project,
                    "workItemType": "Test Case",
                    "fields": fields,
                },
            )

        args: dict[str, Any] = {
            "project": project or self.project,
            "title": title,
        }
        if steps:
            args["steps"] = steps
        if priority is not None:
            args["priority"] = priority
        if area_path_api:
            args["areaPath"] = area_path_api
        if iteration_path_api:
            args["iterationPath"] = iteration_path_api
        if tests_work_item_id is not None:
            args["testsWorkItemId"] = tests_work_item_id

        result = await self._call_first_available_tool(
            [
                "testplan_create_test_case",
                "mcp_ado_testplan_create_test_case",
            ],
            args,
        )

        # If the tool returns non-JSON text or an unexpected shape with no id, fall back.
        if isinstance(result, dict):
            text = result.get("text")
            if _looks_like_error_text(text) or not _has_any_id(result):
                logger.warning(
                    "Test Case creation via testplan tool did not return an id; falling back to wit_create_work_item."
                )
                return await _create_test_case_via_wit()

        return result

    async def update_test_case_steps(self, test_case_id: int, steps: str) -> dict[str, Any]:
        """Update steps of an existing test case work item."""
        return await self._call_first_available_tool(
            [
                "testplan_update_test_case_steps",
                "mcp_ado_testplan_update_test_case_steps",
            ],
            {"id": test_case_id, "steps": steps},
        )

    async def add_test_cases_to_suite(
        self,
        plan_id: int,
        suite_id: int,
        test_case_ids: list[int],
        project: str | None = None,
    ) -> dict[str, Any]:
        """Add existing test case work items to a suite."""
        # MCP schema allows string or array of strings.
        ids_as_str = [str(i) for i in test_case_ids]
        args: dict[str, Any] = {
            "project": project or self.project,
            "planId": plan_id,
            "suiteId": suite_id,
            "testCaseIds": ids_as_str,
        }
        return await self._call_first_available_tool(
            [
                "testplan_add_test_cases_to_suite",
                "mcp_ado_testplan_add_test_cases_to_suite",
            ],
            args,
        )

    async def create_work_item(
        self,
        work_item_type: str,
        title: str,
        description: str = "",
        project: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a work item in Azure DevOps.

        Args:
            work_item_type: Type of work item (Epic, Feature, User Story, Task, Bug).
            title: Title of the work item.
            description: Description of the work item.
            project: Project name (uses default if not specified).
            **kwargs: Additional fields.

        Returns:
            Created work item details.
        """
        return await self.call_tool(
            "mcp_ado_wit_create_work_item",
            {
                "project": project or self.project,
                "type": work_item_type,
                "title": title,
                "description": description,
                **kwargs,
            },
        )

    async def update_work_item(
        self,
        work_item_id: int,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update a work item in Azure DevOps.

        Args:
            work_item_id: ID of the work item to update.
            **fields: Fields to update.

        Returns:
            Updated work item details.
        """
        return await self.call_tool(
            "mcp_ado_wit_update_work_item",
            {
                "workItemId": work_item_id,
                **fields,
            },
        )

    async def get_work_item(self, work_item_id: int) -> dict[str, Any]:
        """Get a work item from Azure DevOps.

        Args:
            work_item_id: ID of the work item.

        Returns:
            Work item details.
        """
        return await self.call_tool(
            "mcp_ado_wit_get_work_item",
            {"workItemId": work_item_id},
        )

    async def query_work_items(self, wiql: str) -> list[dict[str, Any]]:
        """Query work items using WIQL.

        Args:
            wiql: Work Item Query Language query.

        Returns:
            List of matching work items.
        """
        return await self.call_tool(
            "mcp_ado_wit_query",
            {"query": wiql},
        )

    async def add_work_item_link(
        self,
        source_id: int,
        target_id: int,
        link_type: str = "System.LinkTypes.Hierarchy-Forward",
    ) -> dict[str, Any]:
        """Add a link between work items.

        Args:
            source_id: Source work item ID.
            target_id: Target work item ID.
            link_type: Type of link.

        Returns:
            Link creation result.
        """
        return await self.call_tool(
            "mcp_ado_wit_add_link",
            {
                "workItemId": source_id,
                "targetId": target_id,
                "linkType": link_type,
            },
        )

    async def create_epic(
        self,
        title: str,
        description: str,
        acceptance_criteria: list[str] | None = None,
        business_value: str = "",
    ) -> dict[str, Any]:
        """Create an Epic in Azure DevOps.

        Args:
            title: Epic title.
            description: Epic description.
            acceptance_criteria: List of acceptance criteria.
            business_value: Business value statement.

        Returns:
            Created Epic details.
        """
        full_description = description
        if acceptance_criteria:
            full_description += "\n\n**Acceptance Criteria:**\n"
            full_description += "\n".join(f"- {ac}" for ac in acceptance_criteria)
        if business_value:
            full_description += f"\n\n**Business Value:**\n{business_value}"

        return await self.create_work_item("Epic", title, full_description)

    async def create_user_story(
        self,
        title: str,
        description: str,
        acceptance_criteria: list[str] | None = None,
        story_points: int | None = None,
        priority: int = 2,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a User Story in Azure DevOps.

        Args:
            title: Story title (preferably in "As a... I want... So that..." format).
            description: Story description.
            acceptance_criteria: Acceptance criteria in Gherkin format.
            story_points: Story points estimate.
            priority: Priority (1=highest, 4=lowest).
            parent_id: Parent Epic ID.

        Returns:
            Created User Story details.
        """
        full_description = description
        if acceptance_criteria:
            full_description += "\n\n**Acceptance Criteria:**\n"
            full_description += "\n".join(f"- {ac}" for ac in acceptance_criteria)

        kwargs = {"priority": priority}
        if story_points:
            kwargs["storyPoints"] = story_points

        result = await self.create_work_item("User Story", title, full_description, **kwargs)

        # Link to parent Epic if provided
        if parent_id and "id" in result:
            await self.add_work_item_link(parent_id, result["id"])

        return result

    async def create_task(
        self,
        title: str,
        description: str,
        remaining_work: float | None = None,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a Task in Azure DevOps.

        Args:
            title: Task title.
            description: Task description.
            remaining_work: Estimated hours.
            parent_id: Parent Story ID.

        Returns:
            Created Task details.
        """
        kwargs = {}
        if remaining_work:
            kwargs["remainingWork"] = remaining_work

        result = await self.create_work_item("Task", title, description, **kwargs)

        # Link to parent Story if provided
        if parent_id and "id" in result:
            await self.add_work_item_link(parent_id, result["id"])

        return result

    def get_tool_names(self) -> list[str]:
        """Get list of available tool names."""
        return [tool.get("name", "") for tool in self._tools]
