# DOE AI Agent

An AI-assisted job-search operations pipeline built as a practical portfolio project.

The project demonstrates how a messy, repetitive workflow can be turned into a scheduled, observable, mostly deterministic automation system. It is not a packaged SaaS product; it is a working automation stack that combines scraping, scoring, document generation, Google Workspace integrations, and cloud orchestration.

## What This Demonstrates

- End-to-end workflow automation from job discovery to application documents and follow-up tracking.
- Pragmatic LLM usage: language generation and judgment are AI-assisted, while deduplication, state management, file operations, and safety checks stay deterministic.
- Cloud orchestration on Modal with scheduled jobs, persistent volumes, stage-level logging, and failure alerts.
- Google Sheets, Drive, and Gmail integration for tracking, document storage, and follow-up workflows.
- Human-in-the-loop design: the system prepares and maintains application assets, while final submission remains controlled by the applicant.
- Reliability patterns such as checkpoints, dry-run modes, idempotent uploads, orphan cleanup, retry wrappers, and explicit safety gates before destructive actions.

## High-Level Workflow

```text
Job sources
  -> scrape and normalize listings
  -> evaluate fit with an LLM-assisted scoring layer
  -> write ranked jobs to Google Sheets
  -> generate tailored cover letters and CVs
  -> upload application folders to Google Drive
  -> maintain freshness, prune stale rows, and clean orphan folders
  -> prepare/send follow-up emails when due
```

## Architecture

The project follows a three-layer architecture:

1. Directives: Markdown SOPs in `directives/` describe what each workflow should do.
2. Orchestration: the agent or Modal runner decides which deterministic tools to call and in what order.
3. Execution: Python scripts in `execution/` perform the actual work: API calls, document generation, sheet updates, file cleanup, and retries.

This split keeps the LLM away from brittle business logic. The AI helps with judgment-heavy tasks, while Python handles the parts that need repeatability.

## Key Components

- `execution/scrape_jobs.py`: collects and normalizes job listings.
- `execution/evaluate_jobs.py`: scores jobs against a profile.
- `execution/write_jobs_to_sheet.py`: writes ranked jobs to Google Sheets.
- `execution/generate_cover_letter.py`: creates tailored cover letters.
- `execution/generate_cv.py`: creates tailored CVs.
- `execution/send_followups.py`: manages timed follow-up emails.
- `execution/modal_pipeline.py`: cloud schedules and orchestration on Modal.
- `execution/cleanup_orphan_application_folders.py`: removes application folders that no longer exist in the sheet source of truth.
- `directives/`: operational instructions and workflow rules.
- `scripts/`: local Windows helper wrappers.

## Reliability And Safety

- Dry-run defaults for destructive or external-facing actions.
- Explicit `--apply` / `--yes` style flags for deletion workflows.
- Checkpoints for long-running stages.
- Idempotent Drive uploads and folder cleanup.
- Modal stage logs persisted to the project volume for post-mortems.
- Separate local and cloud execution paths with shared deterministic scripts.
- Secrets, OAuth tokens, local profile data, generated documents, and `.tmp/` artifacts are intentionally excluded from Git.

## Tech Stack

- Python 3.11
- Modal for cloud scheduling
- Google Sheets, Drive, and Gmail APIs
- LLM providers via environment-configured API keys
- `python-docx` and PDF rendering for document generation
- Playwright for browser-backed workflows where needed

## Repository Notes

This repository is intended as a high-level demonstration of automation engineering, AI orchestration, and reliability patterns. Running it requires your own API keys, OAuth setup, Google Workspace resources, and a private profile file. Those files are not included in the public repository.

The project is actively evolving, so the most interesting parts are the architecture and workflow-hardening patterns rather than a one-command installation flow.
