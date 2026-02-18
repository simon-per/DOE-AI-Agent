# DOE AI Agent

A 3-layer architecture system that separates AI orchestration from deterministic execution to maximize reliability.

## Architecture Overview

**Layer 1: Directives** (`directives/`)
- Markdown-based SOPs defining what to do
- Natural language instructions for mid-level execution
- Include goals, inputs, tools, outputs, and edge cases

**Layer 2: Orchestration** (AI Agent)
- Intelligent routing and decision-making
- Reads directives, calls execution tools, handles errors
- Updates directives with learnings

**Layer 3: Execution** (`execution/`)
- Deterministic Python scripts
- Handles API calls, data processing, file operations
- Reliable, testable, fast

## Directory Structure

```
.
├── directives/          # SOPs in Markdown
├── execution/           # Python scripts
│   └── webhooks.json   # Modal webhook mappings
├── .tmp/               # Intermediate files (never commit)
├── .env                # Environment variables (never commit)
├── credentials.json    # Google OAuth (never commit)
├── token.json          # Google OAuth (never commit)
├── CLAUDE.md           # Agent instructions
├── AGENTS.md           # Agent instructions (mirror)
└── GEMINI.md           # Agent instructions (mirror)
```

## Setup Instructions

### 1. Clone and Install

```bash
# Install Python dependencies (create requirements.txt as needed)
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit .env and add your API keys
# - ANTHROPIC_API_KEY
# - MODAL_TOKEN_ID and MODAL_TOKEN_SECRET
# - SLACK_WEBHOOK_URL
# - etc.
```

### 3. Google OAuth Setup

For Google Sheets/Slides integration:
1. Create a project in Google Cloud Console
2. Enable Google Sheets API and Google Slides API
3. Create OAuth 2.0 credentials
4. Download and save as `credentials.json`
5. Run your first script - it will generate `token.json`

### 4. Modal Setup (Optional)

For cloud webhooks:
```bash
# Install Modal
pip install modal

# Authenticate
modal token new

# Deploy webhooks
modal deploy execution/modal_webhook.py
```

## Key Principles

1. **Check for tools first** - Before writing scripts, check `execution/`
2. **Self-anneal when things break** - Fix errors, update tools, improve directives
3. **Update directives as you learn** - Document API constraints, edge cases, improvements
4. **Local files are temporary** - Deliverables live in cloud services (Google Sheets, Slides)

## File Organization

- **Deliverables**: Google Sheets, Google Slides, or other cloud-based outputs
- **Intermediates**: Temporary files in `.tmp/` (never committed, always regenerated)

## Modal Webhooks

Webhooks enable event-driven execution:
- List webhooks: to be added in future
- Execute directive: to be added in future
- Test email: to be added in future

See `directives/add_webhook.md` for setup instructions.

## Usage

The AI agent reads directives from `directives/` and executes tasks using scripts in `execution/`. The system continuously improves through self-annealing: when errors occur, the agent fixes the issue, updates the tool, and documents learnings in the directive.

## Contributing

When adding new capabilities:
1. Create a directive in `directives/` describing the process
2. Write deterministic scripts in `execution/` for the work
3. Test and iterate
4. Document learnings and edge cases

---

For detailed agent instructions, see [CLAUDE.md](CLAUDE.md).
