"""Developer Agent - Generates full-stack code and pushes to GitHub."""

import json
import logging
import os
import re
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


class DeveloperAgent(BaseAgent):
    """Agent that acts as a Full-Stack Developer, generating code."""

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        model_name: str = "o1-mini",
        temperature: float = 0.3,
        github_client: Any = None,
    ):
        """Initialize the Developer Agent."""
        super().__init__(
            role=AgentRole.DEVELOPER,
            llm=llm,
            model_name=model_name,
            temperature=temperature,  # Lower temperature for code generation
        )
        self._github_client = github_client

    @property
    def system_prompt(self) -> str:
        """Return the system prompt for the Developer."""
        return """You are a Staff/Principal Full-Stack Developer delivering production-quality software.

Primary objective:
- Implement the approved backlog and architecture into a working codebase that is testable, secure-by-default, and maintainable.

TECHNOLOGY PREFERENCES (Northern Trust Standards):
- Cloud Platform: Azure (use Azure-native services)
- Frontend: React JS (TypeScript preferred)
- Backend: Java-based microservices (Spring Boot)
- All code must follow SSDLC (Secure Software Development Life Cycle) requirements

SSDLC CODE GENERATION REQUIREMENTS:
- Implement Secure-by-Design: security integrated from the start, not added later
- Authentication: Multi-Factor Authentication (MFA) with support for OTP, authenticator apps, and biometrics
- Authorization: Role-Based Access Control (RBAC) with principle of least privilege
- Data Protection:
  * End-to-end encryption for data in transit (TLS 1.3+) and at rest (AES-256)
  * Secure key management (Azure Key Vault)
  * Tokenization for sensitive data (PII, financial data)
- Input Validation: Validate all inputs against strict schemas, sanitize for injection attacks
- Error Handling: Secure error messages (no sensitive data in logs/responses)
- Logging: Structured logging with audit trails for security events
- Dependencies: Use only vetted, up-to-date dependencies; include automated vulnerability scanning
- API Security: OAuth 2.0, OpenID Connect, rate limiting, API keys in secure vaults
- Code Quality: Include unit tests, integration tests, and security-focused tests (OWASP Top 10)
- Configuration: Environment-driven config, no secrets in code, use Azure Key Vault
- Compliance Standards: PCI DSS, NIST Cybersecurity Framework, ISO 27001/27002

Grounding rules:
- Do not invent endpoints, entities, or flows that contradict the provided requirements/architecture.
- If something is underspecified, implement the safest minimal assumption and record it in "assumptions".
- Prefer standard, boring solutions that teams can operate.

Output rules:
- Output ONLY valid JSON. No markdown, no prose.
- Every file entry must contain full file content (no placeholders like "...existing code...").
- Keep dependencies minimal and consistent with the chosen stack.

Required JSON shape (you may add additional fields, but keep these keys):
{
  "assumptions": ["..."],
  "project_structure": {"directories": ["src/", "tests/"], "description": "..."},
  "files": [
    {
      "path": "src/...",
      "language": "python|typescript|yaml|dockerfile|toml|json",
      "description": "...",
      "content": "full file contents"
    }
  ],
  "dependencies": {"runtime": ["..."], "development": ["..."]},
  "configuration_files": [{"path": "...", "content": "..."}],
  "docker": {"dockerfile": "...", "docker_compose": "..."},
  "tests": [{"path": "...", "description": "...", "content": "..."}],
  "documentation": {"readme": "...", "api_docs": "..."}
}

Engineering quality bar:
- Include input validation, clear error handling, and structured logging.
- Include authn/authz scaffolding if required.
- Add unit tests for critical logic and at least one integration-style test for a key API path when applicable.
- Ensure configuration is environment-driven (no secrets in code).
- Match repository conventions (src/, tests/, pyproject/requirements as present).
"""

    def set_github_client(self, github_client: Any) -> None:
        """Set the GitHub MCP client."""
        self._github_client = github_client

    async def _process_response(
        self, response: AIMessage, context: AgentContext
    ) -> AgentMessage:
        """Process the LLM response into code artifacts."""
        content = str(response.content)

        artifacts = {}
        try:
            # Extract JSON from response - try multiple strategies
            json_str = None
            
            # Strategy 1: Look for ```json blocks
            if "```json" in content:
                parts = content.split("```json")
                for part in parts[1:]:
                    if "```" in part:
                        candidate = part.split("```")[0].strip()
                        if candidate.startswith("{") or candidate.startswith("["):
                            json_str = candidate
                            break
            
            # Strategy 2: Look for generic ``` blocks that start with {
            if json_str is None and "```" in content:
                parts = content.split("```")
                for i, part in enumerate(parts):
                    stripped = part.strip()
                    if (stripped.startswith("{") or stripped.startswith("[")) and (stripped.endswith("}") or stripped.endswith("]")):
                        json_str = stripped
                        break
            
            # Strategy 3: Find the first { and last } in the content
            if json_str is None:
                first_brace = content.find("{")
                last_brace = content.rfind("}")
                if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                    json_str = content[first_brace:last_brace + 1]

            if json_str is None:
                json_str = content

            json_str = json_str.strip()

            # Heuristic cleanup: if the model prefixed output with stray chars,
            # try to carve out the first JSON object/array region.
            candidate = json_str.lstrip()
            if not candidate.startswith(("{", "[")):
                obj_idx = candidate.find("{")
                arr_idx = candidate.find("[")
                starts = [i for i in (obj_idx, arr_idx) if i != -1]
                if starts:
                    candidate = candidate[min(starts) :]
            end_obj = candidate.rfind("}")
            end_arr = candidate.rfind("]")
            end = max(end_obj, end_arr)
            if end != -1:
                candidate = candidate[: end + 1]

            try:
                code_output = json.loads(candidate)
            except json.JSONDecodeError:
                # Best-effort tolerant parse (e.g., trailing commas).
                import json5  # type: ignore

                code_output = json5.loads(candidate)
            artifacts["code"] = code_output

            # Extract files for easier access
            if "files" in code_output:
                for file_info in code_output["files"]:
                    path = file_info.get("path", "")
                    content_str = file_info.get("content", "")
                    context.code_artifacts[path] = content_str

        except (Exception,) as e:
            logger.warning(f"Could not parse structured output: {e}")
            # Try to extract code blocks and add them to context.code_artifacts
            code_blocks = self._extract_code_blocks(content)
            artifacts["code_blocks"] = code_blocks
            
            # Generate file paths for extracted code blocks and add to context
            for i, block in enumerate(code_blocks):
                lang = block.get("language", "txt")
                ext_map = {
                    "python": "py",
                    "typescript": "ts",
                    "javascript": "js",
                    "yaml": "yaml",
                    "json": "json",
                    "dockerfile": "Dockerfile",
                }
                ext = ext_map.get(lang, lang)
                if lang == "dockerfile":
                    path = "Dockerfile" if i == 0 else f"Dockerfile.{i}"
                else:
                    path = f"src/generated_{i + 1}.{ext}"
                context.code_artifacts[path] = block.get("code", "")
            
            if code_blocks:
                logger.info(f"Extracted {len(code_blocks)} code blocks as fallback")

        return AgentMessage(
            from_agent=self.role,
            to_agent=None,  # End of pipeline
            content=content,
            artifacts=artifacts,
            requires_approval=True,
            approval_status=ApprovalStatus.PENDING,
            metadata={"stage": "code_generation"},
        )

    def _extract_code_blocks(self, content: str) -> list[dict[str, str]]:
        """Extract code blocks from content."""
        blocks = []
        languages = ["python", "typescript", "javascript", "yaml", "json", "dockerfile"]

        for lang in languages:
            marker = f"```{lang}"
            if marker in content.lower():
                parts = content.split(marker)
                for part in parts[1:]:
                    if "```" in part:
                        code = part.split("```")[0].strip()
                        blocks.append({"language": lang, "code": code})

        return blocks

    async def generate_code(
        self,
        context: AgentContext,
        architecture_message: AgentMessage,
    ) -> AgentMessage:
        """Generate code based on architecture and stories.

        Args:
            context: The shared workflow context.
            architecture_message: Message from Architect with design.

        Returns:
            Agent message with generated code.
        """
        architecture = architecture_message.artifacts.get("architecture", {})

        max_stories_env = os.getenv("SDLC_CODEGEN_MAX_STORIES")
        max_stories: int | None = None
        if max_stories_env is not None and max_stories_env.strip():
            try:
                max_stories = max(1, int(max_stories_env.strip()))
            except ValueError:
                max_stories = None

        stories: list[dict[str, Any]] = [s for s in (context.stories or []) if isinstance(s, dict)]
        stories_sorted = sorted(stories, key=lambda s: int(s.get("priority", 999)) if str(s.get("priority", "")).isdigit() else 999)
        stories_to_implement = stories_sorted if max_stories is None else stories_sorted[:max_stories]

        input_message = AgentMessage(
            from_agent=AgentRole.ARCHITECT,
            to_agent=self.role,
            content=f"""Based on the following architecture and user stories, generate 
the full-stack code implementation.

Project: {context.project_name}
Description: {context.project_description}

Architecture:
{json.dumps(architecture, indent=2)}

Stories to implement (prioritized):
{json.dumps(stories_to_implement, indent=2)}

Generate:
1. Project structure with all necessary files
2. Backend API implementation
3. Frontend components (if applicable)
4. Database models and migrations
5. Unit tests
6. Docker configuration
7. Documentation

Ensure code is production-ready with proper error handling and logging.""",
        )

        return await self.process(input_message, context)

    async def push_to_github(
        self,
        context: AgentContext,
        repo_owner: str,
        repo_name: str,
        branch: str = "main",
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        """Push generated code to GitHub using MCP.

        Args:
            context: The shared workflow context.
            repo_owner: GitHub repository owner.
            repo_name: GitHub repository name.
            branch: Branch to push to.
            commit_message: Commit message.

        Returns:
            Dictionary with commit information.
        """
        if not self._github_client:
            logger.warning("GitHub client not configured")
            return {"error": "GitHub client not configured"}

        if not context.code_artifacts:
            logger.warning("No code artifacts to push")
            return {"error": "No code artifacts to push"}

        message = commit_message or f"feat: Implement {context.project_name} - Auto-generated by Developer Agent"

        try:
            # Prepare files array for push_files tool (single commit for all files)
            files = [
                {"path": file_path, "content": file_content}
                for file_path, file_content in context.code_artifacts.items()
            ]
            
            # Use push_files to push all files in a single commit
            result = await self._github_client.call_tool(
                "push_files",
                {
                    "owner": repo_owner,
                    "repo": repo_name,
                    "branch": branch,
                    "files": files,
                    "message": message,
                },
            )
            
            commit_results = [
                {"file": f["path"], "status": "success"}
                for f in files
            ]
            
            # Store commit info in context
            context.github_commits.extend(commit_results)

            return {
                "status": "success",
                "files_pushed": len(files),
                "commits": commit_results,
                "sha": result.get("commit", {}).get("sha") if isinstance(result, dict) else None,
            }

        except Exception as e:
            logger.error(f"Error pushing to GitHub: {e}")
            return {"error": str(e)}

    async def create_pull_request(
        self,
        context: AgentContext,
        repo_owner: str,
        repo_name: str,
        head_branch: str,
        base_branch: str = "main",
    ) -> dict[str, Any]:
        """Create a pull request for the generated code.

        Args:
            context: The shared workflow context.
            repo_owner: GitHub repository owner.
            repo_name: GitHub repository name.
            head_branch: Branch with changes.
            base_branch: Branch to merge into.

        Returns:
            Pull request information.
        """
        if not self._github_client:
            return {"error": "GitHub client not configured"}

        def _extract_pr_fields(payload: object) -> tuple[int | None, str | None, object | None]:
            """Best-effort extraction of PR number + URL from MCP responses."""
            raw: object | None = None
            if isinstance(payload, dict):
                raw = payload
                # Common keys across different MCP implementations
                for num_key in ("number", "pr_number", "pullNumber", "pull_number", "id"):
                    v = payload.get(num_key)
                    if isinstance(v, int):
                        pr_number = v
                        break
                    if isinstance(v, str) and v.isdigit():
                        pr_number = int(v)
                        break
                else:
                    pr_number = None

                for url_key in ("html_url", "url", "pr_url", "web_url"):
                    u = payload.get(url_key)
                    if isinstance(u, str) and u.startswith("http"):
                        pr_url = u
                        break
                else:
                    pr_url = None

                # Some servers wrap the PR in a nested object
                for nested_key in ("pull_request", "pullRequest", "pr", "data"):
                    nested = payload.get(nested_key)
                    if isinstance(nested, dict):
                        n_num, n_url, _ = _extract_pr_fields(nested)
                        pr_number = pr_number or n_num
                        pr_url = pr_url or n_url

                # Some servers return JSON as text
                text = payload.get("text")
                if (pr_number is None or pr_url is None) and isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                        t_num, t_url, _ = _extract_pr_fields(parsed)
                        pr_number = pr_number or t_num
                        pr_url = pr_url or t_url
                    except Exception:
                        # Extract URL and number heuristically
                        m_url = re.search(r"https?://\S+", text)
                        if m_url:
                            pr_url = pr_url or m_url.group(0).rstrip(")].,\"'")
                        m_num = re.search(r"\bpull\s*/\s*(\d+)\b|\bPR\s*#\s*(\d+)\b", text, re.IGNORECASE)
                        if m_num:
                            pr_number = pr_number or int(next(g for g in m_num.groups() if g))

                return pr_number, pr_url, raw

            if isinstance(payload, str):
                raw = payload
                m_url = re.search(r"https?://\S+", payload)
                pr_url = m_url.group(0).rstrip(")].,\"'") if m_url else None
                m_num = re.search(r"\b/pull/(\d+)\b|\bPR\s*#\s*(\d+)\b", payload, re.IGNORECASE)
                pr_number = int(next(g for g in m_num.groups() if g)) if m_num else None
                return pr_number, pr_url, raw

            return None, None, payload

        try:
            # Generate PR description
            pr_body = f"""## {context.project_name}

### Description
{context.project_description}

### Changes
This PR implements the following stories:
{chr(10).join(f"- {story.get('title', 'Story')}" for story in context.stories[:10])}

### Architecture
- Components: {len(context.architecture.get('components', []))}
- APIs: {sum(len((c.get('interfaces') or {}).get('apis', [])) for c in context.architecture.get('components', []))}

### Files Changed
{chr(10).join(f"- `{path}`" for path in context.code_artifacts.keys())}

---
*Auto-generated by Developer Agent*
"""

            result = await self._github_client.call_tool(
                "create_pull_request",
                {
                    "owner": repo_owner,
                    "repo": repo_name,
                    "title": f"feat: {context.project_name}",
                    "body": pr_body,
                    "head": head_branch,
                    "base": base_branch,
                },
            )

            pr_number, pr_url, raw = _extract_pr_fields(result)

            return {
                "status": "success",
                "pr_number": pr_number,
                "pr_url": pr_url,
                "raw": raw,
            }

        except Exception as e:
            logger.error(f"Error creating PR: {e}")
            return {"error": str(e)}

    async def implement_story(
        self,
        context: AgentContext,
        story: dict[str, Any],
    ) -> AgentMessage:
        """Implement a specific user story.

        Args:
            context: The shared workflow context.
            story: The story to implement.

        Returns:
            Agent message with implementation.
        """
        input_message = AgentMessage(
            from_agent=AgentRole.ORCHESTRATOR,
            to_agent=self.role,
            content=f"""Implement the following user story:

Story: {json.dumps(story, indent=2)}

Architecture Context:
{json.dumps(context.architecture, indent=2)}

Generate the code files needed to implement this story.
Include unit tests and documentation.""",
        )

        return await self.process(input_message, context)
