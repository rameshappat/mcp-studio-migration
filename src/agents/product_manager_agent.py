"""Product Manager Agent - Generates business requirements and product ideas."""

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


class ProductManagerAgent(BaseAgent):
    """Agent that acts as a Product Manager, generating business requirements."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        model_name: str = "gpt-4-turbo",
        temperature: float = 0.8,
    ):
        """Initialize the Product Manager Agent."""
        super().__init__(
            role=AgentRole.PRODUCT_MANAGER,
            llm=llm,
            model_name=model_name,
            temperature=temperature,
        )

    @property
    def system_prompt(self) -> str:
        """Return the system prompt for the Product Manager."""
        return """You are a Principal Product Manager working at an enterprise software company.

Primary objective:
- Translate a high-level product idea into an execution-ready, technically plausible PRD that downstream teams can implement with minimal clarification.

Grounding and rigor rules:
- Do NOT invent external facts (laws, standards, market sizes, vendor pricing). If unknown, state assumptions explicitly.
- Prefer concrete, testable requirements over vague aspirations.
- Make tradeoffs explicit (scope, cost, latency, security).
- Every requirement must have measurable acceptance criteria and a rationale.

Output rules:
- Output ONLY valid JSON. No markdown, no commentary, no trailing commas.
- Keep IDs stable and consistent (REQ-001...).

Required JSON shape (you may add additional fields, but keep these keys):
{
    "product_vision": "...",
    "problem_statement": "...",
    "target_users": ["persona ..."],
    "pain_points": ["..."],
    "in_scope": ["..."],
    "out_of_scope": ["..."],
    "assumptions": ["..."],
    "constraints": ["..."],
    "requirements": [
        {
            "id": "REQ-001",
            "title": "...",
            "description": "...",
            "priority": "high|medium|low",
            "business_value": "...",
            "rationale": "why we need this",
            "user_story": "As a ..., I want ..., so that ...",
            "acceptance_criteria": ["Given... When... Then..."],
            "non_functional": {
                "security": "...",
                "privacy": "...",
                "performance": "...",
                "availability": "...",
                "observability": "..."
            },
            "dependencies": ["..."],
            "risks": ["..."],
            "notes": "implementation-neutral clarifications"
        }
    ],
    "success_metrics": ["metric with target"],
    "release_plan": [
        {"milestone": "MVP", "goals": ["..."], "non_goals": ["..."]},
        {"milestone": "V1", "goals": ["..."], "non_goals": ["..."]}
    ],
    "open_questions": ["..."],
    "glossary": [{"term": "...", "definition": "..."}]
}

Quality bar:
- Do NOT optimize for brevity. Include as many requirements as needed to cover the idea comprehensively.
- If the product is complex, it is normal to produce dozens of requirements spanning functional + non-functional scope.
- Include a mix of functional requirements and explicit non-functional requirements (security, availability, operability, observability).
- Requirements must be consistent with each other and feasible for a small team to build over multiple milestones.
"""

    async def _process_response(
        self, response: AIMessage, context: AgentContext
    ) -> AgentMessage:
        """Process the LLM response into requirements."""
        content = str(response.content)

        # Try to parse structured output
        artifacts = {}
        try:
            # Extract JSON from response
            if "```json" in content:
                json_str = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                json_str = content.split("```")[1].split("```")[0]
            else:
                json_str = content

            requirements_data = json.loads(json_str)
            artifacts["requirements"] = requirements_data

            # Update context
            if "requirements" in requirements_data:
                context.requirements = requirements_data["requirements"]
            if "product_vision" in requirements_data:
                context.project_description = requirements_data["product_vision"]

        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Could not parse structured output: {e}")
            artifacts["raw_requirements"] = content

        return AgentMessage(
            from_agent=self.role,
            to_agent=AgentRole.BUSINESS_ANALYST,
            content=content,
            artifacts=artifacts,
            requires_approval=True,  # Requires human approval before proceeding
            approval_status=ApprovalStatus.PENDING,
            metadata={"stage": "requirements_generation"},
        )

    async def generate_requirements(
        self,
        context: AgentContext,
        domain: str | None = None,
        constraints: list[str] | None = None,
    ) -> AgentMessage:
        """Generate new business requirements.

        Args:
            context: The shared workflow context.
            domain: Optional domain focus (e.g., "fintech", "healthcare").
            constraints: Optional list of constraints to consider.

        Returns:
            Agent message with generated requirements.
        """
        prompt_parts: list[str] = [
            f"Generate business requirements for a new product in the project: {context.project_name}.",
            "Treat this as an enterprise stakeholder demo: output should be execution-ready and technically plausible.",
        ]

        if domain:
            # In this project, the runner often passes the full product idea in `domain`.
            # Prefer including it verbatim when it looks like a natural-language description.
            if len(domain.strip()) > 40 or "\n" in domain:
                prompt_parts.append("Product idea:\n" + domain.strip())
            else:
                prompt_parts.append(f"Domain focus: {domain.strip()}.")

        if constraints:
            prompt_parts.append(f"Constraints: {', '.join(constraints)}")

        prompt_parts.append(
            "Provide comprehensive requirements in the structured JSON format. "
            "Be specific about actors, workflows, data handling, and measurable acceptance criteria."
        )

        input_message = AgentMessage(
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=self.role,
            content=" ".join(prompt_parts),
        )

        return await self.process(input_message, context)

    async def refine_requirements(
        self,
        context: AgentContext,
        feedback: str,
    ) -> AgentMessage:
        """Refine requirements based on feedback.

        Args:
            context: The shared workflow context.
            feedback: Feedback from human or other agents.

        Returns:
            Agent message with refined requirements.
        """
        context.human_feedback.append(feedback)

        input_message = AgentMessage(
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=self.role,
            content=f"""Based on the following feedback, refine the requirements:

Feedback: {feedback}

Current requirements: {json.dumps(context.requirements, indent=2)}

Provide updated requirements addressing the feedback.""",
        )

        return await self.process(input_message, context)
