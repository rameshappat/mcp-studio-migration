"""Base agent class for all specialized agents."""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langsmith import traceable

logger = logging.getLogger(__name__)

# Rate limit retry configuration
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 10  # seconds
MAX_RETRY_DELAY = 120  # seconds


class AgentRole(Enum):
    """Roles for different agents in the pipeline."""

    PRODUCT_MANAGER = "product_manager"
    BUSINESS_ANALYST = "business_analyst"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    ORCHESTRATOR = "orchestrator"


class ApprovalStatus(Enum):
    """Status of human approval."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"


@dataclass
class AgentMessage:
    """Message passed between agents."""

    from_agent: AgentRole
    to_agent: AgentRole | None
    content: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    """Shared context across agents in a workflow."""

    project_name: str = ""
    project_description: str = ""
    requirements: dict[str, Any] = field(default_factory=dict)
    epics: list[dict[str, Any]] = field(default_factory=list)
    stories: list[dict[str, Any]] = field(default_factory=list)
    work_items: dict[str, Any] = field(default_factory=dict)  # Raw work items dict from BA agent
    architecture: dict[str, Any] = field(default_factory=dict)
    code_artifacts: dict[str, str] = field(default_factory=dict)
    ado_work_items: list[dict[str, Any]] = field(default_factory=list)
    github_commits: list[dict[str, Any]] = field(default_factory=list)
    conversation_history: list[AgentMessage] = field(default_factory=list)
    human_feedback: list[str] = field(default_factory=list)


class BaseAgent(ABC):
    """Abstract base class for all agents."""

    def __init__(
        self,
        role: AgentRole,
        llm: ChatOpenAI | None = None,
        model_name: str = "gpt-4-turbo",
        temperature: float = 0.7,
    ):
        """Initialize the base agent.

        Args:
            role: The role of this agent.
            llm: Optional pre-configured LLM instance.
            model_name: OpenAI model to use (if llm not provided).
            temperature: Temperature for LLM responses (if llm not provided).
        """
        self.role = role

        resolved_model = self._resolve_model_name(role=role, default=model_name)
        resolved_temperature = self._resolve_temperature(role=role, default=temperature)
        resolved_provider = self._resolve_provider(role=role)

        self.model_name = resolved_model
        self.temperature = resolved_temperature
        self.provider = resolved_provider
        self._llm = llm if llm is not None else self._create_llm(
            provider=resolved_provider,
            model=resolved_model,
            temperature=resolved_temperature,
        )
        self._tools: list[Any] = []
        self._human_approval_callback: Callable[[AgentMessage], ApprovalStatus] | None = None

    @staticmethod
    def _resolve_provider(role: AgentRole) -> str:
        """Resolve LLM provider.

        Supported values: "openai" (default), "anthropic".
        Priority:
        1) SDLC_LLM_PROVIDER_<ROLE_NAME>
        2) SDLC_LLM_PROVIDER_<ROLE_VALUE>
        3) SDLC_LLM_PROVIDER_DEFAULT
        4) openai
        """
        candidates = [
            f"SDLC_LLM_PROVIDER_{role.name}",
            f"SDLC_LLM_PROVIDER_{role.value.upper()}",
            "SDLC_LLM_PROVIDER_DEFAULT",
        ]
        for key in candidates:
            value = os.environ.get(key)
            if value and value.strip():
                return value.strip().lower()
        return "openai"

    @staticmethod
    def _create_llm(provider: str, model: str, temperature: float):
        """Create an LLM instance for the selected provider."""
        if provider == "anthropic":
            try:
                from langchain_anthropic import ChatAnthropic
            except Exception as e:  # pragma: no cover
                raise RuntimeError(
                    "Anthropic provider selected but langchain-anthropic is not installed. "
                    "Install it (pip install langchain-anthropic) or set SDLC_LLM_PROVIDER_DEFAULT=openai."
                ) from e
            # Limit max_tokens to avoid rate limit issues (8000 tokens/min org limit)
            # and enable retries for rate limit errors
            max_tokens = int(os.getenv("SDLC_ANTHROPIC_MAX_TOKENS", "4096"))
            return ChatAnthropic(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=3,
            )

        # Default: OpenAI
        json_mode = os.getenv("SDLC_OPENAI_JSON_MODE", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        if json_mode:
            return ChatOpenAI(
                model=model,
                temperature=temperature,
                max_retries=3,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        return ChatOpenAI(model=model, temperature=temperature, max_retries=3)

    @staticmethod
    def _resolve_model_name(role: AgentRole, default: str) -> str:
        """Resolve model name with optional per-role environment overrides.

        Priority:
        1) SDLC_MODEL_<ROLE_NAME> (e.g., SDLC_MODEL_PRODUCT_MANAGER)
        2) SDLC_MODEL_<ROLE_VALUE> (e.g., SDLC_MODEL_PRODUCT_MANAGER via role.value)
        3) SDLC_MODEL_DEFAULT
        4) default
        """
        candidates = [
            f"SDLC_MODEL_{role.name}",
            f"SDLC_MODEL_{role.value.upper()}",
            "SDLC_MODEL_DEFAULT",
        ]
        for key in candidates:
            value = os.environ.get(key)
            if value and value.strip():
                return value.strip()
        return default

    @staticmethod
    def _resolve_temperature(role: AgentRole, default: float) -> float:
        """Resolve temperature with optional per-role environment overrides.

        Priority:
        1) SDLC_TEMPERATURE_<ROLE_NAME>
        2) SDLC_TEMPERATURE_<ROLE_VALUE>
        3) SDLC_TEMPERATURE_DEFAULT
        4) default
        """
        candidates = [
            f"SDLC_TEMPERATURE_{role.name}",
            f"SDLC_TEMPERATURE_{role.value.upper()}",
            "SDLC_TEMPERATURE_DEFAULT",
        ]
        for key in candidates:
            value = os.environ.get(key)
            if not value or not value.strip():
                continue
            try:
                return float(value.strip())
            except ValueError:
                logger.warning(f"Ignoring invalid {key}={value!r}; using default")
        return default

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Return the system prompt for this agent."""
        pass

    def set_tools(self, tools: list[Any]) -> None:
        """Set the tools available to this agent."""
        self._tools = tools
        if tools:
            self._llm = self._llm.bind_tools(tools)

    def set_human_approval_callback(
        self, callback: Callable[[AgentMessage], ApprovalStatus]
    ) -> None:
        """Set the callback for human approval."""
        self._human_approval_callback = callback

    @traceable(name="agent.process")
    async def process(
        self,
        input_message: AgentMessage,
        context: AgentContext,
    ) -> AgentMessage:
        """Process an incoming message and return a response.

        Args:
            input_message: The incoming message from another agent or human.
            context: The shared workflow context.

        Returns:
            Response message with artifacts.
        """
        logger.info(f"{self.role.value} processing message from {input_message.from_agent.value}")

        # Build messages for the LLM
        messages = self._build_messages(input_message, context)

        # Generate response with retry logic for rate limits
        response = await self._invoke_with_retry(messages)

        # Process the response into an agent message
        output = await self._process_response(response, context)

        # Check if human approval is needed
        if output.requires_approval and self._human_approval_callback:
            output.approval_status = self._human_approval_callback(output)

            if output.approval_status == ApprovalStatus.REJECTED:
                logger.info(f"{self.role.value} output rejected by human")
            elif output.approval_status == ApprovalStatus.REVISION_REQUESTED:
                logger.info(f"{self.role.value} revision requested by human")

        # Add to conversation history
        context.conversation_history.append(input_message)
        context.conversation_history.append(output)

        return output

    async def _invoke_with_retry(self, messages: list[BaseMessage]) -> AIMessage:
        """Invoke LLM with exponential backoff retry for rate limit errors.

        Args:
            messages: The messages to send to the LLM.

        Returns:
            The LLM response.

        Raises:
            Exception: If all retries are exhausted.
        """
        last_error = None
        delay = INITIAL_RETRY_DELAY

        for attempt in range(MAX_RETRIES):
            try:
                return await self._llm.ainvoke(messages)
            except Exception as e:
                error_str = str(e).lower()
                # Check for rate limit errors (429 or rate_limit in message)
                if "429" in str(e) or "rate_limit" in error_str or "rate limit" in error_str:
                    last_error = e
                    if attempt < MAX_RETRIES - 1:
                        logger.warning(
                            f"Rate limit hit, waiting {delay}s before retry "
                            f"(attempt {attempt + 1}/{MAX_RETRIES})"
                        )
                        print(f"⏳ Rate limit reached. Waiting {delay} seconds before retry...")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, MAX_RETRY_DELAY)  # Exponential backoff
                    else:
                        logger.error(f"Rate limit: all {MAX_RETRIES} retries exhausted")
                        raise
                else:
                    # Non-rate-limit error, raise immediately
                    raise

        raise last_error

    def _build_messages(
        self, input_message: AgentMessage, context: AgentContext
    ) -> list[BaseMessage]:
        """Build the message list for the LLM."""
        messages: list[BaseMessage] = [
            SystemMessage(content=self.system_prompt),
        ]

        # Add context summary
        context_summary = self._build_context_summary(context)
        if context_summary:
            messages.append(SystemMessage(content=f"Current Project Context:\n{context_summary}"))

        # Add recent conversation history
        for msg in context.conversation_history[-5:]:
            if msg.from_agent == self.role:
                messages.append(AIMessage(content=msg.content))
            else:
                messages.append(HumanMessage(content=f"[{msg.from_agent.value}]: {msg.content}"))

        # Add the current input
        messages.append(HumanMessage(content=input_message.content))

        return messages

    def _build_context_summary(self, context: AgentContext) -> str:
        """Build a summary of the current context."""
        parts = [f"Project: {context.project_name}"]

        if context.project_description:
            parts.append(f"Description: {context.project_description}")

        if context.requirements:
            parts.append(f"Requirements: {len(context.requirements)} defined")

        if context.epics:
            parts.append(f"Epics: {len(context.epics)} created")

        if context.stories:
            parts.append(f"Stories: {len(context.stories)} created")

        if context.architecture:
            parts.append("Architecture: Defined")

        if context.code_artifacts:
            parts.append(f"Code Files: {len(context.code_artifacts)} generated")

        return "\n".join(parts)

    @abstractmethod
    async def _process_response(
        self, response: AIMessage, context: AgentContext
    ) -> AgentMessage:
        """Process the LLM response into an agent message.

        Args:
            response: The LLM response.
            context: The shared workflow context.

        Returns:
            Processed agent message with artifacts.
        """
        pass

    async def handoff_to(
        self,
        target_agent: "BaseAgent",
        message: AgentMessage,
        context: AgentContext,
    ) -> AgentMessage:
        """Hand off work to another agent.

        Args:
            target_agent: The agent to hand off to.
            message: The message to send.
            context: The shared workflow context.

        Returns:
            Response from the target agent.
        """
        logger.info(f"{self.role.value} handing off to {target_agent.role.value}")

        # Update message routing
        message.from_agent = self.role
        message.to_agent = target_agent.role

        return await target_agent.process(message, context)
