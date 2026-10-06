"""Reject anything other than a single read-only PostgreSQL statement.

`read_query` accepts SQL written by the model, so placeholders are not enough.
The statement is parsed with PostgreSQL syntax and then checked for writes,
row locks, stacked statements, and functions that still have side effects in
a read-only transaction. The database session is also read-only; this layer
rejects the query before it is sent.
"""

from __future__ import annotations

import warnings
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

MAX_QUERY_CHARS = 100_000

# Functions that can read server files, open other connections, cancel
# backends, or change session settings even when the transaction is read-only.
_BLOCKED_FUNCTIONS = frozenset(
    {
        "dblink",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_exec",
        "dblink_send_query",
        "lo_export",
        "lo_from_bytea",
        "lo_import",
        "lo_put",
        "lo_unlink",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_all",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_cancel_backend",
        "pg_create_restore_point",
        "pg_logical_emit_message",
        "pg_ls_archive_statusdir",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_tmpdir",
        "pg_ls_waldir",
        "pg_promote",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_stat_file",
        "pg_terminate_backend",
        "set_config",
    }
)

_READ_ONLY_MESSAGE = (
    "Only a single read-only SELECT, WITH (CTE), or EXPLAIN statement is allowed."
)

QueryKind = Literal["query", "explain"]


def validate_read_only_sql(sql: str) -> QueryKind:
    """Return ``explain`` or ``query``. Raise ValueError when the SQL is not read-only."""
    statement = _single_statement(sql)
    if isinstance(statement, exp.Command) and _command_name(statement) == "EXPLAIN":
        inner = _single_statement(_explain_body(statement))
        _assert_read_only(inner)
        return "explain"
    _assert_read_only(statement)
    return "query"


def query_is_explain(sql: str) -> bool:
    """True when `sql` is an EXPLAIN of a read-only statement."""
    try:
        return validate_read_only_sql(sql) == "explain"
    except ValueError:
        return False


def _single_statement(sql: str) -> exp.Expr:
    if sql is None or not str(sql).strip():
        raise ValueError("The query is empty.")
    if len(sql) > MAX_QUERY_CHARS:
        raise ValueError(
            f"The query exceeds {MAX_QUERY_CHARS} characters. "
            "Shorten it and try again."
        )
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*unsupported syntax.*")
            parsed = sqlglot.parse(sql, read="postgres")
    except ParseError:
        raise ValueError(
            "Could not parse the query as PostgreSQL. " + _READ_ONLY_MESSAGE
        ) from None
    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise ValueError("The query is empty.")
    if len(statements) > 1:
        raise ValueError(
            "Only a single SQL statement is allowed. "
            "Remove the extra statement after the semicolon."
        )
    return statements[0]


def _assert_read_only(statement: exp.Expr) -> None:
    if not isinstance(statement, _read_roots()):
        raise ValueError(_READ_ONLY_MESSAGE)
    for node in statement.walk():
        if isinstance(node, exp.Into):
            raise ValueError("SELECT INTO is not allowed. Use a plain SELECT.")
        if isinstance(node, exp.Lock):
            raise ValueError(
                "Row locking (FOR UPDATE / FOR SHARE) is not allowed."
            )
        if isinstance(node, _forbidden_nodes()):
            raise ValueError(_READ_ONLY_MESSAGE)
        function_name = _function_name(node)
        if function_name and function_name.lower() in _BLOCKED_FUNCTIONS:
            raise ValueError(
                f"The function {function_name.lower()} is not allowed. "
                "Queries must be read-only."
            )


def _explain_body(statement: exp.Command) -> str:
    raw = _node_text(statement.args.get("expression"))
    body = _strip_explain_options(raw).strip()
    if not body:
        raise ValueError("EXPLAIN is missing the statement to explain.")
    return body


def _strip_explain_options(text: str) -> str:
    """Remove a leading EXPLAIN option list or ANALYZE/VERBOSE keywords."""
    rest = text.strip()
    if rest.startswith("("):
        depth = 0
        for index, char in enumerate(rest):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    rest = rest[index + 1 :].strip()
                    break
        else:
            raise ValueError("Could not parse the EXPLAIN options.")
    while True:
        matched = False
        for keyword in ("ANALYZE", "ANALYSE", "VERBOSE"):
            if _starts_with_keyword(rest, keyword):
                rest = rest[len(keyword) :].strip()
                matched = True
                break
        if not matched:
            return rest


def _starts_with_keyword(text: str, keyword: str) -> bool:
    if not text.upper().startswith(keyword):
        return False
    if len(text) == len(keyword):
        return True
    return not (text[len(keyword)].isalnum() or text[len(keyword)] == "_")


def _command_name(statement: exp.Command) -> str:
    name = statement.this
    if isinstance(name, str):
        return name.upper()
    return str(name).upper()


def _node_text(node: object) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, exp.Literal):
        value = node.this
        return value if isinstance(value, str) else str(value)
    if isinstance(node, exp.Expr):
        return node.sql(dialect="postgres")
    return str(node)


def _function_name(node: exp.Expr) -> str | None:
    if isinstance(node, exp.Anonymous):
        name = node.name
        return name if isinstance(name, str) else None
    if isinstance(node, exp.Func):
        try:
            name = node.sql_name()
        except Exception:
            return None
        return name if isinstance(name, str) else None
    return None


def _read_roots() -> tuple[type[exp.Expr], ...]:
    return _existing_types("Select", "Union", "Intersect", "Except", "Values")


def _forbidden_nodes() -> tuple[type[exp.Expr], ...]:
    return _existing_types(
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Drop",
        "Create",
        "Alter",
        "TruncateTable",
        "Command",
        "Copy",
        "Grant",
        "Revoke",
        "Set",
        "Transaction",
        "Commit",
        "Rollback",
        "Use",
    )


def _existing_types(*names: str) -> tuple[type[exp.Expr], ...]:
    found: list[type[exp.Expr]] = []
    for name in names:
        node_type = getattr(exp, name, None)
        if isinstance(node_type, type):
            found.append(node_type)
    return tuple(found)
