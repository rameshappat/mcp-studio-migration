# Platform Architecture & Design (pythonmcpproject)

## 1. Purpose
This document describes the platform architecture, major components, runtime flows, and third‑party integrations for this repository.

The repo supports **two primary execution modes**:
1. **Interactive GitHub MCP Agent**: `python src/main.py`
2. **Multi‑agent SDLC pipeline** (Product → BA → Architect → Developer): `python run_sdlc_pipeline.py`

---

## 2. System Overview

### 2.1 High-level Concept
This is an **LLM-driven orchestration layer** that:
- Uses **LangGraph** (for the GitHub agent) and a custom orchestrator (for the SDLC pipeline)
- Calls external systems via **MCP servers** (GitHub over HTTP; Azure DevOps over stdio)
- Emits **observability traces** to **LangSmith** when enabled

### 2.2 Key Non-Functional Priorities
- **Determinism where possible**: tool calling boundaries, explicit step gating, capped tool loops
- **Operational clarity**: logs + optional traces
- **Safe-by-default**: human approval gates in SDLC pipeline; scripts default to dry-run

### 2.3 LLM Configuration
The platform supports multiple LLM providers with flexible configuration:
- **Default Provider**: `claude-opus-4-20250514` (Anthropic Claude Opus 4)
- **Fallback**: OpenAI GPT-4 Turbo if Anthropic key not available
- **Per-role overrides** supported via environment variables

---

## 3. Component Model

### 3.1 Components (Runtime)

**CLI Entrypoints**
- `src/main.py`: interactive GitHub agent loop
- `run_sdlc_pipeline.py`: interactive SDLC pipeline runner (approvals, prompts)

**Agent Layer**
- `src/agents/github_agent.py`: LangGraph-based tool-calling agent for GitHub MCP
- `src/agents/sdlc_pipeline.py`: pipeline orchestrator coordinating multiple role agents
- `src/agents/*_agent.py`: Product Manager, Business Analyst, Architect, Developer
- `src/agents/human_in_loop.py`: human approval/feedback/selection/confirmation abstraction

**MCP Client Layer**
- `src/mcp_client/github_client.py`: GitHub MCP client using StreamableHTTP
- `src/mcp_client/ado_client.py`: Azure DevOps MCP client using stdio (`npx @azure-devops/mcp`) + REST fallbacks
- `src/mcp_client/mermaid_client.py`: Mermaid MCP client via stdio (wrapper to keep stdout clean)
- `src/mcp_client/tool_converter.py`: converts MCP tool schemas into LangChain tools

**Observability Layer**
- `src/observability/langsmith_setup.py`: LangSmith wiring (`LANGSMITH_*`)

**Utility / Ops Scripts**
- `scripts/delete_all_work_items.py`: repository cleanup script (dry-run by default, supports `--exclude-ids`)
- `scripts/populate_test_plan_from_work_items.py`: generates Test Cases from existing work items and adds them to a suite
- `scripts/clear_langsmith_traces.py`: clears LangSmith traces for a project (dry-run by default)

### 3.2 Agent Roles & Responsibilities

| Agent | Role | Output | Demo Constraints |
|-------|------|--------|------------------|
| **ProductManagerAgent** | Generates PRD with requirements | JSON with product vision, requirements, success metrics | 5-7 requirements max |
| **BusinessAnalystAgent** | Creates Epics, Stories, Tasks | JSON backlog structure | 3-5 Epics, 2-3 Stories per Epic |
| **ArchitectAgent** | Designs system architecture | JSON with components, diagrams, tech stack | No constraints (full detail) |
| **DeveloperAgent** | Generates implementation code | JSON with files, dependencies, Docker config | Implements from limited stories |

---

## 4. Architecture Diagrams

### 4.1 C4-ish Container Diagram
```mermaid
flowchart LR
  user([User])

  subgraph repo[pythonmcpproject]
    main_cli["Interactive CLI\n(src/main.py)"]
    sdlc_cli["SDLC Runner\n(run_sdlc_pipeline.py)"]

    subgraph agents[Agents]
      gha["GitHubAgent\n(LangGraph)"]
      pm["ProductManagerAgent"]
      ba["BusinessAnalystAgent"]
      arch["ArchitectAgent"]
      dev["DeveloperAgent"]
      hitl["HumanInTheLoop"]
      pipe["SDLCPipelineOrchestrator"]
    end

    subgraph clients[MCP & REST Clients]
      gh_mcp["GitHubMCPClient\n(StreamableHTTP)"]
      ado_mcp["AzureDevOpsMCPClient\n(stdio npx)"]
      mer_mcp["MermaidMCPClient\n(stdio node wrapper)"]
    end

    obs["LangSmith Setup"]
  end

  gh[(GitHub MCP Server\nhttps://api.githubcopilot.com/mcp/)]
  ado[(Azure DevOps MCP\n@npx @azure-devops/mcp)]
  ado_rest[(Azure DevOps REST API)]
  mermaid[(Mermaid MCP Server / node)]
  langsmith[(LangSmith)]

  user --> main_cli --> gha
  gha --> gh_mcp --> gh

  user --> sdlc_cli --> pipe
  pipe --> hitl --> user
  pipe --> pm
  pipe --> ba
  pipe --> arch
  pipe --> dev

  ba --> ado_mcp --> ado
  dev --> gh_mcp
  pipe --> mer_mcp --> mermaid
  ado_mcp -. REST fallback .-> ado_rest

  main_cli --> obs --> langsmith
  sdlc_cli --> obs --> langsmith
```

### 4.2 SDLC Pipeline Sequence (Happy Path)
```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant R as run_sdlc_pipeline.py
  participant P as SDLCPipelineOrchestrator
  participant PM as ProductManagerAgent
  participant BA as BusinessAnalystAgent
  participant A as ArchitectAgent
  participant D as DeveloperAgent
  participant ADO as AzureDevOpsMCPClient
  participant GH as GitHubMCPClient
  participant M as MermaidMCPClient

  U->>R: Provide project name + idea
  R->>P: run(project_idea, project_name)

  P->>PM: generate_requirements()
  PM-->>P: requirements artifact
  P->>U: approval prompt (requirements)

  P->>BA: create_work_items()
  BA-->>P: epics/stories/work items
  P->>U: approval prompt (work items)

  P->>ADO: create/push items (optional)
  ADO-->>P: ADO item ids / status

  P->>U: confirmation (create test plan?)
  P->>ADO: create_test_plan + suite + test cases (optional)

  P->>A: create_architecture()
  A-->>P: JSON architecture + Mermaid
  P->>U: approval prompt (architecture)

  P->>M: render Mermaid (optional)
  M-->>P: image paths

  P->>D: generate_code()
  D-->>P: code artifacts
  P->>U: approval prompt (development)

  P->>GH: push code (optional)
  GH-->>P: commit/pr info
  P-->>R: completed
```

### 4.3 GitHub Agent Tool Loop (LangGraph) - Detailed Flow

This diagram shows the LangGraph-based GitHub agent architecture with clear separation between orchestration, LLM decision-making, and MCP tool execution.

```mermaid
flowchart TD
  start([User Request:<br/>Create repo with code])
  
  subgraph LangGraph["LangGraph State Machine"]
    agent["GitHub Agent<br/>(Orchestrator)"]
    llm["LLM Decision Engine<br/>(GPT-4o/Claude)<br/>Selects which tools to use"]
    state["State Management<br/>- messages<br/>- tool_calls_count<br/>- max_tool_calls: 10"]
  end
  
  subgraph MCP["MCP Tool Layer"]
    toolnode["ToolNode<br/>(Executes selected tools)"]
    
    subgraph GitHubTools["GitHub MCP Tools"]
      t1["create_repository"]
      t2["create_branch"]
      t3["create_or_update_file"]
      t4["push_files"]
      t5["create_pull_request"]
    end
  end
  
  subgraph External["External Systems"]
    github["GitHub MCP Server<br/>api.githubcopilot.com/mcp/"]
    ghapi["GitHub API"]
  end
  
  start --> agent
  agent --> state
  state --> llm
  
  llm -->|"Tool calls selected<br/>(e.g., create_repository)"| toolnode
  llm -->|"No tool calls needed"| answer
  
  toolnode --> t1
  toolnode --> t2
  toolnode --> t3
  toolnode --> t4
  toolnode --> t5
  
  t1 -->|"HTTP request"| github
  t2 -->|"HTTP request"| github
  t3 -->|"HTTP request"| github
  t4 -->|"HTTP request"| github
  t5 -->|"HTTP request"| github
  
  github --> ghapi
  ghapi -->|"API response"| github
  github -->|"Tool results"| toolnode
  
  toolnode -->|"Results added to state"| state
  state -->|"Loop continues<br/>(if count < 10)"| llm
  state -->|"Max calls reached"| answer
  
  answer([Final Answer:<br/>Repo created, code pushed,<br/>PR opened])
  
  style LangGraph fill:#e1f5ff
  style MCP fill:#fff4e1
  style External fill:#f0f0f0
  style llm fill:#ffeb99
  style toolnode fill:#ffd699
```

**Detailed Flow Explanation**:

1. **User Input**: User asks to create a repository with code (e.g., "Create a Spring Boot project with MFA")

2. **GitHub Agent (Orchestrator)**: 
   - Receives user request
   - Initializes LangGraph state machine
   - Manages conversation context and tool call counter

3. **LLM Decision Engine**: 
   - Analyzes current state and user intent
   - Has access to GitHub MCP tool schemas
   - Decides which tools to call and in what order
   - Example sequence: `create_repository` → `create_branch` → `push_files` → `create_pull_request`

4. **State Management**:
   - Tracks conversation messages
   - Counts tool calls (max 10 to prevent infinite loops)
   - Stores tool results for context in next LLM call

5. **ToolNode Execution**:
   - Receives tool calls from LLM
   - Invokes appropriate GitHub MCP tools
   - Returns results to state

6. **GitHub MCP Tools** (Examples):
   - `create_repository`: Creates new GitHub repo
   - `create_branch`: Creates feature branch from main
   - `create_or_update_file`: Writes individual files (e.g., README.md, pom.xml)
   - `push_files`: Batch pushes multiple files in single commit
   - `create_pull_request`: Opens PR from feature branch to main

7. **GitHub MCP Server**:
   - StreamableHTTP transport (HTTPS)
   - Translates MCP calls to GitHub API calls
   - Handles authentication via `GITHUB_TOKEN`

8. **Loop Continuation**:
   - After each tool execution, results are added to state
   - LLM decides: "Do I need more tools?" or "Is the task complete?"
   - Continues until task is done OR max 10 tool calls reached

9. **Final Answer**: Agent provides summary (e.g., "Repository created at github.com/user/repo, code pushed, PR #1 opened")

**Example Multi-Step Flow**:
```
User: "Create a Spring Boot repo with MFA code"

Step 1: LLM → create_repository(name="spring-mfa-demo")
        Result: Repo created

Step 2: LLM → create_branch(branch="feature/mfa-setup")
        Result: Branch created from main

Step 3: LLM → push_files([
          {path: "pom.xml", content: "..."},
          {path: "src/main/java/Application.java", content: "..."},
          {path: "src/main/resources/application.yml", content: "..."}
        ])
        Result: 3 files committed

Step 4: LLM → create_pull_request(
          title="Add MFA implementation",
          body="Implements multi-factor authentication...",
          base="main",
          head="feature/mfa-setup"
        )
        Result: PR #1 opened

Step 5: LLM → No more tools needed
        Return: "Repository created with MFA code. PR #1 is ready for review."
```

**Key Architectural Benefits**:
- **Autonomous Planning**: LLM adapts to errors (e.g., if repo exists, skip creation)
- **Context Awareness**: Each tool result informs next decision
- **Safety Bounds**: Max 10 calls prevents runaway loops
- **Error Recovery**: LLM can try alternative approaches if a tool fails

---

## 5. Key Flows & Data

### 5.1 SDLC Pipeline State
The SDLC pipeline uses a shared `AgentContext` plus a `PipelineState`:
- `AgentContext`: requirements, epics/stories, architecture JSON, code artifacts, ADO/GitHub outputs
- `PipelineState`: stage transitions, messages, errors, revision counts

Human approvals and revision loops are implemented by:
- `HumanInTheLoop.request_approval()`
- `SDLCPipelineOrchestrator` revision counters + feedback prompts

### 5.2 GitHub Agent State
The GitHub agent maintains a LangGraph state:
- `messages`: rolling conversation messages
- `tool_calls_count` / `max_tool_calls`: limits tool recursion

---

## 6. Integrations & MCP Architecture

### 6.1 Model Context Protocol (MCP) - Architectural Decision

This platform demonstrates **production-grade MCP integration patterns** for enterprise AI systems. MCP (Model Context Protocol) is GitHub's standard for connecting LLMs to external tools and data sources.

#### Why MCP?
1. **Standardization**: Industry-standard protocol vs. custom API wrappers
2. **Tool Discovery**: LLMs automatically discover available tools and their schemas
3. **Type Safety**: JSON Schema validation for inputs/outputs
4. **Composability**: Mix-and-match MCP servers from different vendors
5. **Future-Proof**: As MCP ecosystem grows, new capabilities become available without code changes

#### MCP Server Integration Patterns

We implement **three distinct MCP integration patterns**, each optimized for different use cases:

##### Pattern 1: LangGraph + MCP (GitHub Agent)
**Use Case**: Dynamic, LLM-driven tool selection and multi-step reasoning

**Architecture**:
```
User Input → LangGraph State Machine → LLM decides which tools → MCP Tools → External API
                ↑                                                      ↓
                └──────────── Tool Results feed back ────────────────┘
```

**Why LangGraph for GitHub?**
- **Complex reasoning**: Creating repos, branches, files, PRs requires multi-step planning
- **Error recovery**: LLM can adjust strategy if a tool call fails
- **Conditional logic**: "If repo exists, create branch; if not, create repo first"
- **Tool chaining**: Results from one tool inform the next tool selection

**Implementation**: `src/agents/github_agent.py`
- LangGraph StateGraph with tool-calling loop
- Max 10 tool calls to prevent infinite loops
- StreamableHTTP transport to GitHub MCP Server
- LLM autonomously decides: create_repo → push_files → create_pull_request

##### Pattern 2: Direct MCP (Azure DevOps - Deterministic)
**Use Case**: Deterministic, code-driven tool execution with predictable workflows

**Architecture**:
```
Business Logic → Direct MCP Client Call → stdio MCP Server → Azure DevOps API
```

**Why Direct Calls for ADO?**
- **Determinism**: Creating Epics → Stories → Tasks follows a fixed structure
- **Performance**: No LLM invocations = faster + cheaper (no token costs for tool selection)
- **Reliability**: Code-driven flow eliminates LLM hallucination risk
- **Auditability**: Explicit code paths easier to debug and trace
- **Cost Control**: LangGraph loops = multiple LLM calls; direct calls = zero

**Implementation**: `src/mcp_client/ado_client.py`
- stdio transport via `npx @azure-devops/mcp`
- Direct method calls: `create_work_item()`, `link_work_items()`, `create_test_plan()`
- Business Analyst agent calls MCP client directly, no LLM in the loop

##### Pattern 3: Hybrid MCP + REST API Fallback (ADO Test Plans)
**Use Case**: Resilient integration with fallback for known MCP bugs

**Architecture**:
```
Code → Try MCP Tool → Success? → Return
           ↓
       Detect Error
           ↓
    Fallback to REST API → Success
```

**Why Hybrid for Test Plans?**
- **Known Bug**: `@azure-devops/mcp` has `TF200001` bug (empty projectName parameter)
- **Graceful Degradation**: MCP tried first (forward compatibility when bug is fixed)
- **Guaranteed Success**: REST API fallback ensures operation completes
- **User Transparency**: Logs warning when fallback is used
- **No User Impact**: Automatic fallback is invisible to end users

**Implementation**: `src/mcp_client/ado_client.py` - `create_test_plan()` method
```python
# Try MCP first
result = await self._call_mcp_tool("testplan_create_test_plan", args)

# Detect TF200001 error
if "tf200001" in result and "empty" in result:
    logger.warning("MCP failed, falling back to REST API")
    return await self._create_test_plan_via_rest(...)
```

### 6.2 LLM Providers
- **OpenAI GPT-4o** (recommended for demos) via `langchain_openai.ChatOpenAI`
  - Default model: `gpt-4o`
  - Higher rate limits (10,000 TPM) vs Anthropic (8,000 TPM org-wide)
  - Configured via: `SDLC_LLM_PROVIDER_DEFAULT=openai`
- **Anthropic Claude** via `langchain_anthropic.ChatAnthropic`
  - Alternative model: `claude-opus-4-20250514` (Claude Opus 4)
  - Superior reasoning but stricter rate limits
  - Configured via: `SDLC_LLM_PROVIDER_DEFAULT=anthropic`

**Rate Limit Strategy**:
- Exponential backoff with retries (5 attempts, 10s → 120s delays)
- Pre-flight API checks before pipeline execution
- `max_retries=3` on LLM clients
- Token limits: `max_tokens=16000` for Anthropic

Selection is controlled by environment variables (global or per-role):
- `SDLC_LLM_PROVIDER_DEFAULT=openai|anthropic`
- `SDLC_MODEL_DEFAULT=gpt-4o`
- Per-role overrides: `SDLC_LLM_PROVIDER_<ROLE>`, `SDLC_MODEL_<ROLE>`
  - Roles: `PRODUCT_MANAGER`, `BUSINESS_ANALYST`, `ARCHITECT`, `DEVELOPER`
- `SDLC_PREFER_ANTHROPIC=false` (default favors OpenAI for stability)

### 6.3 GitHub MCP (Pattern 1: LangGraph + MCP)
**Transport**: StreamableHTTP over HTTPS
**Client**: `GitHubMCPClient` (src/mcp_client/github_client.py)
**MCP Server**: `https://api.githubcopilot.com/mcp/`
**Auth**: Bearer token via `GITHUB_TOKEN`

**Features**:
- Repository creation and management
- File and directory operations (create, update, delete)
- Branch creation and management
- Pull request creation and management
- Commit operations

**Tool Selection**: LLM-driven via LangGraph
- Agent dynamically selects: `create_repository`, `create_branch`, `create_or_update_file`, `push_files`, `create_pull_request`
- Max 10 tool calls prevents infinite loops
- State tracking ensures coherent multi-step operations

### 6.4 Azure DevOps MCP (Pattern 2: Direct + Pattern 3: Hybrid)
**Transport**: stdio (npx subprocess)
**Client**: `AzureDevOpsMCPClient` (src/mcp_client/ado_client.py)
**MCP Server**: `@azure-devops/mcp` (npm package)
**Command**: `npx -y @azure-devops/mcp <org> -a <auth> -d <domains...>`

**Auth**:
- Preferred PAT env var: `ADO_MCP_AUTH_TOKEN`
- Back-compat: `AZURE_DEVOPS_TOKEN` mapped to `ADO_MCP_AUTH_TOKEN`

**Domains Enabled**:
- `boards`: Work items (Epics, Stories, Tasks, Issues)
- `test-plans`: Test Plans, Test Suites, Test Cases
- `work`: Iterations, team capacity

**Features**:
- **Work Items** (Pattern 2 - Direct): Create, link, query work items
- **Test Plans** (Pattern 3 - Hybrid): MCP with REST fallback for `TF200001` bug
- **Test Cases**: Create from acceptance criteria, link to stories
- **Iterations**: Team iteration management

**Known Issues & Mitigations**:
- **TF200001 Error**: MCP server passes empty project name to ADO CLI
  - **Mitigation**: Automatic REST API fallback
  - **Detection**: Check response for "tf200001" + "projectname" + "empty"
  - **Fallback**: Direct HTTPS call to `https://dev.azure.com/{org}/{project}/_apis/testplan/plans`
  - **Auth**: Basic auth with PAT token
  - **API Versions**: Tries 7.1-preview.1, 7.0, 6.0 for compatibility

### 6.5 Mermaid Rendering MCP (Pattern 4: Local On-Demand MCP Server)
**Transport**: stdio (node subprocess)
**Client**: `MermaidMCPClient` (src/mcp_client/mermaid_client.py)
**MCP Server**: `mcp-mermaid` (npm package) - **LOCAL ONLY, NO REMOTE SERVER**
**Wrapper**: `scripts/mcp_mermaid_stdio_wrapper.mjs` (prevents console.log pollution)
**Command**: `npx -y mcp-mermaid` (started on-demand by client)

#### Pattern 4: Local On-Demand MCP Server

**Key Difference from GitHub/ADO**:
- **GitHub MCP**: Remote server at `https://api.githubcopilot.com/mcp/` (always running)
- **ADO MCP**: Local stdio server started once per pipeline run, kept alive for all operations
- **Mermaid MCP**: Local stdio server started **per render request**, immediately terminated after

**Server Lifecycle**:
1. **Initialization**: Client creates `MermaidMCPClient()` instance (server NOT started yet)
2. **On-Demand Start**: When `render_mermaid_to_file()` is called:
   - Client spawns subprocess: `npx -y mcp-mermaid`
   - Wrapper script filters stdout (JSON-RPC only)
   - Session established via stdio
3. **Single Operation**: Render one diagram
4. **Immediate Shutdown**: Server process terminates when session closes
5. **Next Diagram**: Process repeats (new subprocess for each diagram)

**Who Starts the Server?**
- **Triggered by**: User confirms "Render Mermaid diagrams?" in pipeline
- **Started by**: `MermaidMCPClient._get_session()` context manager
- **Location**: `run_sdlc_pipeline.py` lines ~1119-1140 (Architecture stage)
- **Per-Diagram**: New `npx` subprocess spawned for each diagram render

**Example Flow**:
```python
# run_sdlc_pipeline.py - After Architect generates diagrams
client = MermaidMCPClient()  # No server started yet

for name, mermaid_code in diagrams.items():
    # Each call starts a NEW subprocess
    await client.render_mermaid_to_file(
        mermaid=mermaid_code,
        output_path=f"docs/diagrams/{name}.png"
    )
    # Subprocess terminates after render
```

**Why Wrapper Script (`mcp_mermaid_stdio_wrapper.mjs`)?**
- **Problem**: `mcp-mermaid` npm package prints console.log to stdout
- **MCP Protocol**: Requires stdout to be JSON-RPC messages ONLY
- **Corruption**: Non-JSON on stdout breaks MCP client parsing
- **Solution**: Wrapper filters lines:
  - Lines starting with `{` or `[` → stdout (JSON-RPC)
  - Everything else → stderr (logs)
- **Command**: `node scripts/mcp_mermaid_stdio_wrapper.mjs` which internally runs `npx -y mcp-mermaid`

**Subprocess Management**:
```python
# MermaidMCPClient.__init__
self.command = "node"
self.args = [wrapper_path]  # wrapper.mjs spawns npx mcp-mermaid

# _get_session() - context manager
async with stdio_client(StdioServerParameters(...)) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        yield session  # Render happens here
    # Session closes → subprocess terminates
```

**Features**:
- Render Mermaid diagrams to PNG/SVG
- Validate diagram syntax before rendering
- Output to `docs/diagrams/` directory
- Sanitize LLM-generated Mermaid (remove quotes, fix node syntax)
- Ephemeral subprocess (no persistent server process)

**LLM Diagram Generation Guardrails**:
- Architect agent has strict Mermaid syntax rules in system prompt
- Validation step before rendering (detect invalid patterns)
- Sanitization: remove quotes from labels, fix arrow syntax
- Fallback: extract diagrams from multiple locations (top-level, nested in architecture JSON)

**Comparison of MCP Server Patterns**:

| Aspect | GitHub MCP | ADO MCP | Mermaid MCP |
|--------|------------|---------|-------------|
| **Location** | Remote (GitHub hosted) | Local (stdio subprocess) | Local (stdio subprocess) |
| **Lifecycle** | Always running | Started once per pipeline | Started per render operation |
| **Transport** | StreamableHTTP | stdio (npx process) | stdio (node wrapper → npx) |
| **Tool Selection** | LLM-driven (LangGraph) | Code-driven (direct calls) | Code-driven (on-demand) |
| **When Started** | Pre-existing | Pipeline initialization | First render request |
| **Process Count** | N/A (remote) | 1 per pipeline run | 1 per diagram |
| **Auth Required** | Yes (GITHUB_TOKEN) | Yes (ADO_MCP_AUTH_TOKEN) | No |

### 6.6 LangSmith Observability
**Platform**: LangSmith (LangChain tracing/monitoring)
**Client**: `src/observability/langsmith_setup.py`

**Features**:
- **Tracing**: All agent invocations, LLM calls, tool calls
- **Debugging**: Full conversation history with latency metrics
- **Cost Tracking**: Token usage per agent/stage
- **Error Analysis**: Exception tracking with full context

**Configuration**:
- `LANGSMITH_API_KEY`: Authentication
- `LANGSMITH_PROJECT`: Project name (default: "pythonmcpproject")
- `LANGSMITH_TRACING=true`: Enable tracing

**Instrumentation**:
- Orchestrator methods: `@traceable` decorator
- Agent base class: automatic LLM call tracing
- Pipeline stages: tagged with metadata (stage name, agent role)

**Cleanup**: `scripts/clear_langsmith_traces.py`
- Dry-run by default; `--yes` to delete
- Filter by project, age, limit
- Handles API rate limiting (429 errors) with exponential backoff

---

## 7. Configuration & Secrets

### 7.1 Configuration Sources
- `.env` (gitignored) for local development
- environment variables (CI or developer shell)

### 7.2 Critical Env Vars (non-exhaustive)
- LLM: `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`
- LangSmith: `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_TRACING`
- GitHub MCP: `GITHUB_MCP_URL`, `GITHUB_TOKEN`
- Azure DevOps: `AZURE_DEVOPS_ORGANIZATION`, `AZURE_DEVOPS_PROJECT`, `ADO_MCP_AUTH_TOKEN`

### 7.3 Secret Handling Expectations
- Secrets must never be committed.
- Tokens should be rotated if leaked into logs/chat history.

---

## 8. Operational Notes

### 8.1 Local Runtime Prereqs
- Python venv with dependencies from `requirements.txt`
- Node.js available for:
  - Azure DevOps MCP (`npx @azure-devops/mcp`)
  - Mermaid MCP usage

### 8.2 Cleanup Scripts

#### Work Items Cleanup (`scripts/delete_all_work_items.py`)
- Dry-run by default; use `--yes` to delete
- Deletes Boards work items and Test Cases
- **Does NOT delete Test Plans by default** (use `--delete-test-plans`)
- Supports `--exclude-ids` to preserve specific items (e.g., `--exclude-ids "369,370"`)

```bash
# Dry run
python scripts/delete_all_work_items.py --org appatr --project testingmcp

# Delete all except specific IDs
python scripts/delete_all_work_items.py --org appatr --project testingmcp --exclude-ids "369,370" --yes
```

#### LangSmith Traces Cleanup (`scripts/clear_langsmith_traces.py`)
- Dry-run by default; use `--yes` to delete
- Supports filtering by project, limit, and age
- Handles API rate limiting with retries

```bash
# List projects
python scripts/clear_langsmith_traces.py --list-projects

# Dry run
python scripts/clear_langsmith_traces.py

# Delete all traces
python scripts/clear_langsmith_traces.py --yes

# Delete traces older than 7 days
python scripts/clear_langsmith_traces.py --older-than-days 7 --yes
```

### 8.3 Test Plan Population Defaults
- `scripts/populate_test_plan_from_work_items.py` defaults:
  - `plan_id`: `AZURE_DEVOPS_TEST_PLAN_ID` or `369`
  - `suite_id`: `AZURE_DEVOPS_TEST_SUITE_ID` or `370`

### 8.4 Demo Mode Constraints
For faster demo execution, the agents have built-in constraints:
- **Product Manager**: 5-7 requirements maximum
- **Business Analyst**: 3-5 Epics, 2-3 Stories per Epic, 1-2 Tasks per Story
- **Architect**: No constraints (full architectural detail retained)
- **Test Cases**: Generated 1:1 from stories (limited by story count)

---

## 9. Extensibility Points
- Add a new agent role:
  - Implement `BaseAgent` subclass + wire into `SDLCPipelineOrchestrator`
- Add/replace an MCP server:
  - Create a new client in `src/mcp_client/` and expose tools via conversion
- Add non-interactive automation:
  - Use `SDLC_NON_INTERACTIVE=true` and provide `SDLC_PROJECT_*` inputs

---

## 10. Known Constraints / Tradeoffs
- ADO Test artifacts have special deletion rules; some items require Test Plans APIs.
- MCP server behaviors vary by version/tenant; clients use fallback tool-name lists and REST escapes.
- The architecture agent’s output is LLM-generated JSON; invalid JSON is handled best-effort.- Developer agent has robust JSON extraction with multiple fallback strategies for code parsing.
- LangSmith API has aggressive rate limiting (100 items/request, frequent 429 responses).

---

## 11. Recent Changes (January 2026)

### 11.1 Northern Trust Technology Standards Integration
- **Cloud Platform**: Azure-native architecture enforced
- **Frontend**: React JS (TypeScript preferred)
- **Backend**: Java-based microservices (Spring Boot)
- **SSDLC Compliance**: Comprehensive secure coding standards for banking/financial sector

**Security Requirements**:
- Multi-Factor Authentication (MFA): OTP, authenticator apps, biometrics
- Role-Based Access Control (RBAC) with principle of least privilege
- End-to-end encryption: TLS 1.3+ (transit), AES-256 (at rest)
- Secure key management via Azure Key Vault
- Tokenization for sensitive data (PII, financial data)
- OAuth 2.0 and OpenID Connect for API security

**Compliance Frameworks**:
- PCI DSS (Payment Card Industry Data Security Standard)
- NIST Cybersecurity Framework
- ISO 27001/27002
- Nacha Operating Rules 2026 (ACH fraud monitoring)
- CFPB Open Banking Rule (Section 1033)

**Agent Integration**:
- Architect Agent: System prompts include Northern Trust technology preferences and SSDLC requirements
- Developer Agent: Code generation follows secure-by-design principles, includes security tests
- Knowledge Base: `docs/northern_trust_standards.md` documents all requirements

### 11.2 LLM Provider Optimization for Demo Reliability
- **Migration**: OpenAI GPT-4o now default (was Anthropic Claude)
- **Rationale**: Higher rate limits (10,000 TPM vs 8,000 TPM org-wide)
- **Rate Limit Handling**:
  - Exponential backoff with 5 retry attempts
  - Pre-flight API checks before pipeline execution
  - `max_retries=3` on LLM clients
  - Token limits: `max_tokens=16000` for Anthropic

### 11.3 MCP Integration Pattern Refinements
- **GitHub Agent**: LangGraph pattern for dynamic multi-step reasoning
- **ADO Client**: Direct MCP calls for deterministic work item creation
- **Test Plans**: Hybrid MCP + REST fallback for TF200001 bug resilience
- **Mermaid**: Strict syntax validation and sanitization for LLM-generated diagrams

### 11.4 Demo Constraints
- Added output limits to Product Manager and Business Analyst agents for faster demo execution
- Architect agent retains full detail for comprehensive architecture documentation

### 11.5 Developer Agent Improvements
- Enhanced JSON extraction with multiple fallback strategies
- Code blocks now properly extracted and added to `context.code_artifacts` even when JSON parsing fails
- Improved handling of LLM output variations

### 11.6 New Utility Scripts
- `scripts/clear_langsmith_traces.py`: LangSmith trace cleanup with rate limit handling
- `scripts/delete_all_work_items.py`: Added `--exclude-ids` parameter to preserve specific items
- Input handling: Fixed double-enter detection for pasted text

### 11.7 Azure DevOps Integration Enhancements
- Test case creation from story acceptance criteria
- Support for existing Test Plan/Suite reuse (IDs 369, 370)
- Test cases automatically linked to stories via acceptance criteria parsing
- Hybrid MCP + REST API pattern for resilient test plan operations

---

## 12. Northern Trust Demo - Key Talking Points

### 12.1 Enterprise AI Architecture Principles

#### 🎯 Production-Grade MCP Integration
**What**: Industry-standard protocol for connecting LLMs to enterprise systems
**Why**: Standardization, type safety, tool discovery, vendor flexibility
**Impact**: Future-proof architecture as MCP ecosystem expands

**Three Integration Patterns**:
1. **LangGraph + MCP** (GitHub): Complex multi-step reasoning with error recovery
2. **Direct MCP** (ADO): Deterministic, cost-optimized, code-driven workflows
3. **Hybrid MCP + REST** (Test Plans): Resilient with automatic fallback

**Key Insight**: *Right pattern for right use case - not all integrations need LLM decision-making*

#### 🔒 Secure-by-Design (SSDLC)
**What**: Security integrated from architecture through code generation
**How**: 
- Architect Agent: Security architecture, threat modeling, MFA/RBAC design
- Developer Agent: Secure code patterns, input validation, encryption, audit logging
- Compliance: PCI DSS, NIST, ISO 27001/27002, Nacha 2026

**Northern Trust Alignment**:
- ✅ Azure-native (cloud preference)
- ✅ React JS frontend
- ✅ Java Spring Boot microservices
- ✅ Banking-grade security (MFA, encryption, RBAC)
- ✅ Regulatory compliance (PCI DSS, NIST, CFPB Open Banking)

**Key Insight**: *AI generates compliant, secure code automatically - security is non-negotiable, not an afterthought*

#### ⚡ Demo Reliability Engineering
**Challenge**: Anthropic rate limits (8,000 TPM org-wide) caused demo interruptions
**Solution**: 
- OpenAI GPT-4o as default (10,000+ TPM)
- Exponential backoff with 5 retry attempts
- Pre-flight API checks
- Token budget management

**Result**: Zero rate limit errors in production demos

**Key Insight**: *Enterprise AI requires operational rigor - anticipate failure modes and design for resilience*

#### 🧩 Hybrid Integration Patterns
**Case Study**: ADO Test Plans TF200001 Bug
- **Problem**: MCP server bug (empty projectName parameter)
- **Solution**: Try MCP first → Detect error → Automatic REST API fallback
- **Benefit**: Forward-compatible (works when bug is fixed) + guaranteed success

**Key Insight**: *Real-world integrations require graceful degradation - don't let vendor bugs block your system*

### 12.2 Agent Orchestration Strategy

#### Custom Orchestrator vs LangGraph
**Custom Orchestrator** (SDLC Pipeline):
- Linear workflow with human approval gates
- Deterministic stage progression
- Cost-optimized (no LLM for orchestration logic)
- Clear failure boundaries

**LangGraph** (GitHub Agent):
- Dynamic tool selection based on context
- Multi-step reasoning with backtracking
- Error recovery through re-planning
- Max tool calls prevents infinite loops

**Key Insight**: *Use LangGraph when you need dynamic reasoning; use custom orchestrators for deterministic workflows*

#### Human-in-the-Loop Design
**Approval Gates**:
1. Requirements (Product Manager output)
2. Work Items (Business Analyst output)
3. Architecture (Architect output)
4. Code (Developer output)

**Feedback Loop**: Users can request revisions at each stage (3 max per stage)

**Key Insight**: *AI augments human judgment - critical decisions require human oversight, not blind automation*

### 12.3 Observability & DevOps

#### LangSmith Integration
- **Tracing**: Every LLM call, tool invocation, latency
- **Cost Tracking**: Token usage per agent/stage
- **Debugging**: Full conversation history with context
- **Error Analysis**: Exception tracking with root cause

**Demo Value**: Show live trace during execution - full transparency into AI decision-making

#### Configuration Management
- Environment-driven (12-factor app principles)
- Secrets in environment variables, never in code
- Per-role LLM provider/model overrides
- Feature flags for demo mode constraints

**Key Insight**: *AI systems need traditional DevOps rigor - observability, config management, secret handling*

### 12.4 Banking/Financial Use Case Fit

#### Wealth Management Onboarding Platform
**Demo Scenario**: Northern Trust client onboarding with MFA

**Requirements**:
- Secure registration with identity verification
- MFA: OTP, authenticator apps, biometrics
- KYC/AML integration
- Regulatory compliance (PCI DSS, CFPB)
- Azure-native, React + Java microservices

**Pipeline Output**:
1. **PRD**: Business requirements with success metrics
2. **Backlog**: Epics/Stories in Azure DevOps Boards
3. **Architecture**: Security-first design with threat modeling
4. **Code**: Spring Boot + React with MFA scaffolding
5. **Test Plan**: Test cases from acceptance criteria

**Key Insight**: *From product idea to working code in minutes - AI accelerates SDLC while maintaining enterprise quality standards*

### 12.5 Key Differentiators

| Aspect | Traditional | This Platform |
|--------|-------------|---------------|
| **MCP Integration** | Custom APIs per vendor | Standardized MCP protocol |
| **Tool Selection** | Hardcoded logic | LLM-driven (GitHub) + Code-driven (ADO) |
| **Security** | Retrofitted | Secure-by-design from architecture |
| **Compliance** | Manual checklists | Automated in code generation |
| **Reliability** | Hope for the best | Rate limits, retries, fallbacks |
| **Observability** | Black box | Full LangSmith tracing |
| **Speed** | Days/weeks | Minutes (with human oversight) |

### 12.6 Demo Script Highlights

**Opening** (2 min):
- "Enterprise AI requires production-grade patterns, not prototypes"
- Show MCP architecture diagram - explain three patterns

**Live Demo** (10 min):
1. Input: Wealth management onboarding requirements
2. Show: Product Manager → Business Analyst (work items pushed to ADO)
3. Show: Architect → security architecture with Mermaid diagrams
4. Show: Developer → Spring Boot + React code with MFA
5. Show: LangSmith trace (transparency)

**Technical Deep Dive** (5 min):
- GitHub LangGraph pattern (show tool selection)
- ADO direct MCP pattern (show deterministic flow)
- Test Plans hybrid pattern (explain TF200001 fallback)

**Northern Trust Alignment** (3 min):
- Azure-native: ✅
- React + Java: ✅
- SSDLC compliance: ✅ (show Architect prompt)
- Banking regulations: ✅ (PCI DSS, NIST, Nacha 2026)

**Closing** (2 min):
- "AI can accelerate SDLC while maintaining quality"
- "Right patterns for right use cases"
- "Production-ready today, not a research project"

---

**Questions to Anticipate**:
1. **"Can this handle real production scale?"**
   - Answer: Yes - rate limiting, retries, observability, secret management built-in
2. **"What about AI hallucinations?"**
   - Answer: Human approval gates, validation, deterministic flows where appropriate
3. **"How do you ensure security compliance?"**
   - Answer: SSDLC in agent prompts, automated validation, industry frameworks (PCI DSS, NIST)
4. **"What's the ROI?"**
   - Answer: Days → Minutes for initial implementation; quality bar maintained through oversight