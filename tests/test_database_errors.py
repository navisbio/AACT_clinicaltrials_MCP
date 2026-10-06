"""Error text returned to the client must not include connection details."""

import psycopg2

from src.database import public_database_error


def test_connection_failure_hides_host_and_user() -> None:
    exc = psycopg2.OperationalError(
        'connection to server at "aact-db.ctti-clinicaltrials.org" (1.2.3.4), '
        'port 5432 failed: FATAL: password authentication failed for user "alice"'
    )
    message = public_database_error(exc)
    assert "aact-db" not in message
    assert "alice" not in message
    assert "1.2.3.4" not in message
    assert "password" not in message.lower()
    assert "credentials" in message.lower()


def test_statement_timeout_is_specific() -> None:
    exc = psycopg2.errors.QueryCanceled(
        "canceling statement due to statement timeout"
    )
    message = public_database_error(exc)
    assert "2 minute" in message
    assert "statement timeout" not in message


def test_query_error_keeps_the_column_and_drops_secrets() -> None:
    class Diag:
        message_primary = (
            'column "nonexistent_column_xyz" does not exist '
            "password=hunter2 host=aact-db.ctti-clinicaltrials.org"
        )

    class QueryError(Exception):
        diag = Diag()

    message = public_database_error(QueryError())
    assert "nonexistent_column_xyz" in message
    assert "hunter2" not in message
    assert "aact-db" not in message
    assert "[redacted]" in message


def test_error_without_diagnostics_is_generic() -> None:
    message = public_database_error(psycopg2.ProgrammingError("password=hunter2"))
    assert message == "Database error: the query could not be executed."
    assert "hunter2" not in message
