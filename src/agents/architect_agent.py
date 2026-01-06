"""Architect Agent - Creates architecture and design artifacts."""

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


class ArchitectAgent(BaseAgent):
    """Agent that acts as a Software Architect, creating design artifacts."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        model_name: str = "o1-preview",
        temperature: float = 0.5,
    ):
        """Initialize the Architect Agent."""
        super().__init__(
            role=AgentRole.ARCHITECT,
            llm=llm,
            model_name=model_name,
            temperature=temperature,
        )

    @property
    def system_prompt(self) -> str:
        """Return the system prompt for the Architect."""
        return """You are a Principal Software Architect responsible for production-grade architecture and design.

Primary objective:
- Produce an implementable architecture that is secure-by-default, observable, and operationally viable.

Grounding rules:
- Do NOT invent external facts (vendor pricing, regulatory claims, specific compliance obligations). If uncertain, state assumptions and provide options.
- Be explicit about tradeoffs and why choices were made.

Output rules:
- Output ONLY valid JSON. No markdown, no prose.
- Use Mermaid for all diagrams (C4 + sequences). Put Mermaid code as string values.

Required JSON shape (you may add additional fields, but keep these keys):
{
    "architecture_overview": "...",
    "quality_attributes": {
        "availability": "target + approach",
        "latency": "targets + budgets",
        "scalability": "assumptions + scaling strategy",
        "security": "key controls",
        "privacy": "data minimization + retention",
        "operability": "deploy/rollbacks/runbooks",
        "observability": "logs/metrics/traces"
    },
    "technology_stack": {
        "frontend": ["..."],
        "backend": ["..."],
        "database": ["..."],
        "infrastructure": ["..."],
        "ci_cd": ["..."],
        "observability": ["..."]
    },
    "components": [
        {
            "name": "...",
            "type": "service|frontend|database|external",
            "description": "...",
            "responsibilities": ["..."],
            "interfaces": {
                "apis": [
                    {
                        "method": "GET|POST|PUT|DELETE",
                        "endpoint": "/api/...",
                        "description": "...",
                        "auth": "...",
                        "errors": ["..."]
                    }
                ],
                "events": [{"name": "...", "schema": "..."}]
            },
            "data": {"stores": ["..."], "ownership": "system-of-record?"},
            "scaling": "...",
            "security_controls": ["..."],
            "observability": {"key_metrics": ["..."], "logs": ["..."], "traces": ["..."]}
        }
    ],
    "diagrams": {
        "c4_context": "...",
        "c4_container": "...",
        "c4_component": "...",
        "sequence_diagrams": {"flow_name": "..."}
    },
    "data_models": [
        {
            "name": "EntityName",
            "fields": [{"name": "...", "type": "...", "required": true}],
            "relationships": ["..."],
            "indexes": ["..."],
            "notes": "..."
        }
    ],
    "security_architecture": {
        "authentication": "...",
        "authorization": "...",
        "secrets": "...",
        "encryption": "in transit/at rest",
        "threats": [{"threat": "...", "mitigation": "..."}],
        "abuse_cases": ["..."],
        "audit": "what is audited"
    },
    "operational_design": {
        "deployment": "environments + strategy",
        "configuration": "how config is managed",
        "backups_and_dr": "RPO/RTO assumptions",
        "rate_limits": "...",
        "failure_modes": ["..."],
        "runbooks": ["..."],
        "migration_plan": "if applicable"
    },
    "adrs": [
        {
            "id": "ADR-001",
            "title": "...",
            "status": "proposed|accepted|deprecated",
            "context": "...",
            "decision": "...",
            "consequences": "..."
        }
    ],
    "assumptions": ["..."],
    "open_questions": ["..."]
}

Quality bar:
- Provide enough detail that a senior engineering team can implement APIs, data models, authz, deployment, and operations without major design gaps.
- Include at least: concrete authn/authz approach, key threats + mitigations, telemetry strategy, and failure-mode/runbook considerations.
"""

    async def _process_response(
        self, response: AIMessage, context: AgentContext
    ) -> AgentMessage:
        """Process the LLM response into architecture artifacts."""
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

            architecture = json.loads(json_str)
            artifacts["architecture"] = architecture

            # Update context
            context.architecture = architecture

            # Extract diagrams for easier access
            if "diagrams" in architecture:
                artifacts["diagrams"] = architecture["diagrams"]

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse structured output: {e}")
            artifacts["raw_architecture"] = content
            
            # Try to extract Mermaid diagrams even if JSON fails
            artifacts["diagrams"] = self._extract_mermaid_diagrams(content)

        return AgentMessage(
            from_agent=self.role,
            to_agent=AgentRole.DEVELOPER,
            content=content,
            artifacts=artifacts,
            requires_approval=True,
            approval_status=ApprovalStatus.PENDING,
            metadata={"stage": "architecture_design"},
        )

    def _extract_mermaid_diagrams(self, content: str) -> dict[str, str]:
        """Extract Mermaid diagrams from content."""
        diagrams = {}
        
        # Find all mermaid code blocks
        if "```mermaid" in content:
            parts = content.split("```mermaid")
            for i, part in enumerate(parts[1:], 1):
                if "```" in part:
                    diagram = part.split("```")[0].strip()
                    diagrams[f"diagram_{i}"] = diagram
        
        return diagrams

    async def create_architecture(
        self,
        context: AgentContext,
        work_items_message: AgentMessage,
    ) -> AgentMessage:
        """Create architecture based on requirements and stories.

        Args:
            context: The shared workflow context.
            work_items_message: Message from Business Analyst with work items.

        Returns:
            Agent message with architecture artifacts.
        """
        work_items = work_items_message.artifacts.get("work_items", {})

        input_message = AgentMessage(
            from_agent=AgentRole.BUSINESS_ANALYST,
            to_agent=self.role,
            content=f"""Based on the following requirements and user stories, create a 
comprehensive software architecture.

Treat this as an enterprise stakeholder demo: the output should be production-oriented and implementation-ready.

Project: {context.project_name}
Description: {context.project_description}

Requirements:
{json.dumps(context.requirements, indent=2)}

Epics:
{json.dumps(context.epics, indent=2)}

Stories:
{json.dumps(context.stories, indent=2)}

Create:
1. C4 diagrams (Context, Container, Component) in Mermaid format
2. Sequence diagrams for key user flows
3. Data models
4. API specifications
5. Technology stack recommendations
6. Architectural Decision Records (ADRs)

Ensure the architecture supports all functional and non-functional requirements.""",
        )

        return await self.process(input_message, context)

    async def generate_c4_diagrams(self, context: AgentContext) -> dict[str, str]:
        """Generate C4 model diagrams for the architecture.

        Args:
            context: The shared workflow context.

        Returns:
            Dictionary of diagram type to Mermaid code.
        """
        diagrams = {}

        # Generate Context diagram
        context_diagram = f"""
graph TB
    subgraph boundary [System Boundary]
        system["{context.project_name}<br/>Software System"]
    end
    
    user((User))
    user --> system
    
    style system fill:#438DD5,stroke:#333,color:#fff
    style user fill:#08427B,stroke:#333,color:#fff
"""
        diagrams["c4_context"] = context_diagram

        # Generate Container diagram based on architecture
        if context.architecture and "components" in context.architecture:
            components = context.architecture["components"]
            container_parts = ["graph TB"]
            
            for i, comp in enumerate(components):
                comp_id = f"comp{i}"
                comp_name = comp.get("name", f"Component {i}")
                comp_type = comp.get("type", "service")
                container_parts.append(f'    {comp_id}["{comp_name}<br/>{comp_type}"]')
            
            diagrams["c4_container"] = "\n".join(container_parts)

        return diagrams

    async def refine_architecture(
        self,
        context: AgentContext,
        feedback: str,
    ) -> AgentMessage:
        """Refine architecture based on feedback.

        Args:
            context: The shared workflow context.
            feedback: Feedback from human or other agents.

        Returns:
            Agent message with refined architecture.
        """
        context.human_feedback.append(feedback)

        input_message = AgentMessage(
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=self.role,
            content=f"""Refine the architecture based on the following feedback:

Feedback: {feedback}

Current Architecture:
{json.dumps(context.architecture, indent=2)}

Provide updated architecture addressing the feedback.""",
        )

        return await self.process(input_message, context)
