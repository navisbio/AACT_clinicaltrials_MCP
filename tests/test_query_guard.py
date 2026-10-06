"""Parser checks for read_query. These do not connect to the database."""

import pytest

from src.query_guard import validate_read_only_sql


ALLOWED = [
    "SELECT nct_id, brief_title FROM ctgov.studies LIMIT 20",
    "WITH t AS (SELECT nct_id FROM ctgov.studies LIMIT 3) SELECT * FROM t",
    "-- fetch studies\nSELECT nct_id FROM ctgov.studies LIMIT 3",
    "/* note */ SELECT 1",
    "SELECT DISTINCT s.nct_id FROM ctgov.studies s "
    "JOIN ctgov.browse_interventions bi ON s.nct_id = bi.nct_id "
    "WHERE bi.mesh_term ILIKE '%pembrolizumab%' AND s.phase = 'PHASE3' "
    "ORDER BY s.enrollment DESC NULLS LAST LIMIT 25",
    "SELECT c.reltuples::bigint AS approximate_row_count FROM pg_class c",
    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
    "SELECT * FROM t WHERE a = %(name)s",
    "SELECT * FROM t WHERE a ILIKE '%s'",
    "SELECT '; DROP TABLE studies' AS x",
    "SELECT DISTINCT ON (nct_id) nct_id FROM ctgov.studies ORDER BY nct_id",
    "SELECT COUNT(*) FILTER (WHERE overall_status = 'RECRUITING') FROM ctgov.studies",
    "SELECT nct_id FROM ctgov.studies WHERE brief_title ~ 'lung'",
    "SELECT nct_id FROM ctgov.studies WHERE start_date > CURRENT_DATE - INTERVAL '1 year'",
    "SELECT nct_id, ROW_NUMBER() OVER (PARTITION BY phase ORDER BY enrollment DESC NULLS LAST) "
    "FROM ctgov.studies",
    "SELECT nct_id FROM ctgov.studies WHERE phase = ANY(ARRAY['PHASE2','PHASE3'])",
    "SELECT 1 UNION ALL SELECT 2",
    "VALUES (1), (2)",
    'SELECT "phase" AS value, COUNT(*) AS count FROM ctgov."studies" '
    'WHERE "phase" IS NOT NULL GROUP BY "phase" ORDER BY count DESC LIMIT %s',
    'SELECT * FROM ctgov."studies" LIMIT 3',
    "EXPLAIN SELECT nct_id FROM ctgov.studies LIMIT 1",
    "EXPLAIN ANALYZE SELECT nct_id FROM ctgov.studies LIMIT 1",
    "EXPLAIN (ANALYZE, BUFFERS) SELECT 1",
    "EXPLAIN ANALYZE VERBOSE SELECT 1",
]


REJECTED = [
    "INSERT INTO studies (nct_id) VALUES ('test')",
    "UPDATE ctgov.studies SET brief_title = 'x'",
    "DELETE FROM studies",
    "DROP TABLE studies",
    "SELECT 1; DROP TABLE studies",
    "SELECT 1; SELECT 2",
    "WITH d AS (DELETE FROM ctgov.studies RETURNING *) SELECT * FROM d",
    "EXPLAIN ANALYZE DELETE FROM ctgov.studies",
    "EXPLAIN SELECT 1; DROP TABLE t",
    "SELECT * INTO new_table FROM ctgov.studies",
    "SELECT nct_id FROM ctgov.studies FOR UPDATE",
    "COPY ctgov.studies TO STDOUT",
    "CREATE TABLE t AS SELECT 1",
    "SET statement_timeout = 0",
    "GRANT SELECT ON ctgov.studies TO public",
    "TRUNCATE ctgov.studies",
    "/* SELECT 1 */ DROP TABLE studies",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_catalog.pg_read_file('/etc/passwd')",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT dblink_exec('host=example', 'DELETE FROM t')",
    "SELECT pg_sleep(1000)",
    "SELECT pg_terminate_backend(1)",
    "",
    "   ",
    "-- just a comment",
]


@pytest.mark.parametrize("query", ALLOWED)
def test_allows_read_only_sql(query: str) -> None:
    kind = validate_read_only_sql(query)
    if query.lstrip().upper().startswith("EXPLAIN") or query.lstrip().startswith("--"):
        # Leading comments are not explain. Only a real EXPLAIN is classified as such.
        pass
    if "EXPLAIN" in query.upper().split()[0:1] or query.lstrip().upper().startswith("EXPLAIN"):
        assert kind == "explain"
    else:
        assert kind == "query"


@pytest.mark.parametrize("query", REJECTED)
def test_rejects_unsafe_sql(query: str) -> None:
    with pytest.raises(ValueError):
        validate_read_only_sql(query)


def test_select_into_message_names_the_problem() -> None:
    with pytest.raises(ValueError, match="SELECT INTO"):
        validate_read_only_sql("SELECT * INTO new_table FROM ctgov.studies")


def test_blocked_function_message_names_the_function() -> None:
    with pytest.raises(ValueError, match="pg_read_file"):
        validate_read_only_sql("SELECT pg_read_file('/etc/passwd')")


def test_stacked_statement_message() -> None:
    with pytest.raises(ValueError, match="single SQL statement"):
        validate_read_only_sql("SELECT 1; DROP TABLE studies")


def test_string_literal_containing_drop_is_allowed() -> None:
    assert validate_read_only_sql("SELECT '; DROP TABLE studies' AS x") == "query"


def test_query_is_explain_helper() -> None:
    from src.query_guard import query_is_explain

    assert query_is_explain("EXPLAIN SELECT 1") is True
    assert query_is_explain("SELECT 1") is False
    assert query_is_explain("EXPLAIN ANALYZE DELETE FROM t") is False
