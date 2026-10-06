"""Column and table hints for failed read-only queries. No database connection."""

from src.database import ClientDatabaseError
from src.error_hints import closest_identifier, recovery_hint, referenced_tables
from src.server import relation_summary


def test_view_row_estimate_is_null_and_table_zero_stays_zero() -> None:
    assert relation_summary("v", 0) == ("view", None)
    assert relation_summary("m", 0) == ("view", None)
    assert relation_summary("r", 0) == ("table", 0)
    assert relation_summary("r", -1) == ("table", None)
    assert relation_summary("r", 12) == ("table", 12)


def test_alias_maps_to_the_real_table() -> None:
    tables = referenced_tables(
        "SELECT ion.other_name FROM ctgov.intervention_other_names ion LIMIT 1"
    )
    assert tables == [("intervention_other_names", "ion")]


def test_hint_names_the_extended_column() -> None:
    error = ClientDatabaseError(
        "undefined_column",
        'column ion.other_name does not exist',
        missing_column="other_name",
        column_qualifier="ion",
    )
    hint = recovery_hint(
        "SELECT ion.other_name FROM ctgov.intervention_other_names ion LIMIT 1",
        error,
        {"intervention_other_names": ["id", "nct_id", "name"]},
    )
    assert hint == "Available similar column: intervention_other_names.name"


def test_unrelated_column_has_no_hint() -> None:
    error = ClientDatabaseError(
        "undefined_column",
        'column "nonexistent_column_xyz" does not exist',
        missing_column="nonexistent_column_xyz",
    )
    hint = recovery_hint(
        "SELECT nonexistent_column_xyz FROM ctgov.studies LIMIT 1",
        error,
        {"studies": ["nct_id", "brief_title", "phase", "overall_status"]},
    )
    assert hint is None


def test_closest_identifier_rejects_a_weak_match() -> None:
    assert closest_identifier("nonexistent_column_xyz", ["nct_id", "brief_title"]) is None
    assert closest_identifier("other_name", ["id", "nct_id", "name"]) == "name"
