import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, NoReturn

import psycopg2
import psycopg2.extras
from psycopg2.sql import Composable

from .query_guard import validate_read_only_sql

logger = logging.getLogger("mcp_aact_server.database")

CONNECT_TIMEOUT_SECONDS = 10
POOL_ACQUIRE_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MS = 120_000
LOCK_TIMEOUT_MS = 10_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 120_000
POOL_MAX_CONNECTIONS = 4

_SECRET_TEXT = re.compile(
    r"(?i)\b(?:password|passwd|pwd)\s*=\s*\S+"
    r"|\b(?:host|hostaddr|user|username|dbname|database|port)\s*=\s*\S+"
    r"|postgres(?:ql)?://\S+"
    r"|aact-db\.ctti-clinicaltrials\.org"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"
)


_COLUMN_MESSAGE = re.compile(
    r"""column\s+(?:(?P<qualifier>[A-Za-z_][A-Za-z0-9_]*)\.)?"""
    r"""(?:"(?P<quoted>[^"]+)"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"""
    r"""\s+does not exist""",
    re.IGNORECASE,
)
_RELATION_MESSAGE = re.compile(
    r"""relation\s+"(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"\s+does not exist""",
    re.IGNORECASE,
)
_SQLSTATE_TYPES = {
    "42703": "undefined_column",
    "42P01": "undefined_table",
    "42601": "syntax_error",
    "57014": "query_canceled",
}
_GENERIC_DATABASE_ERROR = "Database error: the query could not be executed."


class ClientDatabaseError(ValueError):
    """A database failure whose text is safe to show to the model."""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        missing_column: str | None = None,
        column_qualifier: str | None = None,
        missing_table: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.missing_column = missing_column
        self.column_qualifier = column_qualifier
        self.missing_table = missing_table

    def as_dict(self, hint: str | None = None) -> dict[str, Any]:
        return {
            "is_error": True,
            "error_type": self.error_type,
            "message": self.message,
            "hint": hint,
        }


def public_database_error(exc: BaseException) -> str:
    """Client-facing text for a database failure. Omits host, user, and password."""
    return client_database_error(exc).message


def client_database_error(exc: BaseException) -> ClientDatabaseError:
    """Classify a database failure without copying connection details."""
    if isinstance(exc, psycopg2.errors.QueryCanceled):
        return ClientDatabaseError(
            "query_canceled",
            "The query was cancelled because it exceeded the 2 minute time limit. "
            "Narrow it with a tighter WHERE or LIMIT and try again.",
        )
    if isinstance(exc, psycopg2.OperationalError):
        return ClientDatabaseError(
            "connection_error",
            "Could not reach the AACT database. "
            "Check your credentials and network, then try again.",
        )
    primary = _diagnostic_message(exc)
    column, qualifier = _missing_column(primary)
    table = _missing_relation(primary)
    if primary:
        return ClientDatabaseError(
            _error_type(exc, column=column, table=table),
            _scrub(primary),
            missing_column=column,
            column_qualifier=qualifier,
            missing_table=table,
        )
    return ClientDatabaseError("database_error", _GENERIC_DATABASE_ERROR)


def _error_type(
    exc: BaseException, *, column: str | None, table: str | None
) -> str:
    code = getattr(exc, "pgcode", None)
    if isinstance(code, str) and code in _SQLSTATE_TYPES:
        return _SQLSTATE_TYPES[code]
    if column:
        return "undefined_column"
    if table:
        return "undefined_table"
    return "database_error"


def _missing_column(primary: str | None) -> tuple[str | None, str | None]:
    if not primary:
        return None, None
    match = _COLUMN_MESSAGE.search(primary)
    if match is None:
        return None, None
    name = match.group("quoted") or match.group("bare")
    return name, match.group("qualifier")


def _missing_relation(primary: str | None) -> str | None:
    if not primary:
        return None
    match = _RELATION_MESSAGE.search(primary)
    if match is None:
        return None
    name = match.group("name")
    schema, separator, table = name.partition(".")
    return table if separator else schema


def _diagnostic_message(exc: BaseException) -> str | None:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    if isinstance(primary, str) and primary.strip():
        return primary.strip()
    return None


def _scrub(text: str) -> str:
    cleaned = _SECRET_TEXT.sub("[redacted]", text)
    return " ".join(cleaned.split())


class _Pool:
    """Small thread-safe pool with a wait timeout.

    Each checkout is an independent connection, so concurrent tool calls do
    not share transaction state. The wait is bounded so a full pool cannot
    hang the server.
    """

    def __init__(
        self,
        max_size: int,
        connect: Callable[[], Any],
        acquire_timeout: float,
    ) -> None:
        self._max_size = max_size
        self._connect = connect
        self._acquire_timeout = acquire_timeout
        self._idle: list[Any] = []
        self._size = 0
        self._closed = False
        self._cond = threading.Condition()

    def acquire(self) -> Any:
        deadline = time.monotonic() + self._acquire_timeout
        while True:
            with self._cond:
                if self._closed:
                    raise RuntimeError("Database connection pool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._size < self._max_size:
                    self._size += 1
                    should_connect = True
                else:
                    should_connect = False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "Timed out waiting for a database connection. "
                            "Try again in a moment."
                        )
                    self._cond.wait(remaining)
                    continue
            if should_connect:
                try:
                    return self._connect()
                except BaseException:
                    with self._cond:
                        self._size -= 1
                        self._cond.notify_all()
                    raise

    def release(self, conn: Any, *, discard: bool = False) -> None:
        if not discard:
            try:
                if conn.closed:
                    discard = True
                else:
                    conn.rollback()
            except Exception:
                discard = True
        with self._cond:
            if self._closed or discard:
                self._size = max(0, self._size - 1)
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                self._idle.append(conn)
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._size = max(0, self._size - len(idle))
            self._cond.notify_all()
        for conn in idle:
            try:
                conn.close()
            except Exception:
                pass


class AACTDatabase:
    def __init__(self) -> None:
        logger.info("Initializing AACT database connection")

        # Fail-hard policy: no defaults, immediate failure if config is missing.
        if "DB_USER" not in os.environ:
            raise ValueError("Missing required environment variable: DB_USER")
        if "DB_PASSWORD" not in os.environ:
            raise ValueError("Missing required environment variable: DB_PASSWORD")

        self.user = os.environ["DB_USER"]
        self.password = os.environ["DB_PASSWORD"]
        self.host = "aact-db.ctti-clinicaltrials.org"
        self.database = "aact"
        self._pool = _Pool(
            POOL_MAX_CONNECTIONS,
            self._connect,
            POOL_ACQUIRE_TIMEOUT_SECONDS,
        )
        try:
            self._test_connection()
        except Exception:
            self.close()
            raise
        logger.info("AACT database initialization complete")

    def close(self) -> None:
        pool = getattr(self, "_pool", None)
        if pool is not None:
            pool.close()

    def _connect(self) -> Any:
        # Startup options apply to every new session. Per-transaction SET LOCAL
        # below reapplies them so a pooled connection cannot keep a timeout
        # or read-only flag cleared by an earlier statement.
        return psycopg2.connect(
            host=self.host,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            application_name="mcp-server-aact",
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={LOCK_TIMEOUT_MS} "
                f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}"
            ),
        )

    def _test_connection(self) -> None:
        logger.debug("Testing database connection to AACT")
        try:
            rows, _ = self.execute_query(
                "SELECT current_database() AS db, current_schema() AS schema"
            )
        except ValueError:
            logger.warning("AACT connection test failed")
            raise
        if not rows:
            raise RuntimeError("Connection test query returned no results")
        logger.info(
            "Connected to database: %s, current schema: %s",
            rows[0]["db"],
            rows[0]["schema"],
        )

    def _begin_read_only(self, conn: Any) -> None:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true), "
                "set_config('idle_in_transaction_session_timeout', %s, true)",
                (
                    str(STATEMENT_TIMEOUT_MS),
                    str(LOCK_TIMEOUT_MS),
                    str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
                ),
            )
            cur.fetchone()

    def _checkout(self) -> Any:
        conn = self._pool.acquire()
        try:
            self._begin_read_only(conn)
            return conn
        except psycopg2.Error:
            logger.warning("Discarding a database connection after session setup failed")
            self._pool.release(conn, discard=True)
        conn = self._pool.acquire()
        try:
            self._begin_read_only(conn)
            return conn
        except psycopg2.Error as exc:
            self._pool.release(conn, discard=True)
            logger.warning(
                "Database session setup failed: %s", public_database_error(exc)
            )
            raise client_database_error(exc) from None

    @contextmanager
    def _connection(self) -> Any:
        conn = self._checkout()
        try:
            yield conn
        finally:
            self._pool.release(conn)

    def execute_query(
        self,
        query: str | Composable,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        row_limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Execute one read-only query and return (rows, truncated).

        Fetches row_limit + 1 rows to detect whether more data exists.
        Returns at most row_limit rows; truncated is True when extra rows were available.
        """
        if isinstance(query, str):
            self._validate(query)

        values = _param_values(params)
        try:
            with self._connection() as conn:
                if not isinstance(query, str):
                    self._validate(query.as_string(conn))
                logged = query if isinstance(query, str) else query.as_string(conn)
                preview = (logged[:200] + "...") if len(logged) > 200 else logged
                logger.debug("Executing query: %s", preview.strip())
                if row_limit:
                    logger.debug("Row limit: %s", row_limit)
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    try:
                        if values:
                            cur.execute(query, values)
                        else:
                            cur.execute(query)
                    except psycopg2.Error as exc:
                        conn.rollback()
                        self._raise_public(exc)
                    if row_limit:
                        results = cur.fetchmany(row_limit + 1)
                        truncated = len(results) > row_limit
                        if truncated:
                            results = results[:row_limit]
                    else:
                        results = cur.fetchall()
                        truncated = False
                    logger.debug(
                        "Query returned %s rows (truncated=%s)", len(results), truncated
                    )
                    return [dict(row) for row in results], truncated
        except TimeoutError as exc:
            raise ClientDatabaseError("connection_error", str(exc)) from None
        except psycopg2.Error as exc:
            self._raise_public(exc)

    def _validate(self, query: str) -> None:
        try:
            validate_read_only_sql(query)
        except ValueError:
            logger.warning("Rejected query: %s", query[:200])
            raise

    def _raise_public(self, exc: psycopg2.Error) -> NoReturn:
        error = client_database_error(exc)
        logger.warning("Database error: %s", error.message)
        raise error from None


def _param_values(
    params: Mapping[str, Any] | Sequence[Any] | None,
) -> list[Any] | None:
    if not params:
        return None
    if isinstance(params, Mapping):
        return list(params.values())
    return list(params)
