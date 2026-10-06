"""Pydantic models and data structures for AACT MCP server."""
from dataclasses import dataclass
from pydantic import BaseModel, Field
from typing import Any

GROUNDING_NOTICE = (
    "GROUNDING: Every factual claim in your response must trace back to a row in a tool result. "
    "NEVER invent, guess, or recall NCT IDs, drug names, statistics, or study details from memory. "
    "Only data that appears in this response may be cited. "
    "If the data is insufficient, say so and suggest a follow-up query."
)


class TableInfo(BaseModel):
    """Information about a database table or view."""
    table_name: str = Field(..., description="Name of the table or view")
    relation_type: str = Field(..., description="table or view. Views do not have a row estimate.")
    approximate_row_count: int | None = Field(
        None,
        description="Approximate number of rows from PostgreSQL statistics. Null for views and when statistics are missing.",
    )


class ColumnInfo(BaseModel):
    """Information about a database column."""
    column_name: str = Field(..., description="Name of the column")
    data_type: str = Field(..., description="SQL data type of the column")
    character_maximum_length: int | None = Field(None, description="Maximum length for character columns")
    total_distinct: int | None = Field(None, description="Approximate number of distinct values (from pg_stats)")
    sample_values: list[str] | None = Field(None, description="Most common values for low-cardinality columns (≤25 distinct values)")
    example_values: list[str] | None = Field(None, description="Example values from a few rows (for high-cardinality text columns)")


class QueryResultSummary(BaseModel):
    """Summary returned by read_query. Full rows are buffered server-side."""
    query_id: str = Field(..., description="ID to use with fetch_rows to retrieve more data")
    columns: list[str] = Field(..., description="Column names in the result set")
    row_count: int = Field(..., description="Total rows buffered from the query")
    truncated: bool = Field(..., description="True if the query had more rows than max_rows")
    preview: list[dict[str, Any]] = Field(..., description="First N rows as a preview")


class QueryResultPage(BaseModel):
    """A page of rows retrieved from the server-side buffer."""
    rows: list[dict[str, Any]] = Field(..., description="Rows in this page")
    start: int = Field(..., description="Starting row index of this page (0-based)")
    count: int = Field(..., description="Number of rows in this page")
    total_rows: int = Field(..., description="Total rows in the buffer")
    has_more: bool = Field(..., description="True if there are more rows after this page")


@dataclass
class ResultBuffer:
    """Server-side buffer holding the full result set of the most recent query."""
    query_id: str
    query: str
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
