#!/usr/bin/env python3
"""Interactive SDLC Pipeline Runner.

This script runs the multi-agent SDLC pipeline with real LLM calls
and Human-in-the-Loop interactions for approvals and feedback.
"""

import asyncio
import os
import sys
import json
import re
from typing import Any

from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env (never hard-code secrets in source code)
# Use an explicit path to avoid python-dotenv find_dotenv() stack-frame issues on Python 3.14.
_dotenv_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_dotenv_path if _dotenv_path.exists() else None)

# Back-compat: allow users to provide the ADO PAT as AZURE_DEVOPS_TOKEN.
# The ADO MCP server expects ADO_MCP_AUTH_TOKEN when auth_type=envvar.
if os.getenv("AZURE_DEVOPS_TOKEN") and not os.getenv("ADO_MCP_AUTH_TOKEN"):
    os.environ["ADO_MCP_AUTH_TOKEN"] = os.environ["AZURE_DEVOPS_TOKEN"]


def validate_required_env() -> list[str]:
    """Validate required environment variables for running the full SDLC pipeline."""
    missing: list[str] = []

    # LLM provider keys
    provider_default = os.getenv("SDLC_LLM_PROVIDER_DEFAULT", "anthropic").strip().lower()
    if provider_default == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            missing.append("ANTHROPIC_API_KEY")
    else:
        if not os.getenv("OPENAI_API_KEY"):
            missing.append("OPENAI_API_KEY")

    # ADO + GitHub are optional at runtime (user can choose to skip pushes),
    # but if they want full end-to-end, these should be set.
    return missing

from src.agents import (
    AgentContext,
    AgentRole,
    ApprovalStatus,
    ProductManagerAgent,
    BusinessAnalystAgent,
    ArchitectAgent,
    DeveloperAgent,
    HumanInTheLoop,
    InteractionType,
)
from src.mcp_client import GitHubMCPClient, AzureDevOpsMCPClient, MermaidMCPClient


def is_approved(status: ApprovalStatus) -> bool:
    """Check if approval status is approved."""
    return status == ApprovalStatus.APPROVED


def print_banner():
    """Print welcome banner."""
    print("\n" + "=" * 70)
    print("🚀 SDLC Multi-Agent Pipeline with Human-in-the-Loop")
    print("=" * 70)
    print("\nThis pipeline orchestrates multiple AI agents:")
    print("  1️⃣  Product Manager Agent - Generates business requirements")
    print("  2️⃣  Business Analyst Agent - Creates Epics & User Stories")
    print("  3️⃣  Architect Agent - Designs system architecture (C4/Mermaid)")
    print("  4️⃣  Developer Agent - Generates full-stack code")
    print("\nYou will be prompted to approve each stage before proceeding.")
    print("=" * 70 + "\n")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _extract_int_id(obj: object, keys: tuple[str, ...]) -> int | None:
    """Best-effort extraction of an integer id from MCP responses.

    Some MCP servers return rich dicts, others return {'text': '<json or message>'}.
    """

    def _from_text(text: str) -> int | None:
        s = (text or "").strip()
        if not s:
            return None

        # If text contains JSON, parse and recurse.
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                parsed = json.loads(s)
                return _extract_int_id(parsed, keys)
            except Exception:
                pass

        # Prefer explicit JSON-like id fields in the text.
        for key in keys:
            m = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', s)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    return None

        # Also allow patterns like 'planId: 123' or 'id = 123'
        for key in keys:
            m = re.search(rf'\b{re.escape(key)}\b\s*[:=]\s*(\d+)', s)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    return None

        return None

    if obj is None:
        return None
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return _from_text(obj)
    if isinstance(obj, list):
        for item in obj:
            found = _extract_int_id(item, keys)
            if found:
                return found
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)

        text = obj.get("text")
        if isinstance(text, str):
            found = _from_text(text)
            if found:
                return found

        for v in obj.values():
            found = _extract_int_id(v, keys)
            if found:
                return found

    return None


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
    # collapse double backslashes -> single (repeat until stable)
    while "\\\\" in p:
        p = p.replace("\\\\", "\\")
    if not p.startswith("\\"):
        p = "\\" + p
    return p


def _looks_like_ado_error_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    # Common ADO error prefixes/codes, e.g. TF200001
    if "tf" in t and any(ch.isdigit() for ch in t):
        return True
    if t.startswith("error") or "exception" in t:
        return True
    return False


def _print_exception_group(eg: BaseException, indent: int = 0) -> None:
    """Recursively print nested exception groups for better debugging."""
    prefix = " " * indent
    if hasattr(eg, "exceptions"):
        for sub in eg.exceptions:
            if hasattr(sub, "exceptions"):
                print(f"{prefix}- ExceptionGroup: {sub}")
                _print_exception_group(sub, indent + 2)
            else:
                # Get the actual error message
                error_msg = str(sub)
                error_type = type(sub).__name__
                print(f"{prefix}- {error_type}: {error_msg}")


def _get_root_exception(eg: BaseException) -> str:
    """Extract the root cause message from nested exception groups."""
    if hasattr(eg, "exceptions") and eg.exceptions:
        # Recursively find the deepest exception
        first = eg.exceptions[0]
        if hasattr(first, "exceptions"):
            return _get_root_exception(first)
        return str(first)
    return str(eg)


def _sanitize_mermaid_diagram(diagram: str) -> str:
    """Sanitize Mermaid diagram to fix common LLM-generated issues."""
    if not diagram:
        return diagram
    
    # Replace escaped newlines with actual newlines
    result = diagram.replace("\\n", "\n")
    
    # Remove any leading/trailing whitespace
    result = result.strip()
    
    # Fix common issues with quotes in node labels
    # e.g., A["Label with "quotes""] -> A["Label with 'quotes'"]
    # This is a simple fix; complex cases may need more handling
    
    # Remove any markdown code fence markers if present
    if result.startswith("```"):
        lines = result.split("\n")
        # Remove first line if it's a code fence
        if lines[0].startswith("```"):
            lines = lines[1:]
        # Remove last line if it's a code fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        result = "\n".join(lines)
    
    # Fix common problematic patterns
    import re
    
    # Remove quotes inside square brackets: [\"Label\"] -> [Label]
    result = re.sub(r'\[\\?"([^"]*?)\\?"\]', r'[\1]', result)
    result = re.sub(r"\[\\?'([^']*?)\\?'\]", r'[\1]', result)
    
    # Remove problematic activation markers in sequence diagrams: ->>+ -> ->>
    result = result.replace("->>+", "->>")
    result = result.replace("-->>-", "-->>")
    
    # Replace problematic subgraph syntax that might fail
    # Some renderers have issues with spaces in subgraph names
    result = re.sub(r'subgraph\s+([A-Za-z0-9_]+)\s+([A-Za-z0-9_])', r'subgraph \1_\2', result)
    
    return result


def _validate_mermaid_diagram(diagram: str) -> tuple[bool, str]:
    """Validate Mermaid diagram syntax and return (is_valid, error_or_fixed_diagram).
    
    Returns:
        (True, diagram) if valid or fixable
        (False, error_message) if unfixable
    """
    if not diagram or not diagram.strip():
        return False, "Empty diagram"
    
    # Check for valid starter
    starters = ("graph ", "graph\n", "flowchart ", "flowchart\n", "sequenceDiagram", 
                "classDiagram", "erDiagram")
    if not any(diagram.strip().startswith(s) for s in starters):
        return False, f"Invalid diagram type. Must start with one of: {starters}"
    
    # Check for common syntax issues
    if '["' in diagram or "['" in diagram:
        return False, "Quotes inside node labels are not allowed"
    
    return True, diagram


def _looks_like_mermaid_diagram(text: str) -> bool:
    """Heuristic check for Mermaid diagram definitions.

    Avoid sending arbitrary strings (e.g., descriptions) to the Mermaid renderer.
    """

    t = (text or "").strip()
    if not t:
        return False

    # Common Mermaid diagram starters/keywords.
    starters = (
        "graph ",
        "graph\n",
        "flowchart ",
        "flowchart\n",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram",
        "stateDiagram-v2",
        "erDiagram",
        "journey",
        "gantt",
        "pie",
        "mindmap",
        "timeline",
        "gitGraph",
        "sankey-beta",
        "quadrantChart",
        "C4Context",
        "C4Container",
        "C4Component",
        "C4Deployment",
    )
    return any(s in t for s in starters)


def _extract_all_diagrams(arch_message_or_context: Any) -> dict[str, str]:
    """Extract all Mermaid diagrams from architecture output.
    
    Looks in multiple places:
    1. artifacts["diagrams"] - direct diagrams dict
    2. artifacts["architecture"]["diagrams"] - nested in architecture
    3. Raw content - extract ```mermaid blocks
    """
    diagrams = {}
    
    # Try to get from artifacts
    artifacts = {}
    content = ""
    
    if hasattr(arch_message_or_context, 'artifacts'):
        artifacts = arch_message_or_context.artifacts or {}
        content = str(arch_message_or_context.content or "")
    elif isinstance(arch_message_or_context, dict):
        artifacts = arch_message_or_context
    
    # Check artifacts["diagrams"]
    if "diagrams" in artifacts and isinstance(artifacts["diagrams"], dict):
        for key, value in artifacts["diagrams"].items():
            if isinstance(value, str) and _looks_like_mermaid_diagram(value):
                diagrams[key] = value
            elif isinstance(value, dict):
                # Handle nested diagrams like sequence_diagrams: {flow_name: "..."}
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, str) and _looks_like_mermaid_diagram(sub_value):
                        diagrams[f"{key}_{sub_key}"] = sub_value
    
    # Check artifacts["architecture"]["diagrams"]
    if "architecture" in artifacts and isinstance(artifacts["architecture"], dict):
        arch = artifacts["architecture"]
        if "diagrams" in arch and isinstance(arch["diagrams"], dict):
            for key, value in arch["diagrams"].items():
                if key not in diagrams:  # Don't overwrite
                    if isinstance(value, str) and _looks_like_mermaid_diagram(value):
                        diagrams[key] = value
                    elif isinstance(value, dict):
                        for sub_key, sub_value in value.items():
                            full_key = f"{key}_{sub_key}"
                            if full_key not in diagrams and isinstance(sub_value, str) and _looks_like_mermaid_diagram(sub_value):
                                diagrams[full_key] = sub_value
    
    # Extract from raw content (```mermaid blocks)
    if content and "```mermaid" in content:
        parts = content.split("```mermaid")
        for i, part in enumerate(parts[1:], 1):
            if "```" in part:
                diagram = part.split("```")[0].strip()
                if diagram and _looks_like_mermaid_diagram(diagram):
                    key = f"extracted_diagram_{i}"
                    if key not in diagrams:
                        diagrams[key] = diagram
    
    return diagrams


def get_project_idea() -> tuple[str, str]:
    """Prompt user for project idea and name."""
    if _env_bool("SDLC_NON_INTERACTIVE", default=False):
        project_name = (os.getenv("SDLC_PROJECT_NAME") or "my-project").strip() or "my-project"
        idea = (os.getenv("SDLC_PROJECT_IDEA") or "").strip() or "A simple task management application with user authentication"
        return project_name, idea

    print("📝 Let's start with your product idea!\n")

    project_name = input("Enter project name (e.g., 'task-tracker'): ").strip()
    if not project_name:
        project_name = "my-project"

    print("\nDescribe your product idea. Be as detailed as you like.")
    print("(Press Enter twice when done)\n")

    lines = []
    empty_count = 0
    while True:
        line = input()
        if line == "":
            empty_count += 1
            if empty_count >= 2:
                break
        else:
            empty_count = 0
        lines.append(line)

    idea = "\n".join(lines).strip()
    if not idea:
        idea = "A simple task management application with user authentication"

    return project_name, idea


def print_stage_header(stage_name: str, emoji: str):
    """Print a stage header."""
    print("\n" + "-" * 70)
    print(f"{emoji} Stage: {stage_name}")
    print("-" * 70)


def print_agent_output(title: str, content: str, max_lines: int = 50):
    """Print agent output with truncation."""
    print(f"\n📄 {title}:")
    print("-" * 40)
    lines = content.split("\n")
    if len(lines) > max_lines:
        print("\n".join(lines[:max_lines]))
        print(f"\n... [{len(lines) - max_lines} more lines truncated]")
    else:
        print(content)
    print("-" * 40)


async def preflight_api_check() -> bool:
    """Verify LLM API connectivity before starting the pipeline.
    
    Returns:
        True if API is accessible and responsive, False otherwise.
    """
    provider = (os.getenv("SDLC_LLM_PROVIDER_DEFAULT") or "openai").strip().lower()
    model = os.getenv("SDLC_MODEL_DEFAULT") or ("gpt-4o" if provider == "openai" else "claude-opus-4-20250514")
    
    try:
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            llm = ChatAnthropic(model=model, temperature=0, max_tokens=50, max_retries=2)
        else:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(model=model, temperature=0, max_tokens=50)
        
        # Simple test call
        response = await llm.ainvoke([{"role": "user", "content": "Say 'ok' only."}])
        if response and response.content:
            print(f"   ✓ {provider.upper()} API ({model}) is responsive")
            return True
        else:
            print(f"   ✗ {provider.upper()} API returned empty response")
            return False
    except Exception as e:
        error_msg = str(e)
        if "rate_limit" in error_msg.lower() or "429" in error_msg:
            print(f"   ⚠️ {provider.upper()} rate limit detected. Waiting 60 seconds...")
            await asyncio.sleep(60)
            return await preflight_api_check()  # Retry once after waiting
        print(f"   ✗ {provider.upper()} API error: {e}")
        return False


async def run_pipeline():
    """Run the interactive SDLC pipeline."""
    print_banner()

    missing = validate_required_env()
    if missing:
        print("\n❌ Missing required configuration: " + ", ".join(missing))
        print("\nCreate a local .env file from .env.example and set your keys.")
        print("Example:")
        print("  cp .env.example .env")
        print("  # then edit .env and set OPENAI_API_KEY or ANTHROPIC_API_KEY")
        return
    
    # Get project idea from user
    project_name, project_idea = get_project_idea()
    
    print(f"\n✅ Project: {project_name}")
    print(f"✅ Idea: {project_idea[:100]}...")
    
    # For demo reliability, prefer OpenAI (higher rate limits) unless explicitly configured.
    # Set SDLC_PREFER_ANTHROPIC=true to use Anthropic/Claude instead.
    # Agents will resolve per-role overrides (SDLC_LLM_PROVIDER_<ROLE>, SDLC_MODEL_<ROLE>, etc.).
    prefer_anthropic = _env_bool("SDLC_PREFER_ANTHROPIC", default=False)

    current_provider = (os.getenv("SDLC_LLM_PROVIDER_DEFAULT") or "").strip().lower()
    current_model = (os.getenv("SDLC_MODEL_DEFAULT") or "").strip()

    if prefer_anthropic and os.getenv("ANTHROPIC_API_KEY"):
        # Override OpenAI defaults commonly present in .env for previous runs.
        if not current_provider or current_provider in {"openai", "gpt"}:
            os.environ["SDLC_LLM_PROVIDER_DEFAULT"] = "anthropic"
        if not current_model or current_model.startswith("gpt-"):
            os.environ["SDLC_MODEL_DEFAULT"] = "claude-opus-4-20250514"

        # Ensure every agent uses Anthropic/Opus unless explicitly overridden.
        # (Env vars set by the user take precedence; we only set defaults here.)
        for role_name in ("PRODUCT_MANAGER", "BUSINESS_ANALYST", "ARCHITECT", "DEVELOPER"):
            os.environ.setdefault(f"SDLC_LLM_PROVIDER_{role_name}", "anthropic")
            os.environ.setdefault(f"SDLC_MODEL_{role_name}", "claude-opus-4-20250514")
    else:
        # Prefer OpenAI for demo reliability (higher rate limits)
        if not current_provider or current_provider == "anthropic":
            os.environ["SDLC_LLM_PROVIDER_DEFAULT"] = "openai"
        if not current_model or current_model.startswith("claude"):
            os.environ["SDLC_MODEL_DEFAULT"] = "gpt-4o"

    print("\n🔧 Initializing LLM configuration...")
    print(f"   Provider default: {os.getenv('SDLC_LLM_PROVIDER_DEFAULT')}")
    print(f"   Model default: {os.getenv('SDLC_MODEL_DEFAULT')}")
    
    # Pre-flight API connectivity check
    print("\n🔍 Running pre-flight API check...")
    preflight_ok = await preflight_api_check()
    if not preflight_ok:
        print("\n❌ Pre-flight check failed. Please verify your API keys and try again.")
        return
    print("✅ Pre-flight check passed!")
    
    # Initialize Human-in-the-Loop (supports automation via env vars)
    hil = HumanInTheLoop(
        interactive=not _env_bool("SDLC_NON_INTERACTIVE", default=False),
        auto_approve=_env_bool("SDLC_AUTO_APPROVE", default=False),
    )
    
    # Initialize agents (each agent creates its own LLM using BaseAgent env resolution)
    print("🔧 Initializing agents...")
    pm_agent = ProductManagerAgent(llm=None)
    ba_agent = BusinessAnalystAgent(llm=None)
    architect_agent = ArchitectAgent(llm=None)
    developer_agent = DeveloperAgent(llm=None)
    
    # Initialize context
    context = AgentContext(project_name=project_name)
    
    # =========================================================================
    # Stage 1: Product Manager - Generate Requirements
    # =========================================================================
    print_stage_header("Product Manager - Requirements Generation", "1️⃣")
    
    print("\n🤖 Product Manager is analyzing your idea and generating requirements...")
    
    try:
        pm_message = await pm_agent.generate_requirements(
            context=context,
            domain=project_idea,
            constraints=[
                "Must be scalable",
                "Should have good UX",
                "Security is important",
                "Do not limit scope artificially; include as much detail as needed to wow an enterprise stakeholder",
            ],
        )
        
        print_agent_output("Generated Requirements", pm_message.content)
        
        # Human approval for requirements
        approval_status = hil.request_approval(pm_message)
        
        if not is_approved(approval_status):
            feedback = hil.request_feedback(
                "What changes would you like to the requirements?",
                {"current_requirements": pm_message.content}
            )
            print(f"\n📝 Feedback received: {feedback}")
            print("🔄 Refining requirements based on feedback...")
            pm_message = await pm_agent.refine_requirements(context, feedback)
            print_agent_output("Refined Requirements", pm_message.content)
            
            # Re-approve
            approval_status = hil.request_approval(pm_message)
            if not is_approved(approval_status):
                print("\n❌ Requirements not approved. Exiting pipeline.")
                return
        
        pm_message.approval_status = ApprovalStatus.APPROVED
        # Keep `context.requirements` as the list of REQ items (avoid overwriting with the full PRD dict).
        prd = pm_message.artifacts.get("requirements", {})
        if isinstance(prd, dict) and isinstance(prd.get("requirements"), list):
            context.requirements = prd.get("requirements", [])
        
    except Exception as e:
        print(f"\n❌ Error in Product Manager stage: {e}")
        raise
    
    # =========================================================================
    # Stage 2: Business Analyst - Create Work Items
    # =========================================================================
    print_stage_header("Business Analyst - Epics & User Stories", "2️⃣")
    
    print("\n🤖 Business Analyst is creating Epics and User Stories...")
    
    try:
        ba_message = await ba_agent.create_work_items(context, pm_message)
        
        print_agent_output("Work Items (Epics & Stories)", ba_message.content)
        
        # Human approval for work items
        approval_status = hil.request_approval(ba_message)
        
        if not is_approved(approval_status):
            feedback = hil.request_feedback(
                "What changes would you like to the work items?",
                {"current_work_items": ba_message.content}
            )
            print(f"\n📝 Feedback received: {feedback}")
            print("🔄 Refining work items based on feedback...")
            ba_message = await ba_agent.refine_work_items(context, feedback)
            print_agent_output("Refined Work Items", ba_message.content)
            
            approval_status = hil.request_approval(ba_message)
            if not is_approved(approval_status):
                print("\n❌ Work items not approved. Exiting pipeline.")
                return
        
        ba_message.approval_status = ApprovalStatus.APPROVED
        context.work_items = ba_message.artifacts.get("work_items", {})
        
        # Ensure context.epics and context.stories are populated from work_items
        # This handles cases where _process_response may not have set them
        if isinstance(context.work_items, dict):
            if not context.epics and "epics" in context.work_items:
                context.epics = context.work_items["epics"]
                print(f"   📦 Loaded {len(context.epics)} epics from work items")
            if not context.stories and "stories" in context.work_items:
                context.stories = context.work_items["stories"]
                print(f"   📦 Loaded {len(context.stories)} stories from work items")
        
        # Ask about pushing to Azure DevOps
        push_to_ado = (
            _env_bool("SDLC_PUSH_TO_ADO", default=False)
            if _env_bool("SDLC_NON_INTERACTIVE", default=False)
            else hil.request_confirmation(
                "Would you like to push these work items to Azure DevOps?",
                default=False,
            )
        )

        # Ask about creating an Azure DevOps Test Plan
        create_test_plan = (
            _env_bool("SDLC_CREATE_TESTPLAN", default=False)
            if _env_bool("SDLC_NON_INTERACTIVE", default=False)
            else hil.request_confirmation(
                "Would you like to create an Azure DevOps Test Plan?",
                default=False,
            )
        )

        if push_to_ado or create_test_plan:
            print("\n🔧 Connecting to Azure DevOps MCP Server (stdio)...")
            try:
                # Use PAT-based auth if available; recommended for reliability.
                # The Azure DevOps MCP server expects ADO_MCP_AUTH_TOKEN with auth_type=envvar.
                auth_type = "envvar" if os.environ.get("ADO_MCP_AUTH_TOKEN") else "interactive"
                ado_client = AzureDevOpsMCPClient(
                    organization=os.environ["AZURE_DEVOPS_ORGANIZATION"],
                    project=os.environ["AZURE_DEVOPS_PROJECT"],
                    auth_type=auth_type,
                )
                await ado_client.connect()
                ba_agent.set_ado_client(ado_client)

                if push_to_ado:
                    print("📤 Pushing work items to Azure DevOps...")
                    result = await ba_agent.push_to_azure_devops(context)
                    if "error" in result:
                        print(f"⚠️ Push had issues: {result}")
                    else:
                        print("✅ Work items pushed to Azure DevOps!")
                        print(f"   Epics created: {len(result.get('epics', []))}")
                        print(f"   Stories created: {len(result.get('stories', []))}")

                if create_test_plan:
                    # Fetch valid iteration paths to help choose a correct one.
                    iteration_paths: list[str] = []
                    try:
                        iters = await ado_client.call_tool(
                            "work_list_iterations",
                            {"project": os.environ["AZURE_DEVOPS_PROJECT"], "depth": 10},
                        )
                        iteration_paths = [_normalize_ado_path(p) for p in _flatten_iteration_paths(iters)]
                    except Exception:
                        iteration_paths = []

                    if _env_bool("SDLC_NON_INTERACTIVE", default=False):
                        plan_name = (os.environ.get("SDLC_TESTPLAN_NAME") or f"{project_name} - Test Plan").strip()
                        iteration = _normalize_ado_path(os.environ.get("SDLC_TESTPLAN_ITERATION") or "")
                        description = (os.environ.get("SDLC_TESTPLAN_DESCRIPTION") or "").strip() or None
                        existing_plan_id = (os.environ.get("SDLC_TESTPLAN_ID") or "").strip()
                        existing_suite_id = (os.environ.get("SDLC_TESTSUITE_ID") or "").strip()

                        # Auto-pick a sane default if not provided.
                        if not iteration and iteration_paths:
                            # Prefer a leaf sprint path if present.
                            leaf = next((p for p in iteration_paths if "\\Iteration\\" in p and not p.endswith("\\Iteration")), None)
                            iteration = leaf or iteration_paths[0]
                    else:
                        plan_name = input("\nEnter Test Plan name (press Enter for default): ").strip()
                        if not plan_name:
                            plan_name = f"{project_name} - Test Plan"

                        env_iteration_default = _normalize_ado_path(
                            (os.environ.get("SDLC_TESTPLAN_ITERATION") or "").strip()
                        )
                        env_existing_plan_id_default = (os.environ.get("SDLC_TESTPLAN_ID") or "").strip()
                        env_existing_suite_id_default = (os.environ.get("SDLC_TESTSUITE_ID") or "").strip()

                        if iteration_paths:
                            print("\nAvailable iteration paths (examples):")
                            for p in iteration_paths[:10]:
                                print(f"  - {p}")

                        # Default iteration:
                        # 1) SDLC_TESTPLAN_ITERATION if set
                        # 2) a leaf sprint path from the discovered iteration tree
                        # 3) blank (user must enter)
                        auto_leaf_default = ""
                        if iteration_paths:
                            auto_leaf_default = (
                                next(
                                    (
                                        p
                                        for p in iteration_paths
                                        if "\\Iteration\\" in p and not p.endswith("\\Iteration")
                                    ),
                                    "",
                                )
                                or iteration_paths[0]
                            )
                        iteration_default = env_iteration_default or auto_leaf_default
                        iteration_prompt = "Enter iteration path (press Enter for default)"
                        if iteration_default:
                            iteration_prompt += f" [{iteration_default}]"
                        iteration_prompt += ": "
                        iteration_in = input(iteration_prompt).strip()
                        iteration = _normalize_ado_path(iteration_in or iteration_default)

                        description = input("Optional description (press Enter to skip): ").strip() or None

                        plan_id_prompt = "Optional: existing Test Plan ID to use"
                        if env_existing_plan_id_default:
                            plan_id_prompt += f" [{env_existing_plan_id_default}]"
                        plan_id_prompt += ": "
                        existing_plan_id = (input(plan_id_prompt).strip() or env_existing_plan_id_default)

                        suite_id_prompt = "Optional: existing Suite ID to populate"
                        if env_existing_suite_id_default:
                            suite_id_prompt += f" [{env_existing_suite_id_default}]"
                        suite_id_prompt += " (press Enter to create suite): "
                        existing_suite_id = (input(suite_id_prompt).strip() or env_existing_suite_id_default)

                    if not iteration:
                        print("⚠️ No iteration path provided; skipping Test Plan creation.")
                    elif iteration_paths and iteration not in iteration_paths:
                        print("⚠️ Iteration path does not match this project. Skipping Test Plan creation.")
                        if iteration_paths:
                            print("   Use one of these iteration paths:")
                            for p in iteration_paths[:10]:
                                print(f"   - {p}")
                        if existing_plan_id.isdigit():
                            plan_id = int(existing_plan_id)
                            print(f"ℹ️ Using existing Test Plan ID: {plan_id}")
                            suite_name = f"{project_name} - MVP Regression"
                            try:
                                suite = await ado_client.create_test_suite(
                                    plan_id=plan_id,
                                    parent_suite_id=plan_id,
                                    name=suite_name,
                                )
                                suite_id = _extract_int_id(suite, ("id", "suiteId"))
                                if not suite_id:
                                    print(f"⚠️ Created suite but could not read suite id: {suite}")
                                else:
                                    stories = getattr(context, "stories", None) or (context.work_items or {}).get("stories") or []
                                    created_case_ids: list[int] = []
                                    for story in stories:
                                        if not isinstance(story, dict):
                                            continue
                                        story_id = str(story.get("id") or "").strip()
                                        title = str(story.get("title") or "Story").strip()
                                        tc_title = f"{story_id}: {title}" if story_id else title

                                        ac = story.get("acceptance_criteria") or []
                                        if not isinstance(ac, list):
                                            ac = [str(ac)]
                                        steps_lines: list[str] = []
                                        n = 1
                                        for item in ac:
                                            item_s = str(item or "").replace("|", "/").strip()
                                            if not item_s:
                                                continue
                                            steps_lines.append(f"{n}. {item_s}|{item_s}")
                                            n += 1
                                        if not steps_lines:
                                            steps_lines = [
                                                f"1. Verify {title} works end-to-end|{title} behaves as specified"
                                            ]
                                        steps = "\n".join(steps_lines)

                                        try:
                                            tc = await ado_client.create_test_case(
                                                title=tc_title,
                                                steps=steps,
                                                priority=int(story.get("priority") or 2),
                                                iteration_path=iteration,
                                            )
                                            tc_id = _extract_int_id(tc, ("id", "workItemId"))
                                            if tc_id:
                                                created_case_ids.append(tc_id)
                                        except Exception as e:
                                            print(f"⚠️ Failed to create test case for {story_id}: {e}")

                                    if stories and not created_case_ids:
                                        print(
                                            "⚠️ No Test Cases were created from the generated stories. "
                                            "This can happen if the Test Case create API returns an unexpected shape or permissions are missing."
                                        )

                                    if created_case_ids:
                                        try:
                                            await ado_client.add_test_cases_to_suite(
                                                plan_id=plan_id,
                                                suite_id=suite_id,
                                                test_case_ids=created_case_ids,
                                            )
                                            print(
                                                f"✅ Added {len(created_case_ids)} test case(s) to suite '{suite_name}'"
                                            )
                                        except Exception as e:
                                            print(
                                                "⚠️ Created test cases but failed to add to suite (check permissions): "
                                                f"{e}"
                                            )
                            except Exception as e:
                                print(f"⚠️ Test Plan population failed (check permissions): {e}")
                    else:
                        # If the user provided an existing Test Plan ID, prefer it and
                        # skip programmatic plan creation (some orgs reject creation with TF200001).
                        plan_id: int | None = int(existing_plan_id) if existing_plan_id.isdigit() else None
                        plan_result: dict | None = None

                        if plan_id is not None:
                            print(f"ℹ️ Using existing Test Plan ID: {plan_id}")
                        else:
                            print("🧪 Creating Azure DevOps Test Plan...")
                            plan_result = await ado_client.create_test_plan(
                                name=plan_name,
                                iteration=iteration,
                                description=description,
                            )
                            context.__dict__["ado_test_plan"] = plan_result

                        if plan_result is None:
                            if plan_id is None:
                                print(
                                    "⚠️ Test Plan creation returned null. This usually means the iteration path is invalid for the project or the API call failed."
                                )
                                if iteration_paths:
                                    print("   Use one of these iteration paths:")
                                    for p in iteration_paths[:10]:
                                        print(f"   - {p}")
                        else:
                            # The MCP server may return {'text': '...'} for both success and errors.
                            # Detect common auth failures so we don't report false success.
                            if isinstance(plan_result, dict) and isinstance(plan_result.get("text"), str):
                                text_lower = plan_result["text"].lower()
                                if _looks_like_ado_error_text(plan_result["text"]):
                                    print(f"⚠️ Test Plan creation failed: {plan_result['text']}")
                                    plan_result = None
                                    if existing_plan_id.isdigit():
                                        plan_id = int(existing_plan_id)
                                        print(f"ℹ️ Using existing Test Plan ID: {plan_id}")
                                if "not authorized" in text_lower or "unauthorized" in text_lower:
                                    print(
                                        f"⚠️ Test Plan creation failed (permissions): {plan_result['text']}"
                                    )
                                    # Skip suite/case population since the plan wasn't created.
                                    plan_result = None
                                    if existing_plan_id.isdigit():
                                        plan_id = int(existing_plan_id)
                                        print(f"ℹ️ Using existing Test Plan ID: {plan_id}")

                        if plan_result is not None:
                            print("✅ Test Plan created!")

                            # Best-effort population: suite + story-based test cases.
                            plan_id = _extract_int_id(plan_result, ("id", "planId"))
                            if not plan_id:
                                # Fallback: list plans and find by name.
                                try:
                                    plans = await ado_client.call_tool(
                                        "testplan_list_test_plans",
                                        {
                                            "project": os.environ["AZURE_DEVOPS_PROJECT"],
                                            "filterActivePlans": True,
                                            "includePlanDetails": True,
                                        },
                                    )
                                    if isinstance(plans, list):
                                        for p in plans:
                                            if not isinstance(p, dict):
                                                continue
                                            # Different versions shape the response slightly.
                                            if p.get("name") == plan_name and isinstance(p.get("id"), int):
                                                plan_id = p["id"]
                                                break
                                            plan_obj = p.get("plan")
                                            if (
                                                isinstance(plan_obj, dict)
                                                and plan_obj.get("name") == plan_name
                                                and isinstance(plan_obj.get("id"), int)
                                            ):
                                                plan_id = plan_obj["id"]
                                                break
                                except Exception as e:
                                    print(f"⚠️ Could not look up plan id via listing: {e}")

                            if not plan_id:
                                if isinstance(plan_result, dict) and isinstance(plan_result.get("text"), str):
                                    print(
                                        "⚠️ Test Plan was not created (no plan id returned). "
                                        f"Response: {plan_result['text']}"
                                    )
                                else:
                                    print(
                                        "⚠️ Could not determine plan id; skipping suite/test cases. "
                                        "(The Test Plans API may be failing or returning an unexpected response shape.)"
                                    )
                            # Fallback: allow supplying an existing plan id.
                            if not plan_id and existing_plan_id.isdigit():
                                plan_id = int(existing_plan_id)
                                print(f"ℹ️ Using existing Test Plan ID: {plan_id}")

                        # If creation failed but we have an existing plan id, proceed with population.
                        if not plan_id and existing_plan_id.isdigit():
                            plan_id = int(existing_plan_id)
                            print(f"ℹ️ Using existing Test Plan ID: {plan_id}")

                        if not plan_id:
                            # No plan id and no fallback: nothing else we can do.
                            pass
                        else:
                            suite_name = f"{project_name} - MVP Regression"
                            suite_id: int | None = None
                            if existing_suite_id.isdigit():
                                suite_id = int(existing_suite_id)
                                print(f"ℹ️ Using existing Suite ID: {suite_id}")
                            else:
                                try:
                                    suite = await ado_client.create_test_suite(
                                        plan_id=plan_id,
                                        parent_suite_id=plan_id,
                                        name=suite_name,
                                    )
                                    suite_id = _extract_int_id(suite, ("id", "suiteId"))
                                    if not suite_id:
                                        print(f"⚠️ Created suite but could not read suite id: {suite}")
                                except Exception as e:
                                    print(f"⚠️ Failed to create suite (check permissions): {e}")

                            if not suite_id:
                                pass
                            else:
                                try:
                                    stories = getattr(context, "stories", None) or (context.work_items or {}).get("stories") or []
                                    created_case_ids: list[int] = []
                                    for story in stories:
                                        if not isinstance(story, dict):
                                            continue
                                        story_id = str(story.get("id") or "").strip()
                                        title = str(story.get("title") or "Story").strip()
                                        tc_title = f"{story_id}: {title}" if story_id else title

                                        ac = story.get("acceptance_criteria") or []
                                        if not isinstance(ac, list):
                                            ac = [str(ac)]
                                        steps_lines: list[str] = []
                                        n = 1
                                        for item in ac:
                                            item_s = str(item or "").replace("|", "/").strip()
                                            if not item_s:
                                                continue
                                            steps_lines.append(f"{n}. {item_s}|{item_s}")
                                            n += 1
                                        if not steps_lines:
                                            steps_lines = [
                                                f"1. Verify {title} works end-to-end|{title} behaves as specified"
                                            ]
                                        steps = "\n".join(steps_lines)

                                        try:
                                            tc = await ado_client.create_test_case(
                                                title=tc_title,
                                                steps=steps,
                                                priority=int(story.get("priority") or 2),
                                                iteration_path=iteration,
                                            )
                                            tc_id = _extract_int_id(tc, ("id", "workItemId"))
                                            if tc_id:
                                                created_case_ids.append(tc_id)
                                        except Exception as e:
                                            print(f"⚠️ Failed to create test case for {story_id}: {e}")

                                    if stories and not created_case_ids:
                                        print(
                                            "⚠️ No Test Cases were created from the generated stories. "
                                            "This can happen if the Test Case create API returns an unexpected shape or permissions are missing."
                                        )

                                    if created_case_ids:
                                        try:
                                            await ado_client.add_test_cases_to_suite(
                                                plan_id=plan_id,
                                                suite_id=suite_id,
                                                test_case_ids=created_case_ids,
                                            )
                                            print(
                                                f"✅ Added {len(created_case_ids)} test case(s) to suite '{suite_name}'"
                                            )
                                        except Exception as e:
                                            print(
                                                "⚠️ Created test cases but failed to add to suite (check permissions): "
                                                f"{e}"
                                            )
                                except Exception as e:
                                    print(f"⚠️ Test Plan population failed (check permissions): {e}")

                await ado_client.close()
            except Exception as e:
                print(f"⚠️ Azure DevOps integration failed: {e}")
                print("Continuing with pipeline...")
        
    except Exception as e:
        print(f"\n❌ Error in Business Analyst stage: {e}")
        raise
    
    # =========================================================================
    # Stage 3: Architect - Create Architecture
    # =========================================================================
    print_stage_header("Architect - System Architecture", "3️⃣")
    
    print("\n🤖 Architect is designing the system architecture...")
    
    try:
        arch_message = await architect_agent.create_architecture(context, ba_message)
        
        print_agent_output("Architecture Design", arch_message.content)
        
        # Human approval for architecture
        approval_status = hil.request_approval(arch_message)
        
        if not is_approved(approval_status):
            feedback = hil.request_feedback(
                "What changes would you like to the architecture?",
                {"current_architecture": arch_message.content}
            )
            print(f"\n📝 Feedback received: {feedback}")
            print("🔄 Refining architecture based on feedback...")
            arch_message = await architect_agent.refine_architecture(context, feedback)
            print_agent_output("Refined Architecture", arch_message.content)
            
            approval_status = hil.request_approval(arch_message)
            if not is_approved(approval_status):
                print("\n❌ Architecture not approved. Exiting pipeline.")
                return
        
        arch_message.approval_status = ApprovalStatus.APPROVED
        context.architecture = arch_message.artifacts.get("architecture", {})

        # Optional: Render Mermaid diagrams via local Mermaid MCP server
        render_mermaid = (
            _env_bool("SDLC_RENDER_MERMAID", default=False)
            if _env_bool("SDLC_NON_INTERACTIVE", default=False)
            else hil.request_confirmation(
                "Render Mermaid diagrams to image files via Mermaid MCP (local)?",
                default=False,
            )
        )

        if render_mermaid:
            # Use comprehensive extraction that looks in multiple places
            diagrams = _extract_all_diagrams(arch_message)
            # Also try from context.architecture as fallback
            if not diagrams:
                diagrams = _extract_all_diagrams({"architecture": context.architecture})
            
            if not diagrams:
                print("ℹ️ No Mermaid diagrams found in architecture output.")
                print("   (Diagrams should be in Mermaid format starting with 'graph', 'flowchart', 'sequenceDiagram', etc.)")
            else:
                print(f"\n🧩 Found {len(diagrams)} Mermaid diagram(s). Rendering via local MCP...")
                for key in diagrams:
                    print(f"   - {key}")
                output_dir = os.environ.get("SDLC_MERMAID_OUTPUT_DIR", "docs/diagrams").strip() or "docs/diagrams"
                client = MermaidMCPClient()
                try:
                    # Render diagrams one by one with validation
                    rendered = 0
                    skipped = 0
                    for key, value in diagrams.items():
                        if not isinstance(value, str):
                            skipped += 1
                            continue
                        # Sanitize the diagram before checking/rendering
                        sanitized = _sanitize_mermaid_diagram(value)
                        if not _looks_like_mermaid_diagram(sanitized):
                            print(f"   ⊘ Skipped {key} (not a valid Mermaid diagram)")
                            skipped += 1
                            continue
                        
                        # Validate before rendering
                        is_valid, result = _validate_mermaid_diagram(sanitized)
                        if not is_valid:
                            print(f"   ⊘ Skipped {key}: {result}")
                            skipped += 1
                            continue
                        
                        out_path = os.path.join(output_dir, f"{key}.png")
                        try:
                            await asyncio.wait_for(
                                client.render_mermaid_to_file(sanitized, out_path),
                                timeout=30,
                            )
                            rendered += 1
                            print(f"   ✓ Rendered {key}")
                        except BaseExceptionGroup as eg:  # Python 3.11+
                            # Try to extract the actual error and show a cleaner message
                            root_error = _get_root_exception(eg)
                            print(f"   ✗ Failed {key}: {root_error}")
                            skipped += 1
                        except Exception as e:
                            print(f"   ✗ Failed {key}: {e}")
                            skipped += 1

                    if rendered > 0:
                        print(f"✅ Rendered {rendered} Mermaid diagram(s) into {output_dir}/")
                    if skipped > 0:
                        print(f"   ({skipped} diagram(s) skipped due to syntax issues)")
                except Exception as e:
                    print(f"⚠️ Mermaid render failed (unexpected): {type(e).__name__}: {e}")
                    print("   (Continuing with pipeline...)")
        
    except Exception as e:
        print(f"\n❌ Error in Architect stage: {e}")
        raise
    
    # =========================================================================
    # Stage 4: Developer - Generate Code
    # =========================================================================
    print_stage_header("Developer - Code Generation", "4️⃣")
    
    print("\n🤖 Developer is generating the code...")
    
    try:
        dev_message = await developer_agent.generate_code(context, arch_message)
        
        print_agent_output("Generated Code", dev_message.content)
        
        # Human approval for code
        approval_status = hil.request_approval(dev_message)
        
        if not is_approved(approval_status):
            print("\n❌ Code not approved. You may want to manually adjust.")
        else:
            dev_message.approval_status = ApprovalStatus.APPROVED
        
        # Ask about pushing to GitHub
        push_to_github = (
            _env_bool("SDLC_PUSH_TO_GITHUB", default=False)
            if _env_bool("SDLC_NON_INTERACTIVE", default=False)
            else hil.request_confirmation(
                "Would you like to push this code to GitHub?",
                default=False,
            )
        )
        
        if push_to_github:
            if _env_bool("SDLC_NON_INTERACTIVE", default=False):
                repo_name = (os.environ.get("SDLC_GITHUB_REPO_NAME") or project_name).strip() or project_name
                repo_owner = (os.environ.get("SDLC_GITHUB_OWNER") or os.environ.get("GITHUB_OWNER") or "").strip()
                if not repo_owner:
                    print("⚠️ Missing GITHUB_OWNER/SDLC_GITHUB_OWNER; skipping GitHub push.")
                    push_to_github = False
            else:
                repo_name = input("\nEnter GitHub repository name: ").strip()
                if not repo_name:
                    repo_name = project_name

                repo_owner = os.environ.get("GITHUB_OWNER", "")
                if not repo_owner:
                    repo_owner = input("Enter GitHub repository owner/username: ").strip()

        if push_to_github:
            print("\n🔧 Connecting to GitHub MCP Server (stdio)...")
            try:
                github_client = GitHubMCPClient(
                    mcp_url=os.environ.get("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/"),
                    github_token=os.environ.get("GITHUB_TOKEN"),
                )
                await github_client.connect()
                developer_agent.set_github_client(github_client)

                # Ensure repository exists (create if missing). The MCP tool will
                # create under the authenticated user by default; if you want an org,
                # set GITHUB_ORGANIZATION.
                try:
                    create_args: dict[str, object] = {
                        "name": repo_name,
                        "private": False,
                        # Important: initialize with a README so the repo has a
                        # default branch/commit and isn't "empty".
                        "autoInit": True,
                        "description": f"Auto-generated by SDLC pipeline for {project_name}",
                    }
                    org = os.environ.get("GITHUB_ORGANIZATION")
                    if org:
                        create_args["organization"] = org
                    await github_client.call_tool("create_repository", create_args)
                except Exception as e:
                    # If it already exists, keep going; otherwise surface the warning.
                    msg = str(e).lower()
                    if "already exists" not in msg and "name already exists" not in msg:
                        print(f"⚠️ Could not create repository (may already exist): {e}")
                
                print(f"📤 Pushing code to GitHub repository: {repo_owner}/{repo_name}...")
                # Optionally create PR (if yes, push changes to the feature branch, not main)
                create_pr = (
                    _env_bool("SDLC_CREATE_PR", default=False)
                    if _env_bool("SDLC_NON_INTERACTIVE", default=False)
                    else hil.request_confirmation(
                        "Would you like to create a Pull Request?",
                        default=False,
                    )
                )

                feature_branch: str | None = None
                target_branch = "main"
                if create_pr:
                    if _env_bool("SDLC_NON_INTERACTIVE", default=False):
                        feature_branch = (os.environ.get("SDLC_PR_BRANCH") or "feature/auto-gen").strip() or "feature/auto-gen"
                    else:
                        feature_branch = input("Enter feature branch name (or press Enter for 'feature/auto-gen'): ").strip()
                        if not feature_branch:
                            feature_branch = "feature/auto-gen"
                    target_branch = feature_branch

                    # Best-effort: ensure the feature branch exists before pushing.
                    # If the MCP server doesn't support this tool or it already exists, we proceed.
                    try:
                        await github_client.call_tool(
                            "create_branch",
                            {
                                "owner": repo_owner,
                                "repo": repo_name,
                                "branch": feature_branch,
                                "from_branch": "main",
                            },
                        )
                    except Exception as e:
                        msg = str(e)
                        if "already exists" not in msg.lower():
                            print(f"⚠️ Could not pre-create branch (may already exist/unsupported): {e}")

                result = await developer_agent.push_to_github(
                    context=context,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    branch=target_branch,
                    commit_message=f"feat: Initial {project_name} implementation",
                )
                if result.get("error"):
                    print(f"⚠️ Push had issues: {result['error']}")
                else:
                    print(
                        f"✅ Code pushed to GitHub ({target_branch})! {result.get('files_pushed', 0)} files committed."
                    )

                if create_pr and feature_branch:
                    pr_result = await developer_agent.create_pull_request(
                        context=context,
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        head_branch=feature_branch,
                        base_branch="main",
                    )
                    if pr_result.get("error"):
                        print(f"⚠️ PR creation failed: {pr_result['error']}")
                    else:
                        pr_number = pr_result.get("pr_number")
                        pr_url = pr_result.get("pr_url")
                        print(f"✅ Pull Request created! #{pr_number}")
                        print(f"   URL: {pr_url}")
                        if not pr_number or not pr_url:
                            print(f"   Raw: {pr_result.get('raw') or pr_result}")
                
                await github_client.close()
            except Exception as e:
                print(f"⚠️ Could not push to GitHub: {e}")
        
    except Exception as e:
        print(f"\n❌ Error in Developer stage: {e}")
        raise
    
    # =========================================================================
    # Pipeline Complete
    # =========================================================================
    print("\n" + "=" * 70)
    print("🎉 SDLC Pipeline Complete!")
    print("=" * 70)
    
    print("\n📊 Pipeline Summary:")
    print(f"  • Project: {project_name}")
    print(f"  • Requirements: {'✅ Generated' if context.requirements else '❌ Not generated'}")
    print(f"  • Work Items: {'✅ Created' if context.work_items else '❌ Not created'}")
    print(f"  • Architecture: {'✅ Designed' if context.architecture else '❌ Not designed'}")
    print(f"  • Code: {'✅ Generated' if context.code_artifacts else '❌ Not generated'}")
    
    print("\n" + "=" * 70)


if __name__ == "__main__":
    try:
        asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        print("\n\n⚠️ Pipeline interrupted by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Pipeline failed: {e}")
        sys.exit(1)
