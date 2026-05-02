# Directive: Job Application Swarm (Stage 7)

## Goal
Semi-automatically apply to validated jobs using **multiple isolated Claude Code agents**, each driving its own Chrome browser via Chrome DevTools MCP. No separate Anthropic API calls — Claude Code (your subscription) IS the brain. The orchestrator script (`run_swarm.py`) sets up workspaces and launches Chrome; the actual browser decisions are made by Claude Code agents reading their per-agent `CLAUDE.md` instructions.

## Pipeline Position
**Stage 7** — runs after CVs and cover letters are generated (Stages 4+5) and the user has reviewed jobs in the Google Sheet and marked them `Ready_to_Apply`.

## Prerequisites
- Stages 4+5 completed (CV + CL PDFs exist in `.tmp/applications/`)
- User has reviewed jobs in Google Sheet and set `Status="Ready_to_Apply"` for target jobs
- Playwright Chromium installed (`python -m playwright install chromium`)
- `chrome-devtools-mcp` available via npx (`npx chrome-devtools-mcp@latest`)
- Chrome installed at `C:\Program Files\Google\Chrome\Application\chrome.exe`

## Architecture

```
run_swarm.py (orchestrator — NO LLM calls)
  ├── setup:   Reads sheet, assigns jobs, creates agent workspaces
  ├── launch:  Starts Chrome instances on ports 9223+
  ├── status:  Aggregates per-agent logs
  ├── kill:    Terminates Chrome instances
  └── cleanup: Updates Google Sheet from agent logs

.tmp/swarm/
  ├── tasks.json                 # Read-only task assignments (written once by setup)
  ├── launch_chrome.ps1          # Spawns Chrome instances
  ├── kill_chrome.ps1            # Tears down all Chrome instances
  ├── screenshots/               # Before-submit + confirmation screenshots
  ├── agent-1/
  │   ├── .mcp.json              # Chrome DevTools MCP on port 9223
  │   ├── CLAUDE.md              # Agent behavior + profile data + job assignment
  │   ├── agent.log              # Agent writes its own status here (no collisions)
  │   └── profile/               # Selectively cloned browser profile
  ├── agent-2/                   # Port 9224, own log, own profile
  └── agent-3/                   # Port 9225
```

## MCP Package

**`chrome-devtools-mcp`** ([ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp))

29 tools including: `click`, `fill`, `fill_form`, `upload_file`, `navigate_page`, `new_page`, `take_screenshot`, `take_snapshot`, `evaluate_script`, `wait_for`, `select_page`, `close_page`, etc.

Each agent's `.mcp.json`:
```json
{
  "mcpServers": {
    "chrome": {
      "command": "npx",
      "args": [
        "chrome-devtools-mcp@latest",
        "--browser-url=http://127.0.0.1:9223"
      ]
    }
  }
}
```

Port increments per agent (9223, 9224, 9225, ...).

## Configuration
- **LLM:** Claude Code (user's subscription) — no separate API calls
- **Browser:** Headed Chrome with per-agent cloned profile (`.tmp/swarm/agent-N/profile/`)
- **Default agents:** 3 (configurable 1-5 via `--agents`)
- **Default limit:** 20 jobs total (configurable via `--limit`, hard max 20 per session)
- **Max jobs per agent:** 5
- **Sheet name:** `Swiss Job Search Pipeline` (configurable via `--sheet-name`)

## Script: `execution/run_swarm.py`

### Subcommands

```bash
# 1. Setup: read sheet, assign jobs with rate limiting, create agent workspaces
python execution/run_swarm.py setup --agents 3 --limit 9

# 2. Launch: start Chrome instances on assigned ports
python execution/run_swarm.py launch

# 3. Status: aggregate agent logs and show progress
python execution/run_swarm.py status

# 4. Kill: tear down all Chrome instances
python execution/run_swarm.py kill

# 5. Cleanup: read agent logs, update Google Sheet, remove swarm dir
python execution/run_swarm.py cleanup
python execution/run_swarm.py cleanup --keep   # preserve workspace
```

### Full Workflow

```bash
# Step 1: Setup workspaces
python execution/run_swarm.py setup --agents 3 --limit 9

# Step 2: Launch Chrome instances
python execution/run_swarm.py launch

# Step 3: Verify logins in each Chrome window

# Step 4: Spawn Claude Code sessions (commands printed by launch)
wt -w 0 new-tab -d ".tmp\swarm\agent-1" -- claude
wt -w 0 new-tab -d ".tmp\swarm\agent-2" -- claude
wt -w 0 new-tab -d ".tmp\swarm\agent-3" -- claude

# Step 5: Each Claude Code agent reads its CLAUDE.md and starts applying

# Step 6: Monitor progress
python execution/run_swarm.py status

# Step 7: After all agents finish, update sheet
python execution/run_swarm.py cleanup

# Step 8: Kill Chrome instances
python execution/run_swarm.py kill
```

### Job Selection: Freshness > Rating
The swarm prioritizes the **newest batch** of scraped jobs:
1. Filter sheet for `Status == "Ready_to_Apply"`
2. Parse `Date_Scraped` column (ISO or DD.MM.YYYY format)
3. Find the most recent scraping batch (same calendar day)
4. Filter to ONLY jobs from that newest batch
5. Sort by `Score` descending within the batch
6. Apply `--limit`

Use `--all-batches` to process jobs from all batches (sorted by date desc, then score desc).

### Platform-Aware Rate Limiting
Multiple agents hitting the same platform simultaneously risks account flagging. The `setup` command enforces concurrency limits per domain:

| Platform | Max Concurrent Agents | Reason |
|----------|----------------------|--------|
| linkedin.com | 1 | Aggressive bot detection |
| workday.com | 1 | Per-company ATS, single session safer |
| indeed.com | 2 | Moderate detection |
| glassdoor.com | 2 | Moderate detection |
| greenhouse.io | 3 | Lenient |
| lever.co | 3 | Lenient |
| jobs.ch | 3 | Lenient |
| smartrecruiters.com | 3 | Lenient |
| Other domains | 3 | Default: full parallelism |

**Assignment algorithm:**
1. Extract domain from each job URL
2. Group jobs by platform
3. For platforms with concurrency=1: all jobs go to a single agent
4. For platforms with concurrency=2+: spread across agents up to the limit
5. Within each agent, minimize domain switching

### Selective Browser Profile Cloning
Full Chrome profile cloning triggers session invalidation on some platforms. Only essential files are copied:

- `Local State` (root level)
- `Default/Cookies` + journal
- `Default/Login Data` + journal
- `Default/Local Storage/`
- `Default/Session Storage/`
- `Default/Preferences`
- `Default/Web Data` + journal

### Status Flow
```
New  -->  (user reviews, sets Ready_to_Apply)  -->  Ready_to_Apply
  -->  APPLYING  (setup marks assigned jobs)
  -->  Applied   (agent submitted successfully)
  -->  PAUSED    (CAPTCHA, login wall — needs human)
  -->  FAILED    (error, job removed, paywall)
```

### Per-Agent Logging (No Shared File Writes)
Each agent writes ONLY to its own `agent.log` — no file collision risk:

```
[2026-03-29T14:31:00] [WORKING] J-abc123 Navigating to Zurich Insurance career page
[2026-03-29T14:32:15] [WORKING] J-abc123 Filling application form (5 fields)
[2026-03-29T14:35:42] [DONE] J-abc123 Application submitted — confirmation screenshot saved
[2026-03-29T14:36:10] [WORKING] J-def456 Starting next job
[2026-03-29T14:38:55] [PAUSED] J-def456 CAPTCHA detected — needs human intervention
```

`tasks.json` is written ONCE by `setup` and is READ-ONLY for agents. The `status` command aggregates all `agent-N/agent.log` files into a unified view.

### Screenshot Archive
Before submit and after confirmation, agents save screenshots:
```
.tmp/swarm/screenshots/
  J-abc123-before-submit.png
  J-abc123-confirmation.png
```
Proof of application if a company claims they never received it. Screenshots are preserved during `cleanup` (moved to `.tmp/swarm_screenshots/`).

### Humanization Rules (CRITICAL for bot detection)
| Action | Delay |
|--------|-------|
| Between form fields | 0.8-2.5 seconds |
| After clicking any button | 1-3 seconds |
| Between page navigations | 3-7 seconds |
| Before clicking Apply | Scroll page down, wait 5-15 seconds, scroll back up |
| Before clicking Submit | 10-25 seconds (review simulation) |

Scrolling uses `evaluate_script` with `window.scrollBy(0, window.innerHeight)` — real humans read the page before applying.

### Anti-Hallucination Rules
- Agent receives the FULL candidate profile via its CLAUDE.md
- Must ONLY use data from the profile — never invent numbers, addresses, or history
- For unknown fields: leave blank or select "Prefer not to say"
- Salary expectations: leave blank or "negotiable"
- "How did you hear about us": "Online job search" / "Online Jobsuche"

### Human-in-the-Loop (HITL)
Each agent is a live Claude Code session — the human is watching.

**When the agent asks (interactive terminal):**
- Form fields not covered by the profile (e.g., salary expectations)
- Dropdowns with unclear options
- Checkboxes where the answer isn't in the profile
- **Always before clicking Submit** — "Ready to submit?"

**When the agent stops (reports [PAUSED] in agent.log):**
- CAPTCHA or bot detection
- Login / account creation required
- Chrome becomes unresponsive after 3 retries

### Session Limits
- **Max 5 jobs per agent** — prevents one agent blocking too many jobs on CAPTCHA
- **Max 20 applications per session** — Chrome profiles accumulate fingerprints. Restart the swarm for more.

### After a Run
1. Run `python execution/run_swarm.py status` to check progress
2. Run `python execution/run_swarm.py cleanup` to update the Google Sheet
3. Review `PAUSED` jobs in the sheet — resolve manually or re-run
4. Review `FAILED` jobs — check Notes column for error reason
5. Run `python execution/run_swarm.py kill` to terminate Chrome instances
6. Check `.tmp/swarm_screenshots/` for application proof

## Edge Cases & Learnings
(Auto-updated as the system learns)
- Persistent browser profile preserves cookies/sessions across runs — only need to log in once per platform
- LinkedIn Easy Apply and Indeed Quick Apply have simpler flows than full ATS portals
- Cookie consent banners should be dismissed if they block the application form
- Some ATS systems (Workday, Greenhouse) use React/dynamic JS — the `wait_for` tool handles AJAX loads
- Selective profile cloning avoids Chrome detecting simultaneous full-profile sessions
- Platform-aware rate limiting prevents account flagging on aggressive platforms (LinkedIn, Workday)
