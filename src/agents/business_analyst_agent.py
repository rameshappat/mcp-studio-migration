"""Business Analyst Agent - Creates Epics and User Stories for Azure DevOps."""

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .base_agent import (
    AgentContext,
    AgentMessage,
    AgentRole,
    ApprovalStatus,
    BaseAgent,
)

logger = logging.getLogger(__name__)


class BusinessAnalystAgent(BaseAgent):
    """Agent that acts as a Business Analyst/Product Owner, creating Epics and Stories."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        model_name: str = "gpt-4-turbo",
        temperature: float = 0.6,
        ado_client: Any = None,
    ):
        """Initialize the Business Analyst Agent."""
        super().__init__(
            role=AgentRole.BUSINESS_ANALYST,
            llm=llm,
            model_name=model_name,
            temperature=temperature,
        )
        self._ado_client = ado_client

    @property
    def system_prompt(self) -> str:
        """Return the system prompt for the Business Analyst."""
        return """You are a Principal Business Analyst / Product Owner for an enterprise engineering organization.

Primary objective:
- Convert high-level product requirements into an execution-ready backlog with clear scope, traceability, and testable acceptance criteria.

Grounding rules:
- Do NOT invent external facts. If important details are missing, write explicit assumptions and open questions.
- Ensure every Epic/Story is traceable back to one or more Requirement IDs (REQ-xxx).
- Prefer smaller, independently deliverable stories (thin vertical slices).

Azure DevOps note:
- Different process templates use different work item names (Basic: Epic/Issue/Task; Agile: Epic/User Story/Task; Scrum: Epic/Product Backlog Item/Task). You must still output using the generic keys "epics", "stories", "tasks"; downstream mapping can adapt.

Output rules:
- Output ONLY valid JSON. No markdown, no prose.
- IDs must be consistent and stable: EPIC-001, STORY-001, TASK-001.
- Use Fibonacci story points: 1,2,3,5,8,13.

Required JSON shape (you may add additional fields, but keep these keys):
{
    "assumptions": ["..."],
    "open_questions": ["..."],
    "epics": [
        {
            "id": "EPIC-001",
            "title": "...",
            "description": "...",
            "business_value": "...",
            "mapped_requirements": ["REQ-001"],
            "acceptance_criteria": ["Given... When... Then..."],
            "definition_of_done": ["..."],
            "non_functional": {"security": "...", "performance": "...", "availability": "...", "observability": "..."},
            "risks": ["..."],
            "dependencies": ["..."],
            "stories": ["STORY-001"]
        }
    ],
    "stories": [
        {
            "id": "STORY-001",
            "epic_id": "EPIC-001",
            "mapped_requirements": ["REQ-001"],
            "title": "As a [user], I want [capability] so that [benefit]",
            "description": "...",
            "acceptance_criteria": ["Given... When... Then..."],
            "story_points": 5,
            "priority": 1,
            "tags": ["api", "frontend"],
            "dependencies": ["..."],
            "test_notes": ["unit", "integration"],
            "telemetry": ["key events/metrics"],
            "security_notes": ["authz", "input validation"],
            "tasks": ["TASK-001"]
        }
    ],
    "tasks": [
        {
            "id": "TASK-001",
            "story_id": "STORY-001",
            "title": "...",
            "description": "...",
            "estimated_hours": 4
        }
    ]
}

Quality bar:
- Backlog should be implementable by an engineering team without guesswork.
- Acceptance criteria must be testable and unambiguous.
- Ensure non-functional coverage: security, compliance posture (without making legal claims), operability, and observability.

DEMO CONSTRAINTS (for faster execution):
- Limit to a MAXIMUM of 3-5 Epics total.
- Each Epic should have 2-3 User Stories maximum.
- Each Story should have 1-2 Tasks maximum.
- Keep acceptance criteria to 2-3 items per story.
- Focus on the most impactful features rather than exhaustive coverage.
"""

    def set_ado_client(self, ado_client: Any) -> None:
        """Set the Azure DevOps MCP client."""
        self._ado_client = ado_client

    async def _process_response(
        self, response: AIMessage, context: AgentContext
    ) -> AgentMessage:
        """Process the LLM response into Epics and Stories."""
        content = str(response.content)

        artifacts = {}
        try:
            # Extract JSON from response
            if "```json" in content:
                json_str = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                json_str = content.split("```")[1].split("```")[0]
            else:
                json_str = content

            work_items = json.loads(json_str)
            artifacts["work_items"] = work_items

            # Update context
            if "epics" in work_items:
                context.epics = work_items["epics"]
            if "stories" in work_items:
                context.stories = work_items["stories"]

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse structured output: {e}")
            artifacts["raw_work_items"] = content

        return AgentMessage(
            from_agent=self.role,
            to_agent=AgentRole.ARCHITECT,
            content=content,
            artifacts=artifacts,
            requires_approval=True,
            approval_status=ApprovalStatus.PENDING,
            metadata={"stage": "work_item_creation"},
        )

    async def create_work_items(
        self,
        context: AgentContext,
        requirements_message: AgentMessage,
    ) -> AgentMessage:
        """Create Epics and Stories from requirements.

        Args:
            context: The shared workflow context.
            requirements_message: Message from Product Manager with requirements.

        Returns:
            Agent message with created work items.
        """
        requirements = requirements_message.artifacts.get("requirements", {})

        input_message = AgentMessage(
            from_agent=AgentRole.PRODUCT_MANAGER,
            to_agent=self.role,
            content=f"""Based on the following requirements, create Epics and User Stories 
for Azure DevOps Boards.

Goal:
- Produce a demo-ready, execution-ready backlog (enterprise-grade) with enough detail to start building immediately.
- Include enabling work: CI/CD, observability, security, data model, and operational readiness.

Requirements:
{json.dumps(requirements, indent=2)}

Create detailed work items following Agile best practices.
Ensure proper hierarchy: Epic -> Story -> Task.
Include acceptance criteria in Gherkin format.
Ensure every Epic and Story maps to one or more REQ-xxx ids.
Include explicit assumptions and open questions when details are missing.""",
        )

        return await self.process(input_message, context)

    async def push_to_azure_devops(
        self,
        context: AgentContext,
    ) -> dict[str, Any]:
        """Push work items to Azure DevOps using MCP.

        Args:
            context: The shared workflow context.

        Returns:
            Dictionary with created work item IDs.
        """
        if not self._ado_client:
            logger.warning("Azure DevOps client not configured")
            return {"error": "ADO client not configured"}

        created_items = {"epics": [], "stories": [], "tasks": []}
        project = self._ado_client.project

        # Get epics and stories from context, with fallback to work_items dict
        epics = context.epics
        stories = context.stories
        
        # Fallback: if context.epics/stories are empty but work_items exists, extract from there
        if not epics and hasattr(context, 'work_items') and isinstance(context.work_items, dict):
            epics = context.work_items.get("epics", [])
            logger.info(f"Using epics from context.work_items: {len(epics)} found")
        if not stories and hasattr(context, 'work_items') and isinstance(context.work_items, dict):
            stories = context.work_items.get("stories", [])
            logger.info(f"Using stories from context.work_items: {len(stories)} found")
        
        if not epics and not stories:
            logger.warning("No epics or stories found in context to push to Azure DevOps")
            return {"error": "No work items found in context. Please ensure work items were created successfully."}

        try:
            # Create Epics using correct tool name and parameter format
            for epic in epics:
                fields = [
                    {"name": "System.Title", "value": epic["title"]},
                    {"name": "System.Description", "value": epic.get("description", ""), "format": "Html"},
                ]
                if epic.get("tags"):
                    fields.append({"name": "System.Tags", "value": ",".join(epic.get("tags", []))})
                
                result = await self._ado_client.call_tool(
                    "wit_create_work_item",
                    {
                        "project": project,
                        "workItemType": "Epic",
                        "fields": fields,
                    },
                )
                created_items["epics"].append({
                    "local_id": epic["id"],
                    "ado_id": result.get("id") if isinstance(result, dict) else None,
                    "result": result,
                })
                logger.info(f"Created Epic: {epic['title']} - Result: {result}")

            # Create Stories using correct tool name and parameter format
            # Note: Azure DevOps Basic process uses "Issue" instead of "User Story"
            # Agile process uses "User Story", Scrum uses "Product Backlog Item"
            story_type = "Issue"  # Basic process template
            for story in stories:
                acceptance_criteria = "\n".join(
                    f"- {ac}" for ac in story.get("acceptance_criteria", [])
                )
                description = f"{story.get('description', '')}\n\n<b>Acceptance Criteria:</b>\n{acceptance_criteria}"
                
                fields = [
                    {"name": "System.Title", "value": story["title"]},
                    {"name": "System.Description", "value": description, "format": "Html"},
                ]
                # Story points - Effort field for Basic process
                if story.get("story_points"):
                    fields.append({"name": "Microsoft.VSTS.Scheduling.Effort", "value": str(story.get("story_points", 0))})
                
                result = await self._ado_client.call_tool(
                    "wit_create_work_item",
                    {
                        "project": project,
                        "workItemType": story_type,
                        "fields": fields,
                    },
                )
                created_items["stories"].append({
                    "local_id": story["id"],
                    "ado_id": result.get("id") if isinstance(result, dict) else None,
                    "result": result,
                })
                logger.info(f"Created Story: {story['title']} - Result: {result}")

            context.ado_work_items = created_items
            return created_items

        except Exception as e:
            logger.error(f"Error pushing to Azure DevOps: {e}")
            return {"error": str(e)}

    async def refine_work_items(
        self,
        context: AgentContext,
        feedback: str,
    ) -> AgentMessage:
        """Refine work items based on feedback.

        Args:
            context: The shared workflow context.
            feedback: Feedback from human or other agents.

        Returns:
            Agent message with refined work items.
        """
        context.human_feedback.append(feedback)

        input_message = AgentMessage(
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=self.role,
            content=f"""Refine the work items based on the following feedback:

Feedback: {feedback}

Current Epics: {json.dumps(context.epics, indent=2)}
Current Stories: {json.dumps(context.stories, indent=2)}

Provide updated work items addressing the feedback.""",
        )

        return await self.process(input_message, context)
