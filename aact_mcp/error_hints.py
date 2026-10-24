"""Suggest a real table or column when a read-only query names one that is not there."""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .database import ClientDatabaseError


def recovery_hint(
    query: str,
    error: ClientDatabaseError,
    columns_by_table: Mapping[str, Sequence[str]],
    table_names: Sequence[str] = (),
) -> str | None:
    """Return one catalog hint, or None when no name is clearly closer."""
    if error.error_type == "undefined_table" and error.missing_table:
        match = closest_identifier(error.missing_table, table_names)
        if match and match != error.missing_table:
            return f"Available similar table: {match}"
        return None
    if error.error_type != "undefined_column" or not error.missing_column:
        return None
    candidates = _candidate_tables(query, error, columns_by_table)
    best_score = -1.0
    best: tuple[str, str] | None = None
    tied = False
    for table in candidates:
        columns = columns_by_table.get(table, ())
        match = closest_identifier(error.missing_column, columns)
        if match is None or match == error.missing_column:
            continue
        score = difflib.SequenceMatcher(
            None, error.missing_column.lower(), match.lower()
        ).ratio()
        if best is None or score > best_score:
            best_score = score
            best = (table, match)
            tied = False
        elif score == best_score and best is not None and best[1] != match:
            tied = True
    if best is None or tied:
        return None
    table, column = best
    return f"Available similar column: {table}.{column}"


def closest_identifier(needle: str, names: Sequence[str]) -> str | None:
    """Pick the closest catalog name. Prefer a name the missing identifier extends."""
    if not needle or not names:
        return None
    lowered = needle.lower()
    contained = [
        name
        for name in names
        if name.lower() != lowered
        and (
            lowered.endswith("_" + name.lower())
            or name.lower().endswith("_" + lowered)
        )
    ]
    pool = contained or list(names)
    matches = difflib.get_close_matches(
        lowered, [name.lower() for name in pool], n=1, cutoff=0.6
    )
    if not matches and contained:
        # "other_name" against "name" can score under the cutoff. The token
        # relationship is still the right suggestion when it is unique.
        return contained[0] if len(contained) == 1 else None
    if not matches:
        return None
    target = matches[0]
    for name in pool:
        if name.lower() == target and name.lower() != lowered:
            return name
    return None


def referenced_tables(query: str) -> list[tuple[str, str | None]]:
    """Return (table name, alias) pairs from a PostgreSQL statement."""
    try:
        statement = sqlglot.parse_one(query, read="postgres")
    except ParseError:
        return []
    if statement is None:
        return []
    found: list[tuple[str, str | None]] = []
    for table in statement.find_all(exp.Table):
        name = table.name
        if not isinstance(name, str) or not name:
            continue
        schema_name = table.db if isinstance(table.db, str) else None
        if schema_name not in (None, "", "ctgov"):
            continue
        alias = table.alias or None
        found.append((name, alias if isinstance(alias, str) and alias else None))
    return found


def _candidate_tables(
    query: str,
    error: ClientDatabaseError,
    columns_by_table: Mapping[str, Sequence[str]],
) -> list[str]:
    referenced = referenced_tables(query)
    qualifier = error.column_qualifier
    if qualifier:
        qualified = [
            name
            for name, alias in referenced
            if qualifier in (alias, name) and name in columns_by_table
        ]
        if qualified:
            return list(dict.fromkeys(qualified))
        if qualifier in columns_by_table:
            return [qualifier]
    names = list(dict.fromkeys(name for name, _alias in referenced if name in columns_by_table))
    return names
