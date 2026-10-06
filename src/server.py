import functools
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from psycopg2 import sql
from pydantic import Field

from .database import AACTDatabase
from .query_guard import query_is_explain
from .models import (
    GROUNDING_NOTICE,
    TableInfo,
    ColumnInfo,
    QueryResultSummary,
    QueryResultPage,
    ResultBuffer,
)

logger = logging.getLogger('mcp_aact_server')

MAX_BUFFERS = 5
_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass
class AppContext:
    db: AACTDatabase
    result_buffers: OrderedDict[str, ResultBuffer] = field(default_factory=OrderedDict)
    query_counter: int = field(default=0)


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Manage application lifecycle — initialize DB on startup."""
    db = AACTDatabase()
    try:
        yield AppContext(db=db)
    finally:
        db.close()
        logger.info("Server shutting down")


mcp = MCPServer(
    name="AACT Clinical Trials Database",
    instructions="""You are an MCP server providing access to the AACT (Aggregate Analysis of ClinicalTrials.gov) database.

Use the available tools to explore and query the database:
1. database_info — confirm connection and data currency
2. list_tables — discover available tables
3. describe_table — examine columns, types, and sample/example values for a table
4. get_column_values — get distinct values for a column (essential for filters like phase, status)
5. search_columns — find columns by keyword across all tables (e.g. "masking" → designs.masking)
6. read_query — execute a SELECT, WITH (CTE), or EXPLAIN query; returns a summary with preview, buffers full results
7. fetch_rows — retrieve pages of rows from the buffered result using the query_id

Recommended workflow:
1. Call database_info to verify connection
2. Call list_tables or search_columns to find relevant tables/columns
3. Call describe_table on tables you plan to query
4. Call get_column_values for columns you want to filter on (phase, overall_status, etc.)
5. Build your SQL using the exact values returned — do NOT guess enum formats
6. Use read_query to run your SQL. CTE (WITH …) and EXPLAIN / EXPLAIN ANALYZE queries are supported. Review the preview rows.
7. Use fetch_rows with the query_id to page through results if needed.

Common pitfalls:
- Phase values are UPPERCASE with no spaces: PHASE1, PHASE2, PHASE3, PHASE4, PHASE1/PHASE2, PHASE2/PHASE3
- Status values use UPPERCASE with underscores: RECRUITING, COMPLETED, ACTIVE_NOT_RECRUITING, TERMINATED, NOT_YET_RECRUITING
- Condition and intervention names are inconsistent free text — always use ILIKE with % wildcards
- All tables join on nct_id. Key tables: studies, conditions, interventions, sponsors, outcomes, facilities
- JOINs can cause row fan-out (duplicate nct_ids). Use SELECT DISTINCT or GROUP BY to deduplicate.
- Use EXPLAIN ANALYZE to understand query plans and optimize slow queries.
- Use browse_conditions and browse_interventions for MeSH-standardized terms (more reliable than free-text tables)

CRITICAL: Your answer MUST be based on data received from the AACT database exclusively.
NEVER invent, guess, or recall NCT IDs, drug names, or statistics from memory.
Every factual claim must trace back to a row in a tool result.
If the data is insufficient, say so and suggest a follow-up query.""",
    lifespan=app_lifespan,
)


def _get_ctx(ctx: Context) -> AppContext:
    """Extract AppContext from lifespan context."""
    lifespan = ctx.request_context.lifespan_context
    if not isinstance(lifespan, AppContext):
        raise RuntimeError("Server lifespan is not initialized")
    return lifespan


def _tool_annotations(title: str, *, open_world: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        open_world_hint=open_world,
    )


def _expose_errors(fn):
    """Turn anticipated ValueErrors into ToolErrors so the client sees the message.

    MCP 2 treats any other exception as a crash and replaces it with
    "Error executing tool <name>", which hides the column or syntax detail
    the model needs. The message is already safe for the client.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    return wrapper


def _identifier(name: str, kind: str) -> sql.Identifier:
    """Quote a table or column name. Rejects anything that is not a plain identifier."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Invalid {kind}: {name}")
    return sql.Identifier(name)


def grounded_result(data: object) -> list[TextContent]:
    """Return tool result with grounding notice as second content element."""
    import json
    return [
        TextContent(type="text", text=json.dumps(data, default=str, indent=2)),
        TextContent(type="text", text=GROUNDING_NOTICE),
    ]


@mcp.tool(annotations=_tool_annotations("List tables"))
@_expose_errors
async def list_tables(ctx: Context):
    """Call this first to discover available tables before writing any queries.
    Returns all table names in the AACT ctgov schema (studies, interventions, outcomes, etc.)
    with approximate row counts. Use the returned names with describe_table to inspect columns before querying."""
    app = _get_ctx(ctx)
    results, _ = app.db.execute_query("""
        SELECT t.table_name, c.reltuples::bigint AS approximate_row_count
        FROM information_schema.tables t
        JOIN pg_class c ON c.relname = t.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'ctgov'
        WHERE t.table_schema = 'ctgov'
        ORDER BY t.table_name;
    """)
    logger.debug("Retrieved %s tables", len(results))
    tables = [
        TableInfo(
            table_name=row['table_name'],
            approximate_row_count=max(0, row.get('approximate_row_count', 0)),
        ).model_dump() for row in results
    ]
    return grounded_result(tables)


@mcp.tool(annotations=_tool_annotations("Describe table"))
@_expose_errors
async def describe_table(
    table_name: Annotated[str, Field(description="Name of the table to describe", min_length=1)],
    ctx: Context,
):
    """Call this before writing a query to learn the column names and types for a table.
    Returns table_name, approximate_row_count, and a columns list with SQL data types and total_distinct counts.
    Use the exact column names in your SELECT queries.
    If the table name is invalid, returns an empty columns list — check list_tables for valid names.
    Low-cardinality columns (≤25 distinct values) include sample_values automatically.
    After this, call get_column_values on any column you plan to filter on (especially phase,
    overall_status, study_type, or any enum-like column) to learn the exact stored values — do NOT guess the format."""
    app = _get_ctx(ctx)

    table = _identifier(table_name, "table name")

    results, _ = app.db.execute_query("""
        SELECT column_name, data_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'ctgov'
        AND table_name = %s
        ORDER BY ordinal_position;
    """, {"table_name": table_name})

    logger.debug("Retrieved %s columns for table %s", len(results), table_name)

    # Get approximate row count from pg_class
    row_count_rows, _ = app.db.execute_query("""
        SELECT c.reltuples::bigint AS approximate_row_count
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'ctgov'
        WHERE c.relname = %s;
    """, {"table_name": table_name})
    approximate_row_count = max(0, row_count_rows[0]['approximate_row_count']) if row_count_rows else 0

    # Get n_distinct stats for all columns in this table
    distinct_map: dict[str, int] = {}
    low_card_cols: set[str] = set()
    if results:
        stats, _ = app.db.execute_query("""
            SELECT attname, n_distinct
            FROM pg_stats
            WHERE schemaname = 'ctgov' AND tablename = %s
            AND n_distinct >= 1;
        """, {"table_name": table_name})
        for row in stats:
            distinct_map[row['attname']] = int(row['n_distinct'])
            if row['n_distinct'] <= 25:
                low_card_cols.add(row['attname'])

    # Fetch sample values for low-cardinality columns
    sample_values_map: dict[str, list[str]] = {}
    for col_name in low_card_cols:
        if not _IDENTIFIER.match(col_name):
            continue
        try:
            column = sql.Identifier(col_name)
            vals, _ = app.db.execute_query(
                sql.SQL(
                    "SELECT {column} AS value, COUNT(*) AS count "
                    "FROM ctgov.{table} "
                    "WHERE {column} IS NOT NULL "
                    "GROUP BY {column} "
                    "ORDER BY count DESC LIMIT 10"
                ).format(column=column, table=table)
            )
            sample_values_map[col_name] = [str(v['value']) for v in vals]
        except Exception:
            pass  # Skip columns that fail (e.g. unsupported types)

    # Fetch example values for high-cardinality text columns from a few rows
    example_values_map: dict[str, list[str]] = {}
    text_cols = [
        row['column_name'] for row in results
        if row['column_name'] not in low_card_cols
        and row['data_type'] in ('character varying', 'text')
        and _IDENTIFIER.match(row['column_name'])
    ]
    if text_cols and results:
        try:
            sample_rows, _ = app.db.execute_query(
                sql.SQL("SELECT * FROM ctgov.{table} LIMIT 3").format(table=table)
            )
            for col in text_cols:
                vals = [str(r[col]) for r in sample_rows if r.get(col) is not None]
                if vals:
                    example_values_map[col] = vals
        except Exception:
            pass  # Skip if sample query fails

    columns = [
        ColumnInfo(
            column_name=row['column_name'],
            data_type=row['data_type'],
            character_maximum_length=row.get('character_maximum_length'),
            total_distinct=distinct_map.get(row['column_name']),
            sample_values=sample_values_map.get(row['column_name']),
            example_values=example_values_map.get(row['column_name']),
        ).model_dump() for row in results
    ]
    return grounded_result({
        "table_name": table_name,
        "approximate_row_count": approximate_row_count,
        "columns": columns,
    })


@mcp.tool(annotations=_tool_annotations("Get column values"))
@_expose_errors
async def get_column_values(
    table_name: Annotated[str, Field(description="Table name in the ctgov schema", min_length=1)],
    column_name: Annotated[str, Field(description="Column to get distinct values for", min_length=1)],
    ctx: Context,
    limit: Annotated[int, Field(
        description="Maximum distinct values to return",
        gt=0, le=100,
    )] = 25,
):
    """Get the distinct values stored in a column, with counts. CALL THIS before filtering on any
    column to learn the exact format — values are often UPPERCASE or use underscores (e.g. PHASE3
    not 'Phase 3', ACTIVE_NOT_RECRUITING not 'Active, not recruiting'). Essential for: studies.phase,
    studies.overall_status, studies.study_type, sponsors.lead_or_collaborator, interventions.intervention_type.
    Returns up to `limit` values sorted by frequency (most common first)."""
    app = _get_ctx(ctx)

    table = _identifier(table_name, "table name")
    column = _identifier(column_name, "column name")

    results, _ = app.db.execute_query(
        sql.SQL(
            "SELECT {column} AS value, COUNT(*) AS count "
            "FROM ctgov.{table} "
            "WHERE {column} IS NOT NULL "
            "GROUP BY {column} "
            "ORDER BY count DESC "
            "LIMIT %s"
        ).format(column=column, table=table),
        {"limit": limit},
    )

    logger.debug(
        "Retrieved %s distinct values for %s.%s",
        len(results),
        table_name,
        column_name,
    )
    return grounded_result(results)


@mcp.tool(annotations=_tool_annotations("Read query"))
@_expose_errors
async def read_query(
    query: Annotated[str, Field(description="SELECT SQL query to execute", min_length=1)],
    ctx: Context,
    max_rows: Annotated[int, Field(
        description="Maximum rows to buffer. Use SQL LIMIT for precise control.",
        gt=0, le=5000,
    )] = 100,
    preview_rows: Annotated[int, Field(
        description="Number of rows to return immediately as a preview.",
        gt=0, le=100,
    )] = 25,
):
    """Run a read-only query and get a summary with a preview of results.
    Full results are buffered server-side — call fetch_rows with the returned query_id
    to page through them without re-executing the query.
    The query must be one SELECT, WITH (CTE), or EXPLAIN statement. It is parsed before
    it runs, executed in a read-only transaction, and cancelled after 2 minutes.
    Use WHERE and LIMIT to narrow results.
    If truncated is true, the query had more rows than max_rows — add a LIMIT or tighter WHERE.

    IMPORTANT: Before writing your query, call get_column_values on any column you plan to
    filter on (phase, overall_status, study_type, etc.) to learn exact stored values.

    Common pitfalls — read before writing your query:
    - Phase is UPPERCASE no spaces: WHERE phase = 'PHASE3' (NOT 'Phase 3')
    - Status is UPPERCASE with underscores: WHERE overall_status = 'RECRUITING' (NOT 'Recruiting')
    - Use ILIKE with % for text search: WHERE name ILIKE '%pembrolizumab%'
    - All tables join on nct_id: JOIN ctgov.conditions c ON s.nct_id = c.nct_id
    - Use browse_conditions/browse_interventions for standardized MeSH terms
    - For lead sponsor only: WHERE lead_or_collaborator = 'lead'
    - JOINs can cause row fan-out (duplicate nct_ids). Use SELECT DISTINCT or GROUP BY to deduplicate.
    - Use EXPLAIN ANALYZE to understand query plans and optimize slow queries.

    Example — find trials by drug + condition + phase:
    SELECT DISTINCT s.nct_id, s.brief_title, s.phase, s.overall_status, s.enrollment
    FROM ctgov.studies s
    JOIN ctgov.browse_interventions bi ON s.nct_id = bi.nct_id
    JOIN ctgov.conditions c ON s.nct_id = c.nct_id
    WHERE bi.mesh_term ILIKE '%pembrolizumab%'
      AND c.name ILIKE '%lung%'
      AND s.phase = 'PHASE3'
    ORDER BY s.enrollment DESC NULLS LAST LIMIT 25"""
    app = _get_ctx(ctx)

    query = query.strip()
    # EXPLAIN plans are small; return the whole plan in the preview.
    if query_is_explain(query):
        preview_rows = max_rows

    results, truncated = app.db.execute_query(query, row_limit=max_rows)
    row_count = len(results)
    logger.debug("Query returned %s rows (truncated=%s)", row_count, truncated)

    columns = list(results[0].keys()) if results else []

    app.query_counter += 1
    query_id = f"q{app.query_counter}"

    # Store in multi-slot buffer, evict oldest if full
    if len(app.result_buffers) >= MAX_BUFFERS:
        app.result_buffers.popitem(last=False)
    app.result_buffers[query_id] = ResultBuffer(
        query_id=query_id,
        query=query,
        columns=columns,
        rows=results,
        truncated=truncated,
    )

    summary = QueryResultSummary(
        query_id=query_id,
        columns=columns,
        row_count=row_count,
        truncated=truncated,
        preview=results[:preview_rows],
    )
    result = grounded_result(summary.model_dump())
    if row_count == 0:
        result.append(TextContent(
            type="text",
            text="HINT: Query returned 0 rows. Check your filter values with "
                 "get_column_values — AACT often uses UPPERCASE (e.g. 'PHASE3' not 'Phase 3').",
        ))
    # Warn about duplicate nct_ids from JOIN fan-out
    if "nct_id" in columns and row_count > 1:
        nct_values = [r["nct_id"] for r in results if "nct_id" in r]
        if len(nct_values) != len(set(nct_values)):
            result.append(TextContent(
                type="text",
                text="HINT: Duplicate nct_id values detected — this is likely JOIN fan-out. "
                     "Use SELECT DISTINCT or GROUP BY to deduplicate.",
            ))
    return result


@mcp.tool(annotations=_tool_annotations("Fetch rows", open_world=False))
@_expose_errors
async def fetch_rows(
    query_id: Annotated[str, Field(description="query_id from a previous read_query result")],
    ctx: Context,
    start: Annotated[int, Field(
        description="Row index to start from (0-based)",
        ge=0,
    )] = 0,
    count: Annotated[int, Field(
        description="Number of rows to fetch",
        gt=0, le=100,
    )] = 25,
):
    """Retrieve a page of rows from a buffered result. Up to 5 query buffers are kept
    simultaneously — no need to re-run queries when switching between results.
    No database round-trip — reads from server memory. Use the query_id from read_query.
    If has_more is true, call again with start incremented by count for the next page."""
    app = _get_ctx(ctx)

    if not app.result_buffers:
        raise ValueError(
            "No buffered query result. Call read_query first to execute a query."
        )

    buf = app.result_buffers.get(query_id)
    if buf is None:
        available = list(app.result_buffers.keys())
        raise ValueError(
            f"query_id '{query_id}' not found in buffer. "
            f"Available query IDs: {available}. "
            "Call read_query again to re-execute your query and get a new query_id."
        )

    page = buf.rows[start:start + count]

    result = QueryResultPage(
        rows=page,
        start=start,
        count=len(page),
        total_rows=len(buf.rows),
        has_more=(start + count) < len(buf.rows),
    )
    return grounded_result(result.model_dump())


@mcp.tool(annotations=_tool_annotations("Search columns"))
@_expose_errors
async def search_columns(
    keyword: Annotated[str, Field(description="Keyword to search for in column names (case-insensitive)", min_length=1)],
    ctx: Context,
    table_name: Annotated[str | None, Field(description="Optional: restrict search to a single table")] = None,
):
    """Search for columns across all tables by keyword (case-insensitive partial match).
    Useful for finding where data lives — e.g. search_columns('masking') → designs.masking.
    Optionally filter to a single table with table_name.
    Returns table_name, column_name, and data_type for each match."""
    app = _get_ctx(ctx)

    if table_name is not None:
        _identifier(table_name, "table name")
        results, _ = app.db.execute_query(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'ctgov' "
            "AND column_name ILIKE %s "
            "AND table_name = %s "
            "ORDER BY table_name, column_name",
            {"keyword": f"%{keyword}%", "table_name": table_name},
        )
    else:
        results, _ = app.db.execute_query(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'ctgov' "
            "AND column_name ILIKE %s "
            "ORDER BY table_name, column_name",
            {"keyword": f"%{keyword}%"},
        )
    logger.debug("Found %s columns matching '%s'", len(results), keyword)
    return grounded_result(results)


@mcp.tool(annotations=_tool_annotations("Database info"))
@_expose_errors
async def database_info(ctx: Context):
    """Get database connection info: server time, PostgreSQL version, schema, and table count.
    Call this to confirm the connection is working and check data currency.
    Note: AACT refreshes its data from ClinicalTrials.gov weekly."""
    app = _get_ctx(ctx)

    info_rows, _ = app.db.execute_query(
        "SELECT NOW() AS server_time, version() AS pg_version, current_schema() AS schema_name"
    )
    count_rows, _ = app.db.execute_query(
        "SELECT COUNT(*) AS table_count FROM information_schema.tables WHERE table_schema = 'ctgov'"
    )

    info = info_rows[0]
    info['table_count'] = count_rows[0]['table_count']
    info['note'] = 'AACT data is refreshed weekly from ClinicalTrials.gov.'
    return grounded_result(info)


def main():
    """Main entry point for the server."""
    load_dotenv()
    try:
        mcp.run()
    except Exception as e:
        logger.exception("Server error")
        raise


if __name__ == "__main__":
    main()
