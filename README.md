# Python MCP Agent with LangGraph & LangSmith

A Python application that uses **LangGraph** for agent orchestration, **LangSmith** for observability, and connects to the **GitHub MCP Server** as an MCP host.

## Features

- 🤖 **LangGraph Agent**: Stateful agent with tool-calling capabilities
- 🔗 **MCP Client**: Connects to GitHub MCP server for GitHub API access
- 📊 **LangSmith Observability**: Full tracing and monitoring of agent runs
- 🎭 **Agent Orchestrator**: Multi-agent coordination and workflow execution
- ✅ **Comprehensive Tests**: Unit and integration tests

## Project Structure

```
.
├── src/
│   ├── agents/              # LangGraph agents and orchestrator
│   │   ├── github_agent.py  # GitHub-focused agent
│   │   └── orchestrator.py  # Multi-agent orchestration
│   ├── mcp_client/          # MCP client implementation
│   │   ├── github_client.py # GitHub MCP server client
│   │   └── tool_converter.py# MCP to LangChain tool conversion
│   ├── observability/       # LangSmith integration
│   │   └── langsmith_setup.py
│   ├── config.py            # Configuration management
│   └── main.py              # Application entry point
├── tests/                   # Test files
├── .vscode/
│   └── mcp.json            # MCP server configuration
├── .env.example            # Environment variables template
├── pyproject.toml          # Project configuration
└── requirements.txt        # Dependencies
```

## Setup

1. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

   Required:
   - `OPENAI_API_KEY`: Your OpenAI API key

   Optional:
   - `LANGSMITH_API_KEY`: For observability (get at https://smith.langchain.com)
   - `GITHUB_TOKEN`: For GitHub API authentication

## Usage

### Run the Agent

```bash
python src/main.py
```

This starts an interactive session where you can chat with the GitHub agent.

### Example Queries

```
You: List my GitHub repositories
You: Search for Python MCP projects
You: Get details about repo owner/name
```

### Run Tests

```bash
pytest tests/ -v
```

## Architecture

See [docs/architecture_and_design.md](docs/architecture_and_design.md) for a detailed platform architecture & integration design.

### LangGraph Agent

The agent uses LangGraph's `StateGraph` for structured workflow:

```
┌─────────┐     ┌───────┐     ┌─────────┐
│  Agent  │────▶│ Tools │────▶│  Agent  │
└─────────┘     └───────┘     └─────────┘
     │                              │
     └──────────── END ◀────────────┘
```

### MCP Integration

The application connects to the GitHub MCP server configured in `.vscode/mcp.json`:

```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    }
  }
}
```

### LangSmith Observability

All agent runs are traced to LangSmith when configured:

- View traces at https://smith.langchain.com
- Monitor token usage, latency, and errors
- Debug agent reasoning steps

## Development

### Format code
```bash
black src tests
```

### Lint code
```bash
ruff check src tests
```

### Type check
```bash
mypy src
```

## License

MIT
