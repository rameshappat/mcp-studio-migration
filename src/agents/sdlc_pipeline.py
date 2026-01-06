"""SDLC Pipeline Orchestrator - Multi-agent software development lifecycle pipeline."""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from langchain_openai import ChatOpenAI
from langsmith import traceable

from src.agents.architect_agent import ArchitectAgent
from src.agents.base_agent import AgentContext, AgentMessage, AgentRole, ApprovalStatus
from src.agents.business_analyst_agent import BusinessAnalystAgent
from src.agents.developer_agent import DeveloperAgent
from src.agents.human_in_loop import HumanInTheLoop
from src.agents.product_manager_agent import ProductManagerAgent
from src.mcp_client.ado_client import AzureDevOpsMCPClient
from src.mcp_client.github_client import GitHubMCPClient

logger = logging.getLogger(__name__)


def _flatten_iteration_paths(nodes: object) -> list[str]:
    paths: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, dict):
            p = n.get("path")
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
            children = n.get("children")
            if isinstance(children, list):
                for c in children:
                    walk(c)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(nodes)
    # keep stable order, remove duplicates
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def _normalize_ado_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return p
    p = p.replace("/", "\\")
    while "\\\\" in p:
        p = p.replace("\\\\", "\\")
    if not p.startswith("\\"):
        p = "\\" + p
    return p


def _looks_like_ado_error_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "tf" in t and any(ch.isdigit() for ch in t):
        return True
    if t.startswith("error") or "exception" in t:
        return True
    return False


class PipelineStage(Enum):
    """SDLC Pipeline stages."""

    INITIALIZATION = "initialization"
    REQUIREMENTS = "requirements"
    REQUIREMENTS_APPROVAL = "requirements_approval"
    WORK_ITEMS = "work_items"
    WORK_ITEMS_APPROVAL = "work_items_approval"
    ADO_PUSH = "ado_push"
    TEST_PLAN = "test_plan"
    ARCHITECTURE = "architecture"
    ARCHITECTURE_APPROVAL = "architecture_approval"
    MERMAID_RENDER = "mermaid_render"
    DEVELOPMENT = "development"
    DEVELOPMENT_APPROVAL = "development_approval"
    GITHUB_PUSH = "github_push"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PipelineState:
    """State of the SDLC pipeline."""

    stage: PipelineStage = PipelineStage.INITIALIZATION
    context: AgentContext = field(default_factory=AgentContext)
    messages: list[AgentMessage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    revision_count: dict[str, int] = field(default_factory=dict)
    max_revisions: int = 3

    def add_message(self, message: AgentMessage) -> None:
        """Add a message to the pipeline state."""
        self.messages.append(message)

    def get_last_message_from(self, agent: AgentRole) -> AgentMessage | None:
        """Get the last message from a specific agent."""
        for msg in reversed(self.messages):
            if msg.from_agent == agent:
                return msg
        return None


class SDLCPipelineOrchestrator:
    """Orchestrator for the multi-agent SDLC pipeline."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        github_client: GitHubMCPClient | None = None,
        ado_client: AzureDevOpsMCPClient | None = None,
        human_in_loop: HumanInTheLoop | None = None,
        interactive: bool = True,
    ):
        """Initialize the SDLC Pipeline Orchestrator.

        Args:
            llm: ChatOpenAI instance for agents.
            github_client: GitHub MCP client.
            ado_client: Azure DevOps MCP client.
            human_in_loop: Human-in-the-loop handler.
            interactive: Whether to prompt for human input.
        """
        # If a shared LLM is provided, all agents will use it.
        # Otherwise, each agent will instantiate its own LLM (enables per-role model selection via env vars).
        self.llm = llm
        self.github_client = github_client
        self.ado_client = ado_client
        self.hitl = human_in_loop or HumanInTheLoop(interactive=interactive)

        # Initialize agents
        if self.llm is not None:
            self.product_manager = ProductManagerAgent(llm=self.llm)
            self.business_analyst = BusinessAnalystAgent(llm=self.llm, ado_client=ado_client)
            self.architect = ArchitectAgent(llm=self.llm)
            self.developer = DeveloperAgent(llm=self.llm, github_client=github_client)
        else:
            self.product_manager = ProductManagerAgent()
            self.business_analyst = BusinessAnalystAgent(ado_client=ado_client)
            self.architect = ArchitectAgent()
            self.developer = DeveloperAgent(github_client=github_client)

        self.state = PipelineState()

    @traceable(name="sdlc_pipeline_run")
    async def run(
        self,
        project_idea: str,
        project_name: str | None = None,
    ) -> PipelineState:
        """Run the complete SDLC pipeline.

        Args:
            project_idea: High-level project idea or description.
            project_name: Optional project name.

        Returns:
            Final pipeline state.
        """
        logger.info(f"Starting SDLC Pipeline for: {project_idea[:100]}...")
        self.hitl.notify(f"Starting SDLC Pipeline for: {project_name or 'New Project'}", "info")

        try:
            # Initialize context
            self.state.context.project_name = project_name or "new-project"
            self.state.stage = PipelineStage.INITIALIZATION

            # Stage 1: Generate Requirements
            await self._run_requirements_stage(project_idea)

            # Stage 2: Create Work Items
            await self._run_work_items_stage()

            # Stage 3: Push to Azure DevOps
            await self._run_ado_push_stage()

            # Stage 3b: Create Azure Test Plan (optional)
            await self._run_test_plan_stage()

            # Stage 4: Architecture Design
            await self._run_architecture_stage()

            # Stage 4b: Render Mermaid diagrams (optional)
            await self._run_mermaid_render_stage()

            # Stage 5: Development
            await self._run_development_stage()

            # Stage 6: Push to GitHub
            await self._run_github_push_stage()

            # Complete
            self.state.stage = PipelineStage.COMPLETED
            self.state.completed_at = datetime.now()
            self.hitl.notify("SDLC Pipeline completed successfully!", "success")

        except Exception as e:
            logger.error(f"Pipeline failed: {e}")
            self.state.stage = PipelineStage.FAILED
            self.state.errors.append(str(e))
            self.hitl.notify(f"Pipeline failed: {e}", "error")

        return self.state

    @traceable(name="requirements_stage")
    async def _run_requirements_stage(self, project_idea: str) -> None:
        """Run the requirements generation stage."""
        self.state.stage = PipelineStage.REQUIREMENTS
        self.hitl.display_progress("Requirements", 1, 6, "Generating requirements...")

        # Set project description from initial idea
        self.state.context.project_description = project_idea

        while True:
            # Generate requirements
            message = await self.product_manager.generate_requirements(
                self.state.context,
                domain=None,  # Could be extracted from project_idea
            )
            self.state.add_message(message)

            # Store requirements in context
            if "requirements" in message.artifacts:
                self.state.context.requirements = message.artifacts["requirements"]

            # Human approval
            self.state.stage = PipelineStage.REQUIREMENTS_APPROVAL
            approval = self.hitl.request_approval(message)
            message.approval_status = approval

            if approval == ApprovalStatus.APPROVED:
                break
            elif approval == ApprovalStatus.REJECTED:
                raise Exception("Requirements rejected by user")
            elif approval == ApprovalStatus.REVISION_REQUESTED:
                self._increment_revision("requirements")
                feedback = self.hitl.request_feedback(
                    "What changes would you like to the requirements?"
                )
                project_idea = f"{project_idea}\n\nUser Feedback: {feedback}"

        self.hitl.notify("Requirements approved!", "success")

    @traceable(name="work_items_stage")
    async def _run_work_items_stage(self) -> None:
        """Run the work items creation stage."""
        self.state.stage = PipelineStage.WORK_ITEMS
        self.hitl.display_progress("Work Items", 2, 6, "Creating Epics and Stories...")

        # Get the requirements message from PM
        requirements_message = self.state.get_last_message_from(AgentRole.PRODUCT_MANAGER)
        if not requirements_message:
            raise Exception("No requirements message found from Product Manager")

        while True:
            # Create work items
            message = await self.business_analyst.create_work_items(
                self.state.context,
                requirements_message,
            )
            self.state.add_message(message)

            # Store work items in context
            if "work_items" in message.artifacts:
                work_items = message.artifacts["work_items"]
                if "epics" in work_items:
                    self.state.context.epics = work_items["epics"]
                if "stories" in work_items:
                    self.state.context.stories = work_items["stories"]
            if "epics" in message.artifacts:
                self.state.context.epics = message.artifacts["epics"]
            if "stories" in message.artifacts:
                self.state.context.stories = message.artifacts["stories"]

            # Human approval
            self.state.stage = PipelineStage.WORK_ITEMS_APPROVAL
            approval = self.hitl.request_approval(message)
            message.approval_status = approval

            if approval == ApprovalStatus.APPROVED:
                break
            elif approval == ApprovalStatus.REJECTED:
                raise Exception("Work items rejected by user")
            elif approval == ApprovalStatus.REVISION_REQUESTED:
                self._increment_revision("work_items")
                feedback = self.hitl.request_feedback(
                    "What changes would you like to the Epics/Stories?"
                )
                message = await self.business_analyst.refine_work_items(
                    self.state.context, feedback
                )
                self.state.add_message(message)

        self.hitl.notify("Work items approved!", "success")

    @traceable(name="ado_push_stage")
    async def _run_ado_push_stage(self) -> None:
        """Push work items to Azure DevOps."""
        self.state.stage = PipelineStage.ADO_PUSH
        self.hitl.display_progress("Azure DevOps", 3, 6, "Pushing to ADO...")

        if not self.ado_client:
            self.hitl.notify("Azure DevOps client not configured, skipping push", "warning")
            return

        # Confirm before pushing
        if not self.hitl.request_confirmation("Push work items to Azure DevOps?"):
            self.hitl.notify("Azure DevOps push skipped", "info")
            return

        try:
            push_message = await self.business_analyst.push_to_azure_devops(
                self.state.context
            )
            self.state.add_message(push_message)

            if "ado_work_items" in push_message.artifacts:
                self.state.context.ado_work_items = push_message.artifacts["ado_work_items"]

            self.hitl.notify("Work items pushed to Azure DevOps!", "success")
        except Exception as e:
            logger.error(f"Failed to push to ADO: {e}")
            self.hitl.notify(f"ADO push failed: {e}", "warning")

    @staticmethod
    def _extract_int_id(value: Any, keys: tuple[str, ...]) -> int | None:
        import json
        import re

        def _from_text(text: str) -> int | None:
            s = (text or "").strip()
            if not s:
                return None

            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    parsed = json.loads(s)
                    return SDLCPipelineOrchestrator._extract_int_id(parsed, keys)
                except Exception:
                    pass

            # Prefer explicit key matches.
            for key in keys:
                m = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', s)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        return None

            for key in keys:
                m = re.search(rf'\b{re.escape(key)}\b\s*[:=]\s*(\d+)', s)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        return None

            return None

        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return _from_text(value)
        if isinstance(value, list):
            for item in value:
                found = SDLCPipelineOrchestrator._extract_int_id(item, keys)
                if found:
                    return found
            return None
        if isinstance(value, dict):
            for k in keys:
                v = value.get(k)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)

            text = value.get("text")
            if isinstance(text, str):
                found = _from_text(text)
                if found:
                    return found

            for v in value.values():
                found = SDLCPipelineOrchestrator._extract_int_id(v, keys)
                if found:
                    return found

        return None

    @staticmethod
    def _story_to_test_steps(story: dict[str, Any]) -> str:
        """Convert a BA story into Azure Test Plans step format."""

        def _clean(s: str) -> str:
            # '|' is a reserved delimiter in ADO steps.
            return (s or "").replace("|", "/").strip()

        ac = story.get("acceptance_criteria") or []
        if not isinstance(ac, list):
            ac = [str(ac)]

        lines: list[str] = []
        idx = 1
        for item in ac:
            item_s = _clean(str(item))
            if not item_s:
                continue
            # Use the acceptance criterion as the action and expected outcome.
            lines.append(f"{idx}. {item_s}|{item_s}")
            idx += 1

        if not lines:
            title = _clean(str(story.get("title") or "the feature"))
            lines.append(f"1. Verify {title} works end-to-end|{title} behaves as specified")

        return "\n".join(lines)

    @traceable(name="test_plan_stage")
    async def _run_test_plan_stage(self) -> None:
        """Create an Azure DevOps Test Plan (optional).

        This is intentionally lightweight: it creates the Test Plan container.
        Creating suites/cases depends on additional MCP tools and project settings.
        """
        self.state.stage = PipelineStage.TEST_PLAN

        if not self.ado_client:
            self.hitl.notify("Azure DevOps client not configured, skipping Test Plan", "warning")
            return

        if not self.hitl.request_confirmation("Create an Azure DevOps Test Plan?", default=False):
            self.hitl.notify("Test Plan creation skipped", "info")
            return

        plan_name = self.hitl.request_feedback(
            "Enter Test Plan name (leave blank for default):"
        ).strip()
        if not plan_name:
            plan_name = f"{self.state.context.project_name} - Test Plan"

        iteration = _normalize_ado_path(
            self.hitl.request_feedback(
                "Enter iteration path (e.g., 'Project\\Iteration 1' or 'Project\\Sprint 1'):"
            ).strip()
        )
        if not iteration:
            self.hitl.notify("No iteration provided; skipping Test Plan creation", "warning")
            return

        # Validate the iteration path against this project to avoid opaque null responses.
        iteration_paths: list[str] = []
        try:
            iters = await self.ado_client.call_tool(
                "work_list_iterations",
                {"project": self.ado_client.project, "depth": 10},
            )
            iteration_paths = [_normalize_ado_path(p) for p in _flatten_iteration_paths(iters)]
        except Exception:
            iteration_paths = []

        if iteration_paths and iteration not in iteration_paths:
            self.hitl.notify(
                "Iteration path does not match this project; skipping Test Plan creation",
                "warning",
            )
            # Provide a few valid options.
            examples = "\n".join(f"- {p}" for p in iteration_paths[:10])
            self.hitl.notify(f"Valid iteration paths (examples):\n{examples}", "info")
            return

        description = self.hitl.request_feedback(
            "Optional description (press Enter to skip):"
        ).strip()

        try:
            result = await self.ado_client.create_test_plan(
                name=plan_name,
                iteration=iteration,
                description=description or None,
            )
            # Store on context for later visibility
            self.state.context.__dict__["ado_test_plan"] = result

            if result is None:
                self.hitl.notify(
                    "Test Plan creation returned null. This usually means the iteration path is invalid for the project or the API call failed.",
                    "warning",
                )
                if iteration_paths:
                    examples = "\n".join(f"- {p}" for p in iteration_paths[:10])
                    self.hitl.notify(f"Valid iteration paths (examples):\n{examples}", "info")
                return
            # Detect common auth failures so we don't report false success.
            if isinstance(result, dict) and isinstance(result.get("text"), str):
                if _looks_like_ado_error_text(result["text"]):
                    self.hitl.notify(f"Test Plan creation failed: {result['text']}", "warning")
                    return
                text_lower = result["text"].lower()
                if "not authorized" in text_lower or "unauthorized" in text_lower:
                    self.hitl.notify(f"Test Plan creation failed (permissions): {result['text']}", "warning")
                    return

            self.hitl.notify(f"Test Plan created: {result}", "success")

            # Best-effort: create a suite and populate it with test cases based on BA stories.
            plan_id = self._extract_int_id(result, keys=("id", "planId"))
            if not plan_id:
                # Fallback: list plans and find by name.
                try:
                    plans = await self.ado_client.call_tool(
                        "testplan_list_test_plans",
                        {
                            "project": self.ado_client.project,
                            "filterActivePlans": True,
                            "includePlanDetails": True,
                        },
                    )
                    if isinstance(plans, list):
                        for p in plans:
                            if not isinstance(p, dict):
                                continue
                            if p.get("name") == plan_name and isinstance(p.get("id"), int):
                                plan_id = p["id"]
                                break
                            plan_obj = p.get("plan")
                            if isinstance(plan_obj, dict) and plan_obj.get("name") == plan_name and isinstance(plan_obj.get("id"), int):
                                plan_id = plan_obj["id"]
                                break
                except Exception as e:
                    logger.warning(f"Could not look up plan id via listing: {e}")

            if not plan_id:
                self.hitl.notify(
                    "Could not determine Test Plan ID from response; skipping suite/test case creation",
                    "warning",
                )
                if isinstance(result, dict) and isinstance(result.get("text"), str):
                    self.hitl.notify(f"Test Plan create response: {result['text']}", "info")
                return

            suite_name = f"{self.state.context.project_name} - MVP Regression"
            # Many ADO setups use the root suite id equal to the plan id; if not, this will fail and we warn.
            parent_suite_id = plan_id
            suite = await self.ado_client.create_test_suite(
                plan_id=plan_id,
                parent_suite_id=parent_suite_id,
                name=suite_name,
            )
            suite_id = self._extract_int_id(suite, keys=("id", "suiteId"))
            if not suite_id:
                self.hitl.notify(
                    f"Created suite but could not read suite id; response: {suite}",
                    "warning",
                )
                return

            stories = list(getattr(self.state.context, "stories", []) or [])
            created_case_ids: list[int] = []
            for story in stories:
                title = str(story.get("title") or story.get("id") or "Story")
                story_id = str(story.get("id") or "")
                tc_title = f"{story_id}: {title}" if story_id else title
                steps = self._story_to_test_steps(story)
                try:
                    tc = await self.ado_client.create_test_case(
                        title=tc_title,
                        steps=steps,
                        priority=int(story.get("priority") or 2),
                        iteration_path=iteration,
                    )
                    tc_id = self._extract_int_id(tc, keys=("id", "workItemId"))
                    if tc_id:
                        created_case_ids.append(tc_id)
                except Exception as e:
                    logger.warning(f"Failed to create test case for {story_id}: {e}")

            if created_case_ids:
                try:
                    await self.ado_client.add_test_cases_to_suite(
                        plan_id=plan_id,
                        suite_id=suite_id,
                        test_case_ids=created_case_ids,
                    )
                    self.hitl.notify(
                        f"Added {len(created_case_ids)} test case(s) to suite '{suite_name}'",
                        "success",
                    )
                except Exception as e:
                    self.hitl.notify(
                        f"Created test cases but failed to add to suite (check permissions): {e}",
                        "warning",
                    )
        except Exception as e:
            logger.error(f"Failed to create Test Plan: {e}")
            self.hitl.notify(f"Test Plan creation failed: {e}", "warning")

    @traceable(name="architecture_stage")
    async def _run_architecture_stage(self) -> None:
        """Run the architecture design stage."""
        self.state.stage = PipelineStage.ARCHITECTURE
        self.hitl.display_progress("Architecture", 4, 6, "Creating architecture...")

        # Get the work items message from BA
        work_items_message = self.state.get_last_message_from(AgentRole.BUSINESS_ANALYST)
        if not work_items_message:
            raise Exception("No work items message found from Business Analyst")

        while True:
            # Create architecture
            message = await self.architect.create_architecture(
                self.state.context,
                work_items_message,
            )
            self.state.add_message(message)

            # Store architecture in context
            if "architecture" in message.artifacts:
                self.state.context.architecture = message.artifacts["architecture"]

            # Human approval
            self.state.stage = PipelineStage.ARCHITECTURE_APPROVAL
            approval = self.hitl.request_approval(message)
            message.approval_status = approval

            if approval == ApprovalStatus.APPROVED:
                break
            elif approval == ApprovalStatus.REJECTED:
                raise Exception("Architecture rejected by user")
            elif approval == ApprovalStatus.REVISION_REQUESTED:
                self._increment_revision("architecture")
                feedback = self.hitl.request_feedback(
                    "What changes would you like to the architecture?"
                )
                message = await self.architect.refine_architecture(
                    self.state.context, feedback
                )
                self.state.add_message(message)

        self.hitl.notify("Architecture approved!", "success")

    @traceable(name="mermaid_render_stage")
    async def _run_mermaid_render_stage(self) -> None:
        """Render Mermaid diagrams to image files via local Mermaid MCP (optional)."""
        self.state.stage = PipelineStage.MERMAID_RENDER

        if not self.hitl.request_confirmation(
            "Render Mermaid diagrams to image files via Mermaid MCP (local)?",
            default=False,
        ):
            return

        arch = self.state.context.architecture or {}
        diagrams = arch.get("diagrams") or {}
        if not diagrams:
            self.hitl.notify("No diagrams found to render", "info")
            return

        output_dir = (os.getenv("SDLC_MERMAID_OUTPUT_DIR") or "docs/diagrams").strip() or "docs/diagrams"

        try:
            from src.mcp_client import MermaidMCPClient
        except Exception as e:
            self.hitl.notify(f"Mermaid MCP client unavailable: {e}", "warning")
            return

        client = MermaidMCPClient()
        rendered = 0
        for key, value in diagrams.items():
            if not isinstance(value, str):
                continue
            out_path = os.path.join(output_dir, f"{key}.png")
            try:
                await asyncio.wait_for(
                    client.render_mermaid_to_file(value, out_path),
                    timeout=30,
                )
                rendered += 1
            except Exception as e:
                logger.warning(f"Failed to render diagram {key}: {e}")

        self.hitl.notify(f"Rendered {rendered} Mermaid diagram(s) into {output_dir}/", "success")

    @traceable(name="development_stage")
    async def _run_development_stage(self) -> None:
        """Run the development stage."""
        self.state.stage = PipelineStage.DEVELOPMENT
        self.hitl.display_progress("Development", 5, 6, "Generating code...")

        # Get the architecture message
        architecture_message = self.state.get_last_message_from(AgentRole.ARCHITECT)
        if not architecture_message:
            raise Exception("No architecture message found from Architect")

        while True:
            # Generate code
            message = await self.developer.generate_code(
                self.state.context,
                architecture_message,
            )
            self.state.add_message(message)

            # Store code in context
            if "code" in message.artifacts:
                self.state.context.code_artifacts = message.artifacts["code"]

            # Human approval
            self.state.stage = PipelineStage.DEVELOPMENT_APPROVAL
            approval = self.hitl.request_approval(message)
            message.approval_status = approval

            if approval == ApprovalStatus.APPROVED:
                break
            elif approval == ApprovalStatus.REJECTED:
                raise Exception("Code rejected by user")
            elif approval == ApprovalStatus.REVISION_REQUESTED:
                self._increment_revision("development")
                feedback = self.hitl.request_feedback(
                    "What changes would you like to the code?"
                )
                # Re-generate with feedback
                self.state.context.requirements["revision_feedback"] = feedback
                continue

        self.hitl.notify("Code approved!", "success")

    @traceable(name="github_push_stage")
    async def _run_github_push_stage(self) -> None:
        """Push code to GitHub."""
        self.state.stage = PipelineStage.GITHUB_PUSH
        self.hitl.display_progress("GitHub", 6, 6, "Pushing to GitHub...")

        if not self.github_client:
            self.hitl.notify("GitHub client not configured, skipping push", "warning")
            return

        # Confirm before pushing
        if not self.hitl.request_confirmation("Push code to GitHub and create PR?"):
            self.hitl.notify("GitHub push skipped", "info")
            return

        try:
            # Push code
            repo_name = self.hitl.request_feedback(
                "Enter GitHub repository name (owner/repo):"
            )
            if not repo_name:
                repo_name = f"user/{self.state.context.project_name}"

            push_message = await self.developer.push_to_github(
                self.state.context,
                repo_name=repo_name,
            )
            self.state.add_message(push_message)

            if "github_commits" in push_message.artifacts:
                self.state.context.github_commits = push_message.artifacts["github_commits"]

            # Create PR
            pr_message = await self.developer.create_pull_request(
                self.state.context,
                repo_name=repo_name,
                title=f"feat: {self.state.context.project_name} implementation",
                description=self._generate_pr_description(),
            )
            self.state.add_message(pr_message)

            self.hitl.notify("Code pushed to GitHub with PR created!", "success")
        except Exception as e:
            logger.error(f"Failed to push to GitHub: {e}")
            self.hitl.notify(f"GitHub push failed: {e}", "warning")

    def _increment_revision(self, stage: str) -> None:
        """Increment revision count for a stage."""
        current = self.state.revision_count.get(stage, 0)
        if current >= self.state.max_revisions:
            raise Exception(f"Maximum revisions ({self.state.max_revisions}) exceeded for {stage}")
        self.state.revision_count[stage] = current + 1

    def _generate_pr_description(self) -> str:
        """Generate a PR description from the context."""
        description = f"# {self.state.context.project_name}\n\n"

        if self.state.context.requirements:
            description += "## Requirements\n"
            if "product_vision" in self.state.context.requirements:
                description += f"{self.state.context.requirements['product_vision']}\n\n"

        if self.state.context.epics:
            description += "## Epics\n"
            for epic in self.state.context.epics[:3]:  # Limit to first 3
                description += f"- {epic.get('title', 'Untitled')}\n"
            description += "\n"

        if self.state.context.architecture:
            description += "## Architecture\n"
            arch = self.state.context.architecture
            if "decisions" in arch:
                for decision in arch["decisions"][:3]:
                    description += f"- {decision.get('title', 'Untitled')}\n"
            description += "\n"

        description += "---\n*Generated by SDLC Pipeline with AI Agents*\n"
        return description

    def get_pipeline_summary(self) -> dict[str, Any]:
        """Get a summary of the pipeline execution."""
        return {
            "project_name": self.state.context.project_name,
            "stage": self.state.stage.value,
            "messages_count": len(self.state.messages),
            "errors": self.state.errors,
            "revisions": self.state.revision_count,
            "started_at": self.state.started_at.isoformat(),
            "completed_at": (
                self.state.completed_at.isoformat()
                if self.state.completed_at
                else None
            ),
            "duration_seconds": (
                (self.state.completed_at - self.state.started_at).total_seconds()
                if self.state.completed_at
                else None
            ),
            "has_requirements": bool(self.state.context.requirements),
            "has_epics": bool(self.state.context.epics),
            "has_architecture": bool(self.state.context.architecture),
            "has_code": bool(self.state.context.code_artifacts),
        }


async def run_sdlc_demo():
    """Run a demo of the SDLC pipeline."""
    print("\n" + "=" * 60)
    print("SDLC Multi-Agent Pipeline Demo")
    print("=" * 60 + "\n")

    # Create orchestrator in non-interactive mode for demo
    orchestrator = SDLCPipelineOrchestrator(
        human_in_loop=HumanInTheLoop(interactive=False, auto_approve=True),
    )

    # Run with a sample project idea
    project_idea = """
    Create a task management application with the following features:
    - User authentication and authorization
    - Create, read, update, delete tasks
    - Task categories and tags
    - Due date reminders
    - Team collaboration features
    - Dashboard with task analytics
    """

    state = await orchestrator.run(
        project_idea=project_idea,
        project_name="task-manager-app",
    )

    # Print summary
    summary = orchestrator.get_pipeline_summary()
    print("\n" + "=" * 60)
    print("Pipeline Summary")
    print("=" * 60)
    for key, value in summary.items():
        print(f"  {key}: {value}")

    return state


if __name__ == "__main__":
    asyncio.run(run_sdlc_demo())
