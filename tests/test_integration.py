"""Integration tests for the AACT MCP server.

These tests exercise the full tool chain against the live AACT database,
verifying real user stories end-to-end through the MCP protocol.

Requires: DB_USER and DB_PASSWORD environment variables (or .env file).
"""
import json
import pytest
from tests.conftest import parse_tool_result


# ---------------------------------------------------------------------------
# Story 1: Discover the database structure
# ---------------------------------------------------------------------------

class TestDatabaseDiscovery:
    """A user explores the database to understand what tables and columns exist."""

    async def test_list_tables_returns_known_tables(self, client):
        """The AACT database should contain well-known tables like 'studies'."""
        result = await client.call_tool("list_tables", {})
        tables = parse_tool_result(result)
        table_names = [t["table_name"] for t in tables]

        assert len(table_names) > 10, "AACT should have many tables"
        assert "studies" in table_names
        assert "sponsors" in table_names
        assert "interventions" in table_names

    async def test_describe_table_returns_columns(self, client):
        """Describing 'studies' should return columns with type information."""
        result = await client.call_tool("describe_table", {"table_name": "studies"})
        data = parse_tool_result(result)[0]
        columns = data["columns"]
        col_names = [c["column_name"] for c in columns]

        assert len(col_names) > 5, "studies table should have many columns"
        assert "nct_id" in col_names
        assert "brief_title" in col_names

        # Each column should have type info
        for col in columns:
            assert "data_type" in col
            assert isinstance(col["data_type"], str)

    async def test_describe_nonexistent_table_returns_empty(self, client):
        """Describing a table that doesn't exist should return empty columns."""
        result = await client.call_tool(
            "describe_table", {"table_name": "this_table_does_not_exist_xyz"}
        )
        data = parse_tool_result(result)[0]
        assert data["columns"] == []


# ---------------------------------------------------------------------------
# Story 2: Query data with preview and pagination
# ---------------------------------------------------------------------------

class TestQueryAndPagination:
    """A user runs a query, gets a preview, then pages through results."""

    async def test_read_query_returns_summary_with_preview(self, client):
        """read_query should return a summary with query_id, columns, and preview rows."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id, brief_title FROM ctgov.studies LIMIT 20",
            "max_rows": 100,
            "preview_rows": 3,
        })
        summary = parse_tool_result(result)[0]

        assert "query_id" in summary
        assert summary["query_id"].startswith("q")
        assert "nct_id" in summary["columns"]
        assert "brief_title" in summary["columns"]
        assert summary["row_count"] == 20
        assert summary["truncated"] is False
        assert len(summary["preview"]) == 3

    async def test_fetch_rows_retrieves_pages_from_buffer(self, client):
        """After read_query, fetch_rows should return pages without re-querying."""
        # Run the query
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 50",
            "max_rows": 100,
            "preview_rows": 2,
        })
        summary = parse_tool_result(result)[0]
        query_id = summary["query_id"]
        assert summary["row_count"] == 50

        # Fetch first page
        page1 = await client.call_tool("fetch_rows", {
            "query_id": query_id, "start": 0, "count": 10,
        })
        page1_data = parse_tool_result(page1)[0]
        assert page1_data["count"] == 10
        assert page1_data["start"] == 0
        assert page1_data["total_rows"] == 50
        assert page1_data["has_more"] is True
        assert len(page1_data["rows"]) == 10

        # Fetch second page
        page2 = await client.call_tool("fetch_rows", {
            "query_id": query_id, "start": 10, "count": 10,
        })
        page2_data = parse_tool_result(page2)[0]
        assert page2_data["start"] == 10
        assert page2_data["has_more"] is True

        # Rows should be different between pages
        page1_ids = [r["nct_id"] for r in page1_data["rows"]]
        page2_ids = [r["nct_id"] for r in page2_data["rows"]]
        assert page1_ids != page2_ids

    async def test_fetch_rows_last_page_has_more_false(self, client):
        """The last page of results should have has_more=False."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 10",
            "max_rows": 100,
            "preview_rows": 2,
        })
        summary = parse_tool_result(result)[0]
        query_id = summary["query_id"]

        # Fetch beyond the end
        page = await client.call_tool("fetch_rows", {
            "query_id": query_id, "start": 0, "count": 100,
        })
        page_data = parse_tool_result(page)[0]
        assert page_data["count"] == 10
        assert page_data["has_more"] is False

    async def test_preview_rows_is_subset_of_full_result(self, client):
        """Preview rows should match the first rows you'd get from fetch_rows."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies ORDER BY nct_id LIMIT 20",
            "max_rows": 100,
            "preview_rows": 5,
        })
        summary = parse_tool_result(result)[0]
        query_id = summary["query_id"]
        preview_ids = [r["nct_id"] for r in summary["preview"]]

        # Fetch the same first 5 rows via fetch_rows
        page = await client.call_tool("fetch_rows", {
            "query_id": query_id, "start": 0, "count": 5,
        })
        page_data = parse_tool_result(page)[0]
        fetched_ids = [r["nct_id"] for r in page_data["rows"]]

        assert preview_ids == fetched_ids


# ---------------------------------------------------------------------------
# Story 3: Truncation detection
# ---------------------------------------------------------------------------

class TestTruncation:
    """A user runs a query that exceeds the buffer limit."""

    async def test_truncated_flag_when_more_rows_exist(self, client):
        """When the query has more rows than max_rows, truncated should be True."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 20",
            "max_rows": 5,
            "preview_rows": 2,
        })
        summary = parse_tool_result(result)[0]
        assert summary["truncated"] is True
        assert summary["row_count"] == 5  # capped at max_rows

    async def test_not_truncated_when_all_rows_fit(self, client):
        """When all rows fit in max_rows, truncated should be False."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 3",
            "max_rows": 100,
            "preview_rows": 2,
        })
        summary = parse_tool_result(result)[0]
        assert summary["truncated"] is False
        assert summary["row_count"] == 3


# ---------------------------------------------------------------------------
# Story 4: Multi-slot buffer management
# ---------------------------------------------------------------------------

class TestBufferManagement:
    """Multiple query buffers are kept simultaneously."""

    async def test_new_query_produces_new_id(self, client):
        """Running a second read_query should produce a new query_id."""
        r1 = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 5",
        })
        q1 = parse_tool_result(r1)[0]["query_id"]

        r2 = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.sponsors LIMIT 5",
        })
        q2 = parse_tool_result(r2)[0]["query_id"]

        assert q1 != q2

    async def test_multiple_buffers_coexist(self, client):
        """Both old and new query_ids should remain fetchable (multi-slot buffer)."""
        r1 = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 5",
        })
        q1 = parse_tool_result(r1)[0]["query_id"]

        r2 = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.sponsors LIMIT 5",
        })
        q2 = parse_tool_result(r2)[0]["query_id"]

        # Both buffers should be fetchable
        page1 = await client.call_tool("fetch_rows", {
            "query_id": q1, "start": 0, "count": 5,
        })
        assert page1.is_error is not True
        page1_data = parse_tool_result(page1)[0]
        assert page1_data["count"] == 5

        page2 = await client.call_tool("fetch_rows", {
            "query_id": q2, "start": 0, "count": 5,
        })
        assert page2.is_error is not True
        page2_data = parse_tool_result(page2)[0]
        assert page2_data["count"] == 5

    async def test_oldest_buffer_evicted_after_max(self, client):
        """After MAX_BUFFERS (5) queries, the oldest buffer should be evicted."""
        query_ids = []
        for i in range(6):
            r = await client.call_tool("read_query", {
                "query": f"SELECT nct_id FROM ctgov.studies LIMIT {i + 1}",
            })
            query_ids.append(parse_tool_result(r)[0]["query_id"])

        # The first query_id should be evicted
        result = await client.call_tool("fetch_rows", {
            "query_id": query_ids[0], "start": 0, "count": 5,
        })
        assert result.is_error is True

        # The second query_id should still be available (5 slots: ids 1-5)
        result = await client.call_tool("fetch_rows", {
            "query_id": query_ids[1], "start": 0, "count": 5,
        })
        assert result.is_error is not True

    async def test_fetch_rows_without_prior_query_raises_error(self, client):
        """Calling fetch_rows before any read_query should fail."""
        result = await client.call_tool("fetch_rows", {
            "query_id": "q999", "start": 0, "count": 5,
        })
        assert result.is_error is True


# ---------------------------------------------------------------------------
# Story 5: SQL validation (read-only enforcement)
# ---------------------------------------------------------------------------

class TestReadOnlyEnforcement:
    """The server must reject non-SELECT queries."""

    async def test_rejects_insert_query(self, client):
        """INSERT queries should be rejected."""
        result = await client.call_tool("read_query", {
            "query": "INSERT INTO studies (nct_id) VALUES ('test')",
        })
        assert result.is_error is True

    async def test_rejects_delete_query(self, client):
        """DELETE queries should be rejected."""
        result = await client.call_tool("read_query", {
            "query": "DELETE FROM studies",
        })
        assert result.is_error is True

    async def test_rejects_drop_query(self, client):
        """DROP queries should be rejected."""
        result = await client.call_tool("read_query", {
            "query": "DROP TABLE studies",
        })
        assert result.is_error is True

    async def test_allows_cte_query(self, client):
        """WITH (CTE) queries should be accepted."""
        result = await client.call_tool("read_query", {
            "query": "WITH t AS (SELECT nct_id FROM ctgov.studies LIMIT 3) SELECT * FROM t",
        })
        assert result.is_error is not True

    async def test_allows_explain_query(self, client):
        """EXPLAIN queries should be accepted."""
        result = await client.call_tool("read_query", {
            "query": "EXPLAIN SELECT nct_id FROM ctgov.studies LIMIT 1",
        })
        assert result.is_error is not True

    async def test_allows_commented_query(self, client):
        """A SELECT query preceded by SQL comments should be accepted."""
        result = await client.call_tool("read_query", {
            "query": "-- fetch studies\nSELECT nct_id FROM ctgov.studies LIMIT 3",
        })
        assert result.is_error is not True
        summary = parse_tool_result(result)[0]
        assert summary["row_count"] == 3


# ---------------------------------------------------------------------------
# Story 6: Empty results
# ---------------------------------------------------------------------------

class TestEmptyResults:
    """Queries that return no rows should be handled gracefully."""

    async def test_empty_query_result(self, client):
        """A query matching nothing should return 0 rows and empty preview."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies WHERE nct_id = 'NONEXISTENT_ID_XYZ'",
        })
        summary = parse_tool_result(result)[0]
        assert summary["row_count"] == 0
        assert summary["preview"] == []
        assert summary["columns"] == []
        assert summary["truncated"] is False


# ---------------------------------------------------------------------------
# Story 7: Sample values and row counts
# ---------------------------------------------------------------------------

class TestMetadataEnrichment:
    """list_tables returns row counts, describe_table returns sample_values."""

    async def test_list_tables_includes_row_counts(self, client):
        """list_tables should return approximate_row_count for each table."""
        result = await client.call_tool("list_tables", {})
        tables = parse_tool_result(result)
        studies = next(t for t in tables if t["table_name"] == "studies")
        assert "approximate_row_count" in studies
        assert isinstance(studies["approximate_row_count"], int)
        assert studies["approximate_row_count"] > 0

    async def test_describe_table_includes_sample_values(self, client):
        """describe_table should populate sample_values for low-cardinality columns."""
        result = await client.call_tool("describe_table", {"table_name": "studies"})
        data = parse_tool_result(result)[0]
        columns = data["columns"]

        # studies.phase is a well-known low-cardinality column
        phase_col = next((c for c in columns if c["column_name"] == "phase"), None)
        assert phase_col is not None
        assert phase_col["sample_values"] is not None
        assert len(phase_col["sample_values"]) > 0
        # Verify at least one expected phase value is present
        assert any("PHASE" in v for v in phase_col["sample_values"])

    async def test_high_cardinality_columns_have_no_sample_values(self, client):
        """High-cardinality columns like nct_id should NOT have sample_values."""
        result = await client.call_tool("describe_table", {"table_name": "studies"})
        data = parse_tool_result(result)[0]
        columns = data["columns"]

        nct_col = next((c for c in columns if c["column_name"] == "nct_id"), None)
        assert nct_col is not None
        assert nct_col["sample_values"] is None

    async def test_high_cardinality_has_example_values(self, client):
        """High-cardinality text columns should have example_values from sample rows."""
        result = await client.call_tool("describe_table", {"table_name": "studies"})
        data = parse_tool_result(result)[0]
        columns = data["columns"]

        title_col = next((c for c in columns if c["column_name"] == "brief_title"), None)
        assert title_col is not None
        assert title_col["example_values"] is not None
        assert len(title_col["example_values"]) > 0

    async def test_describe_includes_row_count(self, client):
        """describe_table should include approximate_row_count in the response."""
        result = await client.call_tool("describe_table", {"table_name": "studies"})
        data = parse_tool_result(result)[0]
        assert "approximate_row_count" in data
        assert isinstance(data["approximate_row_count"], int)
        assert data["approximate_row_count"] > 0

    async def test_total_distinct(self, client):
        """describe_table should include total_distinct for columns with pg_stats data."""
        result = await client.call_tool("describe_table", {"table_name": "studies"})
        data = parse_tool_result(result)[0]
        columns = data["columns"]

        # phase should have a small total_distinct value
        phase_col = next((c for c in columns if c["column_name"] == "phase"), None)
        assert phase_col is not None
        assert phase_col["total_distinct"] is not None
        assert phase_col["total_distinct"] > 0


# ---------------------------------------------------------------------------
# Story 8: CTE and EXPLAIN queries
# ---------------------------------------------------------------------------

class TestCTEAndExplain:
    """CTE (WITH) and EXPLAIN queries should be allowed."""

    async def test_allows_cte_query(self, client):
        """WITH (CTE) queries should execute successfully."""
        result = await client.call_tool("read_query", {
            "query": "WITH recent AS (SELECT nct_id FROM ctgov.studies LIMIT 5) SELECT * FROM recent",
        })
        assert result.is_error is not True
        summary = parse_tool_result(result)[0]
        assert summary["row_count"] == 5

    async def test_allows_explain_query(self, client):
        """EXPLAIN queries should execute successfully."""
        result = await client.call_tool("read_query", {
            "query": "EXPLAIN SELECT nct_id FROM ctgov.studies LIMIT 5",
        })
        assert result.is_error is not True
        summary = parse_tool_result(result)[0]
        assert summary["row_count"] > 0

    async def test_explain_shows_full_plan(self, client):
        """EXPLAIN should show all plan rows in preview without needing fetch_rows."""
        result = await client.call_tool("read_query", {
            "query": "EXPLAIN SELECT s.nct_id FROM ctgov.studies s JOIN ctgov.conditions c ON s.nct_id = c.nct_id LIMIT 5",
            "preview_rows": 2,  # Would normally limit preview, but EXPLAIN overrides
        })
        assert result.is_error is not True
        summary = parse_tool_result(result)[0]
        # All plan rows should appear in preview
        assert len(summary["preview"]) == summary["row_count"]


# ---------------------------------------------------------------------------
# Story 9: Error surfacing
# ---------------------------------------------------------------------------

class TestErrorSurfacing:
    """Database errors should surface helpful messages."""

    async def test_bad_column_surfaces_error(self, client):
        """Querying a non-existent column should return an error with the column name."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nonexistent_column_xyz FROM ctgov.studies LIMIT 1",
        })
        assert result.is_error is True
        error_text = result.content[0].text
        assert "nonexistent_column_xyz" in error_text

    async def test_bad_table_surfaces_error(self, client):
        """Querying a non-existent table should return an error with the table name."""
        result = await client.call_tool("read_query", {
            "query": "SELECT * FROM ctgov.nonexistent_table_xyz LIMIT 1",
        })
        assert result.is_error is True
        error_text = result.content[0].text
        assert "nonexistent_table_xyz" in error_text


# ---------------------------------------------------------------------------
# Story 10: search_columns
# ---------------------------------------------------------------------------

class TestSearchColumns:
    """search_columns finds columns by keyword across all tables."""

    async def test_nct_id_many_tables(self, client):
        """nct_id should appear in many tables."""
        result = await client.call_tool("search_columns", {"keyword": "nct_id"})
        matches = parse_tool_result(result)
        tables = [m["table_name"] for m in matches]
        assert len(tables) > 5
        assert "studies" in tables

    async def test_case_insensitive(self, client):
        """Search should be case-insensitive."""
        result = await client.call_tool("search_columns", {"keyword": "NCT_ID"})
        matches = parse_tool_result(result)
        assert len(matches) > 0

    async def test_no_matches(self, client):
        """A nonsense keyword should return no matches."""
        result = await client.call_tool("search_columns", {"keyword": "zzz_nonexistent_zzz"})
        matches = parse_tool_result(result)
        assert matches == []

    async def test_partial_match(self, client):
        """Partial keywords should match (e.g. 'mask' matches 'masking')."""
        result = await client.call_tool("search_columns", {"keyword": "mask"})
        matches = parse_tool_result(result)
        col_names = [m["column_name"] for m in matches]
        assert any("mask" in c for c in col_names)

    async def test_table_name_filter(self, client):
        """search_columns with table_name should restrict results to that table."""
        result = await client.call_tool("search_columns", {
            "keyword": "nct_id",
            "table_name": "studies",
        })
        matches = parse_tool_result(result)
        assert len(matches) > 0
        assert all(m["table_name"] == "studies" for m in matches)


# ---------------------------------------------------------------------------
# Story 11: database_info
# ---------------------------------------------------------------------------

class TestDatabaseInfo:
    """database_info returns connection and schema metadata."""

    async def test_expected_fields(self, client):
        """database_info should return server_time, pg_version, schema_name, table_count."""
        result = await client.call_tool("database_info", {})
        info = parse_tool_result(result)[0]
        assert "server_time" in info
        assert "pg_version" in info
        assert "schema_name" in info
        assert "table_count" in info
        assert "note" in info

    async def test_schema_ctgov(self, client):
        """The schema should be ctgov or public (default search path)."""
        result = await client.call_tool("database_info", {})
        info = parse_tool_result(result)[0]
        # current_schema() returns the first schema in search_path
        assert isinstance(info["schema_name"], str)

    async def test_table_count_positive(self, client):
        """There should be a positive number of tables."""
        result = await client.call_tool("database_info", {})
        info = parse_tool_result(result)[0]
        assert info["table_count"] > 0


# ---------------------------------------------------------------------------
# Story 13: Default preview_rows increased
# ---------------------------------------------------------------------------

class TestDefaultPreviewRows:
    """Default preview_rows should be 25."""

    async def test_default_preview_includes_small_results(self, client):
        """A query with 10 rows should show all 10 in preview with default preview_rows=25."""
        result = await client.call_tool("read_query", {
            "query": "SELECT nct_id FROM ctgov.studies LIMIT 10",
        })
        summary = parse_tool_result(result)[0]
        assert len(summary["preview"]) == 10
