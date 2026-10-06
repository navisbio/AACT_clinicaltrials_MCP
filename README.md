# AACT Clinical Trials MCP Server

Query the [AACT](https://aact.ctti-clinicaltrials.org) (ClinicalTrials.gov) database directly from Claude. Explore 70+ tables of clinical trial data — studies, interventions, outcomes, sponsors, facilities — using read-only SQL with buffered pagination.

## Why AACT over the ClinicalTrials.gov API?

The ClinicalTrials.gov API returns one JSON record per trial — useful for quick lookups, but awkward for analytics. Want the average duration of Phase 2 NSCLC trials from 2020-2025? With the API you'd filter trials, extract dates from each JSON record, then compute durations client-side. With AACT, that's a single SQL query.

A structured PostgreSQL database makes it far easier to **aggregate, combine, and summarize** clinical trial data in any way you need. And for AI-assisted analysis, SQL is a standard that LLMs handle extremely well — fewer mistakes, less context to manage, better performance, and lower cost compared to parsing bespoke API responses.

> **Note:** This is an independent, third-party integration. It is not affiliated with or endorsed by the [Clinical Trials Transformation Initiative (CTTI)](https://ctti-clinicaltrials.org) or Duke University. However, we released a case study with CTTI on integrating their database with Claude - see [AACT case study](https://connects.ctti-clinicaltrials.org/show/57.pdf).

## Tools

| Tool | Description |
|------|-------------|
| `database_info` | Confirm database connection, server time, and data currency |
| `list_tables` | Discover all available tables with approximate row counts |
| `describe_table` | Inspect column names, types, distinct counts, and sample values |
| `get_column_values` | Get distinct values for a column with counts — essential before filtering |
| `search_columns` | Find columns by keyword across all tables (e.g. `masking` -> `designs.masking`) |
| `read_query` | Execute a SELECT, CTE, or EXPLAIN query with buffered results and preview |
| `fetch_rows` | Page through buffered query results without re-querying |

All tables join on `nct_id`.

Every `read_query` statement is parsed before it runs. Only one read-only `SELECT`, `WITH`, or `EXPLAIN` statement is accepted. It runs in a read-only database session and is cancelled after 2 minutes.

## Setup

1. Create a free account at https://aact.ctti-clinicaltrials.org/users/sign_up
2. Install the plugin (see options below)
3. Enter your AACT credentials when prompted

## Installation

### Option 1: Claude Desktop Plugin (recommended)

Download the latest `.mcpb` file from [Releases](https://github.com/navisbio/AACT_clinicaltrials_MCP/releases) and open it in Claude Desktop. You'll be prompted for your AACT credentials.

### Option 2: Published package

Add to your `claude_desktop_config.json` (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "aact": {
      "command": "uvx",
      "args": ["mcp-server-aact"],
      "env": {
        "DB_USER": "your_username",
        "DB_PASSWORD": "your_password"
      }
    }
  }
}
```

### Option 3: Docker

```json
{
  "mcpServers": {
    "aact": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env", "DB_USER=your_username",
        "--env", "DB_PASSWORD=your_password",
        "navisbio/mcp-server-aact:latest"
      ]
    }
  }
}
```

### Option 4: From source

```bash
git clone https://github.com/navisbio/AACT_clinicaltrials_MCP.git
cd AACT_clinicaltrials_MCP
uv sync
```

```json
{
  "mcpServers": {
    "aact": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/AACT_clinicaltrials_MCP", "mcp-server-aact"],
      "env": {
        "DB_USER": "your_username",
        "DB_PASSWORD": "your_password"
      }
    }
  }
}
```

## Example Prompts

### 1. Competitive landscape analysis

> "Who are the top 10 sponsors of Phase 3 Alzheimer's disease trials? Break down by trial status."

The server will discover relevant tables, check enum values for phase and status, then build a query joining `studies`, `conditions`, and `sponsors`.

### 2. Drug pipeline search

> "Find all actively recruiting Phase 2 and Phase 3 trials for pembrolizumab in non-small cell lung cancer. Show NCT ID, title, enrollment, and lead sponsor."

Uses `get_column_values` to confirm phase format (`PHASE2`, `PHASE3`), then queries across `studies`, `browse_interventions`, and `conditions`.

### 3. Endpoint analysis

> "What are the most common primary outcome measures in completed Phase 3 type 2 diabetes trials?"

Joins `studies` with `outcomes` to analyze endpoint patterns, grouped by outcome measure type.

### 4. Geographic distribution

> "How many clinical trial sites does a typical rare disease trial have? Show the top countries by site count."

Queries the `facilities` table joined with `conditions` to map trial geography.

## Privacy

This server is read-only and does not collect or store any personal data. See [PRIVACY.md](PRIVACY.md) for details.

## Troubleshooting

### Connection or authentication errors

- Verify your AACT credentials at https://aact.ctti-clinicaltrials.org/users/sign_in
- The AACT database undergoes weekly maintenance (typically weekends) — try again later if the connection is refused
- Ensure `DB_USER` and `DB_PASSWORD` are set correctly in your config

### `spawn uvx ENOENT` error

The system cannot find `uvx`. Use the full path:

```json
{
  "mcpServers": {
    "aact": {
      "command": "/Users/username/.local/bin/uvx",
      "args": ["mcp-server-aact"],
      "env": {
        "DB_USER": "your_username",
        "DB_PASSWORD": "your_password"
      }
    }
  }
}
```

## Contributing

- Open an issue on [GitHub](https://github.com/navisbio/AACT_clinicaltrials_MCP)
- Email: jonas.walheim@navis-bio.com

## License

MIT
