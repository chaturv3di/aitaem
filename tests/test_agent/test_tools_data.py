"""data group, with the v2 tests of list_tables, describe_table, date_range, column_distribution,
and their query helpers carried over to ToolResult and artifacts."""

from __future__ import annotations

import dataclasses
import datetime
from unittest.mock import MagicMock

import ibis
import pyarrow as pa
import pytest

from aitaem import ConnectionManager
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import distribution_agg, reject_unsafe_filter, stringify_bounds
from aitaem.agent.tools.data import (
    column_distribution,
    data_toolset,
    date_range,
    describe_table,
    list_tables,
    sample_rows,
)
from aitaem.agent.trace import Status
from aitaem.utils.exceptions import AitaemConnectionError, ConnectionNotFoundError
from tests.test_agent.conftest import make_ctx

SOURCE = "duckdb://ad_campaigns.duckdb/ad_campaigns"


def _mock_deps(make_deps, cm: MagicMock) -> SessionDeps:
    cm.requires_compute_lock = False
    return make_deps(connection_manager=cm)


def test_toolset_names() -> None:
    assert sorted(data_toolset.tools) == [
        "column_distribution",
        "date_range",
        "describe_table",
        "list_tables",
        "sample_rows",
    ]


# ── list_tables ────────────────────────────────────────────────────────────


def test_list_tables_real_backend(deps: SessionDeps) -> None:
    result = list_tables(make_ctx(deps))
    assert "duckdb://:memory:/ad_campaigns" in result.facts["tables"]["duckdb"]
    assert deps.store.get(result.artifact_id).type_tag == "table_list"


def test_list_tables_omits_names_with_no_resolvable_uri(make_deps) -> None:
    cm = MagicMock()
    cm.backend_types = ["bigquery"]
    connector = MagicMock()
    connector.list_tables.return_value = ["good_table", "ambiguous_table"]
    connector.build_source_uri.side_effect = lambda name: (
        "bigquery://proj/ds.good_table" if name == "good_table" else None
    )
    cm.get_connection.return_value = connector

    result = list_tables(make_ctx(_mock_deps(make_deps, cm)))

    assert result.facts["tables"]["bigquery"] == ["bigquery://proj/ds.good_table"]


def test_list_tables_one_backend_fails(make_deps) -> None:
    cm = MagicMock()
    cm.backend_types = ["duckdb", "bigquery"]

    def get_conn(bt: str) -> MagicMock:
        if bt == "bigquery":
            raise AitaemConnectionError("BQ auth failed")
        conn = MagicMock()
        conn.list_tables.return_value = ["events"]
        conn.build_source_uri.side_effect = lambda name: f"duckdb://db.duckdb/{name}"
        return conn

    cm.get_connection.side_effect = get_conn

    result = list_tables(make_ctx(_mock_deps(make_deps, cm)))

    assert result.facts["tables"] == {"duckdb": ["duckdb://db.duckdb/events"]}
    assert "BQ auth failed" in result.facts["errors"]["bigquery"]


def test_list_tables_single_backend_specified_fails(make_deps) -> None:
    cm = MagicMock()
    cm.get_connection.side_effect = ConnectionNotFoundError("not found")

    result = list_tables(make_ctx(_mock_deps(make_deps, cm)), backend_type="duckdb")

    assert result.facts["tables"] == {}
    assert "duckdb" in result.facts["errors"]


# ── describe_table ─────────────────────────────────────────────────────────


def test_describe_table_returns_columns(deps: SessionDeps) -> None:
    result = describe_table(make_ctx(deps), SOURCE)
    assert result.facts["columns"]["date"] == "date"
    assert result.facts["columns"]["revenue"] == "float64"
    assert len(result.facts["columns"]) == 10


@pytest.mark.parametrize(
    "source", ["duckdb://ad_campaigns.duckdb/no_such_table", "xyz://db/t", "not-a-uri"]
)
def test_describe_table_errors(deps: SessionDeps, source: str) -> None:
    result = describe_table(make_ctx(deps), source)
    assert result.status == Status.error
    assert deps.store.get(result.artifact_id).type_tag == "error"


# ── sample_rows ────────────────────────────────────────────────────────────


def test_sample_rows(deps: SessionDeps) -> None:
    result = sample_rows(make_ctx(deps), SOURCE, n=3)
    assert len(result.facts["rows"]) == 3
    assert "revenue" in result.facts["columns"]
    ref = deps.store.get(result.artifact_id)
    assert (ref.kind, ref.type_tag) == ("table", "sample")


def test_sample_rows_caps_n(deps: SessionDeps) -> None:
    result = sample_rows(make_ctx(deps), SOURCE, n=500)
    assert len(result.facts["rows"]) == 20


# ── date_range ─────────────────────────────────────────────────────────────


def test_date_range_success(deps: SessionDeps) -> None:
    result = date_range(make_ctx(deps), SOURCE, "date")
    assert result.status == Status.ok
    assert result.facts["count"] == 1800
    assert result.facts["min_val"] == "2024-01-01"
    assert result.facts["distinct_count"] > 0
    assert set(result.facts) == {
        "source",
        "column",
        "count",
        "null_count",
        "min_val",
        "max_val",
        "distinct_count",
    }


def test_date_range_rejects_non_temporal_column(deps: SessionDeps) -> None:
    result = date_range(make_ctx(deps), SOURCE, "revenue")
    assert result.status == Status.error
    assert "not temporal" in result.summary


@pytest.mark.parametrize(
    ("source", "column", "filter"),
    [
        (SOURCE, "bogus_col", None),
        ("bigquery://no-such-project/no_such_dataset.no_such_table", "date", None),
        (SOURCE, "date", "x IN (SELECT y FROM dim_platforms)"),
        (SOURCE, "date", "nonexistent_col > 5"),
    ],
)
def test_date_range_errors(deps: SessionDeps, source: str, column: str, filter: str | None) -> None:
    result = date_range(make_ctx(deps), source, column, filter)
    assert result.status == Status.error


def test_date_range_filter_narrows_results(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    unfiltered = date_range(ctx, SOURCE, "date")
    filtered = date_range(ctx, SOURCE, "date", filter="revenue > 1000")
    assert filtered.facts["count"] < unfiltered.facts["count"]


# ── column_distribution ────────────────────────────────────────────────────


def test_column_distribution_default_column_is_timestamp_col(deps: SessionDeps) -> None:
    result = column_distribution(make_ctx(deps), "total_revenue")
    assert result.facts["column"] == "date"
    assert result.facts["count"] == 1800
    datetime.date.fromisoformat(result.facts["min_val"])
    ref = deps.store.get(result.artifact_id)
    assert (ref.kind, ref.type_tag) == ("table", "distribution")


def test_column_distribution_explicit_numeric_column(deps: SessionDeps) -> None:
    result = column_distribution(make_ctx(deps), "total_revenue", column="revenue")
    assert result.facts["mean"] is not None
    assert "distinct_count" not in result.facts


def test_column_distribution_takes_catalog_metrics_only(deps: SessionDeps) -> None:
    result = column_distribution(make_ctx(deps), SOURCE)
    assert result.status == Status.error
    assert "Unknown metric" in result.summary


@pytest.mark.parametrize(
    ("column", "filter"),
    [("bogus_col", None), (None, "x IN (SELECT y FROM dim_platforms)"), ("revenue", "nope > 5")],
)
def test_column_distribution_errors(
    deps: SessionDeps, column: str | None, filter: str | None
) -> None:
    result = column_distribution(make_ctx(deps), "total_revenue", column=column, filter=filter)
    assert result.status == Status.error


def test_column_distribution_filter_narrows_results(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    unfiltered = column_distribution(ctx, "total_revenue", column="revenue")
    filtered = column_distribution(ctx, "total_revenue", column="revenue", filter="revenue > 1000")
    assert filtered.facts["count"] < unfiltered.facts["count"]


def test_column_distribution_no_column_no_timestamp_col(make_deps, example_specs) -> None:
    spec = example_specs.metrics["total_revenue"]
    example_specs.update(dataclasses.replace(spec, timestamp_col=None))
    deps = make_deps(spec_cache=example_specs)
    result = column_distribution(make_ctx(deps), "total_revenue")
    assert "timestamp_col" in result.summary


def test_data_tools_take_compute_lock(make_deps, example_specs) -> None:
    deps = make_deps(spec_cache=example_specs)
    seen: list[bool] = []
    original = deps.connection_manager.get_connection_for_source

    def spy(source: str):  # type: ignore[no-untyped-def]
        seen.append(deps.compute_lock.locked())
        return original(source)

    deps.connection_manager.get_connection_for_source = spy  # type: ignore[method-assign]
    date_range(make_ctx(deps), SOURCE, "date")
    assert seen == [True]
    assert ConnectionManager().requires_compute_lock is False


# ── query helpers ──────────────────────────────────────────────────────────


def test_distribution_agg_numeric_and_non_numeric() -> None:
    numeric = distribution_agg(ibis.memtable({"v": [1.0, 2.0, 3.0, None]}), "v", None)
    row = numeric.to_pyarrow().to_pylist()[0]
    assert (row["count"], row["null_count"], row["mean"]) == (3, 1, pytest.approx(2.0))
    assert "distinct_count" not in numeric.columns
    text = distribution_agg(ibis.memtable({"v": ["a", "b", "a", None]}), "v", None)
    assert text.to_pyarrow().to_pylist()[0]["distinct_count"] == 2
    assert "mean" not in text.columns


def test_distribution_agg_grouped() -> None:
    agg = distribution_agg(ibis.memtable({"g": ["a", "a", "b"], "v": [1.0, 2.0, 3.0]}), "v", ["g"])
    counts = {r["g"]: r["count"] for r in agg.to_pyarrow().to_pylist()}
    assert counts == {"a": 2, "b": 1}


def test_distribution_agg_uses_approx_quantile_on_bigquery() -> None:
    agg = distribution_agg(ibis.memtable({"v": [1.0, 2.0, 3.0]}), "v", None)
    sql = ibis.to_sql(agg, dialect="bigquery").upper()
    assert "APPROX_QUANTILES" in sql
    assert "QUANTILE(" not in sql


def test_stringify_bounds_iso8601() -> None:
    arrow = pa.table({"min_val": [datetime.datetime(2024, 1, 1, 5)], "max_val": [42], "count": [1]})
    out = stringify_bounds(arrow).to_pylist()[0]
    assert out == {"min_val": "2024-01-01T05:00:00", "max_val": "42", "count": 1}


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery", "postgres"])
@pytest.mark.parametrize(
    "bad_filter",
    [
        "x IN (SELECT y FROM other)",
        "EXISTS (SELECT 1 FROM other WHERE other.id = t.id)",
        "x > (SELECT MAX(y) FROM other)",
    ],
)
def test_reject_unsafe_filter_rejects_subqueries(dialect: str, bad_filter: str) -> None:
    with pytest.raises(ValueError, match="subquer"):
        reject_unsafe_filter(bad_filter, dialect)


@pytest.mark.parametrize("bad", ["1=1; DROP TABLE x", "1=1) UNION SELECT * FROM x --"])
def test_reject_unsafe_filter_rejects_stacking_and_breakout(bad: str) -> None:
    with pytest.raises(ValueError):
        reject_unsafe_filter(bad, "duckdb")


def test_reject_unsafe_filter_accepts_and_neutralizes() -> None:
    ok = reject_unsafe_filter("order_value > 1000 AND (country = 'US' OR country = 'IN')", "duckdb")
    assert "1000" in ok and "US" in ok
    commented = reject_unsafe_filter("1=1 -- ' AND x", "duckdb")
    assert "/*" in commented and "*/" in commented
