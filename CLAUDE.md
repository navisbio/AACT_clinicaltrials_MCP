# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP (Model Context Protocol) server that gives AI assistants read-only access to the AACT clinical trials database (ClinicalTrials.gov). It exposes three tools: `list_tables`, `describe_table`, and `read_query` over the FastMCP framework, connecting to a remote PostgreSQL database.

## Commands

```bash
# Run the server
uv run mcp-server-aact

# Run tests
uv run pytest

# Type checking
uv run pyright src/

# Install dependencies (including dev)
uv sync

# Docker build and run
docker build -t mcp-server-aact .
docker run --rm -i --env DB_USER=X --env DB_PASSWORD=Y mcp-server-aact
```

## Architecture

Source files in `src/`, each with a single responsibility:

- **server.py** — FastMCP server definition and tool handlers. Loads `.env` at startup, creates the `AACTDatabase` instance and the `FastMCP` app. All tool handlers are async and use `Context` for structured logging/progress.
- **database.py** — `AACTDatabase` class wrapping psycopg2. Connects to `aact-db.ctti-clinicaltrials.org` through a small pool with a connect timeout and a statement timeout. Fails hard if `DB_USER`/`DB_PASSWORD` env vars are missing.
- **query_guard.py** — Parses user SQL and rejects anything other than a single read-only SELECT, WITH, or EXPLAIN statement.
- **models.py** — Pydantic models (`TableInfo`, `ColumnInfo`, `QueryResult`) used as tool return types.

Entry point: `src/__init__.py` exports `main()` which calls `mcp.run()`.

## Key Design Decisions

- **Read-only enforcement**: `query_guard.py` parses SQL before it is sent. The PostgreSQL session is read-only as well, and dynamic table or column names use `psycopg2.sql.Identifier`.
- **Fail-hard**: No silent defaults for missing config. Missing credentials raise immediately.
- **Row limiting**: Default 25 rows per query, configurable via `max_rows` parameter.
- **Remote database**: Connects directly to AACT's hosted PostgreSQL — no local DB setup needed. Users register at https://aact.ctti-clinicaltrials.org for credentials.

## Environment Variables

Required in `.env` or environment:
- `DB_USER` — AACT database username
- `DB_PASSWORD` — AACT database password
