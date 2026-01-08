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

TECHNOLOGY PREFERENCES (Northern Trust Standards):
- Cloud Platform: Azure (all infrastructure must be Azure-native)
- Frontend: React JS
- Backend: Java-based microservices (Spring Boot recommended)
- All code must follow SSDLC (Secure Software Development Life Cycle) requirements

SSDLC REQUIREMENTS:
- Implement Secure-by-Design principles and DevSecOps practices
- Include MFA (Multi-Factor Authentication), biometric authentication, and RBAC (Role-Based Access Control)
- Data encryption: end-to-end for data in transit and at rest
- Secure key management and tokenization of sensitive data
- Automated security testing in CI/CD pipelines (vulnerability scanning, dependency analysis)
- Compliance: PCI DSS, NIST Cybersecurity Framework, ISO 27001/27002
- For APIs: OAuth 2.0 and OpenID Connect standards
- Include fraud monitoring capabilities for ACH transactions (Nacha Operating Rules 2026)

Grounding rules:
- Do NOT invent external facts (vendor pricing, regulatory claims, specific compliance obligations). If uncertain, state assumptions and provide options.
- Be explicit about tradeoffs and why choices were made.

Output rules:
- Output ONLY valid JSON. No markdown, no prose.
- Use Mermaid for all diagrams (C4 + sequences). Put Mermaid code as string values.

CRITICAL - Mermaid diagram syntax rules (MUST FOLLOW EXACTLY):
1. Use ONLY these diagram types: "graph TB", "graph LR", "flowchart TB", "sequenceDiagram"
2. Use \\n for newlines within JSON strings
3. Node IDs must be simple alphanumeric (no spaces, no special chars): User, API, DB, WebApp
4. Labels in brackets must NOT contain quotes or special characters: [Web Application] not ["Web App"]
5. Use simple arrow syntax: --> or ->> (no complex arrows)
6. NO parentheses in node definitions except for database cylinders: [(Database)]
7. NO quotes inside node labels
8. Keep diagrams simple with 5-10 nodes maximum

VALID diagram examples (copy this exact syntax pattern):
- "graph TB\\n  User[User]-->WebApp[Web Application]\\n  WebApp-->API[API Server]\\n  API-->DB[(Database)]"
- "graph LR\\n  A[Frontend]-->B[Backend]\\n  B-->C[Cache]\\n  B-->D[(PostgreSQL)]"
- "sequenceDiagram\\n  User->>API: Request\\n  API->>DB: Query\\n  DB-->>API: Result\\n  API-->>User: Response"

INVALID patterns to AVOID:
- Do NOT use: A["Label"] - no quotes in labels
- Do NOT use: A(Label) - no parentheses except for [(Database)]
- Do NOT use special characters in labels: &, <, >, quotes
- Do NOT use C4Context, C4Container - use simple graph TB instead

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
        "c4_context": "graph TB\\n  User[User]-->App[Application]\\n  App-->DB[(Database)]\\n  App-->ExtAPI[External API]",
        "c4_container": "graph TB\\n  WebApp[Web App]-->API[API Server]\\n  API-->Cache[Redis Cache]\\n  API-->DB[(PostgreSQL)]\\n  API-->Queue[Message Queue]",
        "c4_component": "graph TB\\n  Auth[Auth Module]-->UserSvc[User Service]\\n  UserSvc-->Repo[Repository]\\n  Repo-->DB[(Database)]",
        "sequence_main": "sequenceDiagram\\n  User->>WebApp: Request\\n  WebApp->>API: Forward\\n  API->>DB: Query\\n  DB-->>API: Data\\n  API-->>WebApp: Response\\n  WebApp-->>User: Display"
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
