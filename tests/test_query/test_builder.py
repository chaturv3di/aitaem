"""
Tests for aitaem.query.builder — QueryGroup dataclass and QueryBuilder static methods.

Post Plan 34: QueryBuilder produces ibis.Table expressions, not SQL strings.
Assertions are expression/schema/executed-data based, or ibis.to_sql(dialect=...)
compilation checks, rather than raw-SQL substring matches.

Sub-feature coverage order (per plan):
  1. QueryGroup dataclass
  2. QueryBuildError
  3. _group_by_source
  4. (table-reference resolution — see ConnectionManager.resolve_table_reference)
  5. _build_metric_value_expr
  6. _build_slice_case_when_expr
  7. _build_segment_case_when_expr
  8. _build_slice_value_expr
  9. _build_metric_segment_query (all four cases + DuckDB validation)
 10. _resolve_slice_components
 11. _build_queries_for_metric
 12. build_queries (integration)
 13. by_entity
 14. wildcard slices
 15. BigQuery-dialect compilation (Gap A regression test)
 16. period-boundary edge case (half-open semantics)
 17. fragment-splicing round-trip fidelity
"""

import ibis
import pandas as pd
import pytest

from aitaem.connectors.connection import ConnectionManager
from aitaem.query.builder import QueryBuilder, QueryGroup
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.utils.exceptions import QueryBuildError

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

DUCKDB_URI = "duckdb://ad_campaigns.duckdb/ad_campaigns"
BIGQUERY_URI = "bigquery://my-project/my_dataset/my_table"


def make_metric(
    name="revenue", agg="sum", numerator="SUM(amount)", denominator=None, source=DUCKDB_URI
):
    return MetricSpec(
        name=name,
        source=source,
        numerator=numerator,
        timestamp_col="event_ts",
        denominator=denominator,
    )


def make_ratio_metric(name="ctr", source=DUCKDB_URI):
    return MetricSpec(
        name=name,
        source=source,
        numerator="SUM(clicks)",
        denominator="SUM(impressions)",
        timestamp_col="event_ts",
    )


def make_slice(name="geo", values=None):
    if values is None:
        values = (
            SliceValue(name="USA", where="country = 'USA'"),
            SliceValue(name="EU", where="country IN ('UK', 'Germany')"),
        )
    return SliceSpec(name=name, values=tuple(values))


def make_segment(name="platform", values=None, source=DUCKDB_URI, entity_id="user_id"):
    if values is None:
        values = (
            SegmentValue(name="Google Ads", where="platform = 'Google Ads'"),
            SegmentValue(name="Meta Ads", where="platform = 'Meta Ads'"),
        )
    return SegmentSpec(name=name, source=source, entity_id=entity_id, values=tuple(values))


def _make_manager(setup_sql: str | None = None) -> ConnectionManager:
    """Fresh in-memory DuckDB ConnectionManager, optionally pre-loaded via setup_sql."""
    manager = ConnectionManager()
    manager.add_connection("duckdb", path=":memory:")
    if setup_sql:
        manager.get_connection("duckdb").connection.raw_sql(setup_sql)
    return manager


# Minimal generic tables matching make_metric()/make_slice()/make_segment()'s
# default columns (source=DUCKDB_URI -> table "ad_campaigns"), for tests that
# only check query *structure* (counts, grouping), not data correctness.
GENERIC_SETUP_SQL = """
CREATE TABLE ad_campaigns AS
SELECT * FROM (VALUES
    ('USA', 'u1', 'Google Ads', TIMESTAMP '2026-01-01', 100.0, 10, 100)
) AS t(country, user_id, platform, event_ts, amount, clicks, impressions);
CREATE TABLE orders AS
SELECT * FROM (VALUES
    (TIMESTAMP '2026-01-01', 50.0)
) AS t(event_ts, amount)
"""


def _make_generic_manager() -> ConnectionManager:
    return _make_manager(GENERIC_SETUP_SQL)


# ---------------------------------------------------------------------------
# 1. QueryGroup dataclass
# ---------------------------------------------------------------------------


class TestQueryGroup:
    def test_instantiate(self):
        m = make_metric()
        expr = ibis.table(schema={"metric_value": "float64"}, name="q")
        qg = QueryGroup(source=DUCKDB_URI, metrics=[m], expressions=[expr])
        assert qg.source == DUCKDB_URI
        assert qg.metrics == [m]
        assert qg.expressions == [expr]

    def test_default_expressions_empty(self):
        m = make_metric()
        qg = QueryGroup(source=DUCKDB_URI, metrics=[m])
        assert qg.expressions == []


# ---------------------------------------------------------------------------
# 2. QueryBuildError
# ---------------------------------------------------------------------------


class TestQueryBuildError:
    def test_raise_and_catch_as_aitaem_error(self):
        from aitaem.utils.exceptions import AitaemError

        with pytest.raises(AitaemError):
            raise QueryBuildError("test error")

    def test_message(self):
        with pytest.raises(QueryBuildError, match="test error"):
            raise QueryBuildError("test error")


# ---------------------------------------------------------------------------
# 3. _group_by_source
# ---------------------------------------------------------------------------


class TestGroupBySource:
    def test_single_source(self):
        metrics = [make_metric("a"), make_metric("b"), make_metric("c")]
        result = QueryBuilder._group_by_source(metrics)
        assert list(result.keys()) == [DUCKDB_URI]
        assert len(result[DUCKDB_URI]) == 3

    def test_two_sources(self):
        source2 = "duckdb://other.db/orders"
        metrics = [
            make_metric("a", source=DUCKDB_URI),
            make_metric("b", source=source2),
            make_metric("c", source=DUCKDB_URI),
        ]
        result = QueryBuilder._group_by_source(metrics)
        assert len(result) == 2
        assert len(result[DUCKDB_URI]) == 2
        assert len(result[source2]) == 1


# ---------------------------------------------------------------------------
# 4. table-reference resolution moved to ConnectionManager.resolve_table_reference
#    (see tests/test_connectors/test_connection_manager.py::TestURIParsing) —
#    QueryBuilder's two call sites are covered by TestBuildMetricSegmentQuery
#    and TestBuildQueries below, which exercise get_table(table_name, database=...)
#    end to end via a live DuckDB connection.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. _build_metric_value_expr
# ---------------------------------------------------------------------------


class TestBuildMetricValueExpr:
    def test_sum(self):
        m = make_metric(agg="sum", numerator="SUM(amount)")
        assert QueryBuilder._build_metric_value_expr(m) == "CAST(SUM(amount) AS DOUBLE)"

    def test_ratio(self):
        m = make_ratio_metric()
        expr = QueryBuilder._build_metric_value_expr(m)
        assert expr == "CAST(SUM(clicks) / NULLIF(SUM(impressions), 0) AS DOUBLE)"

    def test_count(self):
        m = make_metric(agg="count", numerator="COUNT(*)")
        assert QueryBuilder._build_metric_value_expr(m) == "CAST(COUNT(*) AS DOUBLE)"


# ---------------------------------------------------------------------------
# 6. _build_slice_case_when_expr
# ---------------------------------------------------------------------------


class TestBuildSliceCaseWhenExpr:
    def test_contains_all_values(self):
        ss = make_slice()
        result = QueryBuilder._build_slice_case_when_expr(ss, "_slice_geo")
        assert "country = 'USA'" in result
        assert "'USA'" in result
        assert "country IN ('UK', 'Germany')" in result
        assert "'EU'" in result

    def test_ends_with_alias(self):
        ss = make_slice()
        result = QueryBuilder._build_slice_case_when_expr(ss, "_slice_geo")
        assert result.strip().endswith("AS _slice_geo")

    def test_else_null(self):
        ss = make_slice()
        result = QueryBuilder._build_slice_case_when_expr(ss, "_slice_geo")
        assert "ELSE NULL" in result


# ---------------------------------------------------------------------------
# 7. _build_segment_case_when_expr
# ---------------------------------------------------------------------------


class TestBuildSegmentCaseWhenExpr:
    """Post Plan 34: no _dim. qualification — spliced directly onto the DIM
    table's own schema, so unqualified column refs resolve naturally."""

    def test_contains_all_values(self):
        seg = make_segment()
        result = QueryBuilder._build_segment_case_when_expr(seg, "_segment")
        assert "platform = 'Google Ads'" in result
        assert "'Google Ads'" in result
        assert "platform = 'Meta Ads'" in result

    def test_ends_with_alias(self):
        seg = make_segment()
        result = QueryBuilder._build_segment_case_when_expr(seg, "_segment")
        assert result.strip().endswith("AS _segment")

    def test_no_dim_qualification(self):
        """Unlike the pre-Ibis implementation, no _dim. prefix is added — the
        fragment is spliced onto the DIM table's own schema (see
        _build_metric_segment_query), where the column already resolves."""
        seg = make_segment()
        result = QueryBuilder._build_segment_case_when_expr(seg, "_segment")
        assert "_dim." not in result


# ---------------------------------------------------------------------------
# 8. _build_slice_value_expr
# ---------------------------------------------------------------------------


class TestBuildSliceValueExpr:
    def test_no_aliases_returns_literal_all(self):
        t = ibis.table(schema={"x": "string"}, name="t")
        expr = QueryBuilder._build_slice_value_expr(t, [])
        # Compile-level check: literal 'all', no column reference
        sql = ibis.to_sql(t.mutate(slice_value=expr), dialect="duckdb")
        assert "'all'" in sql

    def test_single_alias_returns_column_value(self):
        con = ibis.duckdb.connect()
        con.create_table("t", schema={"_slice_geo": "string"})
        con.raw_sql("INSERT INTO t VALUES ('USA')")
        t = con.table("t")
        expr = QueryBuilder._build_slice_value_expr(t, ["_slice_geo"])
        df = t.mutate(slice_value=expr).execute()
        assert df["slice_value"].iloc[0] == "USA"

    def test_two_aliases_pipe_joined(self):
        con = ibis.duckdb.connect()
        con.create_table("t", schema={"_slice_geo": "string", "_slice_device": "string"})
        con.raw_sql("INSERT INTO t VALUES ('USA', 'mobile')")
        t = con.table("t")
        expr = QueryBuilder._build_slice_value_expr(t, ["_slice_geo", "_slice_device"])
        df = t.mutate(slice_value=expr).execute()
        assert df["slice_value"].iloc[0] == "USA|mobile"

    def test_three_aliases_pipe_joined(self):
        con = ibis.duckdb.connect()
        con.create_table("t", schema={"_a": "string", "_b": "string", "_c": "string"})
        con.raw_sql("INSERT INTO t VALUES ('x', 'y', 'z')")
        t = con.table("t")
        expr = QueryBuilder._build_slice_value_expr(t, ["_a", "_b", "_c"])
        df = t.mutate(slice_value=expr).execute()
        assert df["slice_value"].iloc[0] == "x|y|z"


# ---------------------------------------------------------------------------
# 9. _build_metric_segment_query — all four cases
# ---------------------------------------------------------------------------

SETUP_SQL = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    (1, 'US', 'mobile', TIMESTAMP '2026-01-10', 100.0),
    (2, 'US', 'desktop', TIMESTAMP '2026-01-15', 50.0),
    (3, 'DE', 'mobile', TIMESTAMP '2026-01-20', 80.0),
    (4, 'DE', 'desktop', TIMESTAMP '2026-01-25', 40.0)
) AS t(user_id, country_code, device_type, transaction_date, amount);
CREATE TABLE dim_users AS
SELECT * FROM (VALUES
    (1, 'premium'),
    (2, 'free'),
    (3, 'premium'),
    (4, 'free')
) AS t(user_id, subscription_tier)
"""

_geo_slice = SliceSpec(
    name="geography",
    values=(
        SliceValue(name="North America", where="country_code IN ('US', 'CA', 'MX')"),
        SliceValue(name="Europe", where="country_code IN ('DE', 'FR', 'UK')"),
    ),
)
_device_slice = SliceSpec(
    name="device",
    values=(
        SliceValue(name="mobile", where="device_type = 'mobile'"),
        SliceValue(name="desktop", where="device_type = 'desktop'"),
    ),
)
_user_tier_segment = SegmentSpec(
    name="user_tier",
    source="duckdb://analytics.db/dim_users",
    entity_id="user_id",
    values=(
        SegmentValue(name="premium", where="subscription_tier = 'premium'"),
        SegmentValue(name="free", where="subscription_tier = 'free'"),
    ),
)
_txn_metric = MetricSpec(
    name="revenue",
    source="duckdb://analytics.db/transactions",
    numerator="SUM(amount)",
    timestamp_col="transaction_date",
)


class TestBuildMetricSegmentQuery:
    def _run(
        self,
        metric,
        slices,
        segment,
        time_window=None,
        period_type="all_time",
        period_start=None,
        period_end=None,
        by_entity=None,
    ):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=metric,
            connector=connector,
            slice_specs=slices,
            segment_spec=segment,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            time_window=time_window,
            by_entity=by_entity,
        )
        return expr, expr.execute()

    def test_slices_and_segment(self):
        expr, df = self._run(_txn_metric, [_geo_slice, _device_slice], _user_tier_segment)
        expected_cols = {
            "period_type",
            "period_start_date",
            "period_end_date",
            "metric_name",
            "slice_type",
            "slice_value",
            "segment_name",
            "segment_value",
            "metric_value",
        }
        assert expected_cols.issubset(set(df.columns))
        assert all(df["slice_type"] == "geography|device")
        assert all(df["segment_name"] == "user_tier")
        assert df["metric_value"].notna().all()
        # 2 geo x 2 device x 2 segment = up to 8 combinations (all present here)
        assert len(df) == 4  # one distinct (geo, device) combo per row in this dataset

    def test_slices_only_no_segment(self):
        expr, df = self._run(_txn_metric, [_geo_slice], None)
        assert all(df["segment_name"] == "none")
        assert all(df["segment_value"] == "all")
        assert all(df["slice_type"] == "geography")
        assert set(df["slice_value"]) == {"North America", "Europe"}

    def test_segment_only_no_slices(self):
        expr, df = self._run(_txn_metric, None, _user_tier_segment)
        assert all(df["slice_type"] == "none")
        assert all(df["slice_value"] == "all")
        assert all(df["segment_name"] == "user_tier")
        assert set(df["segment_value"]) == {"premium", "free"}

    def test_no_slices_no_segment(self):
        expr, df = self._run(_txn_metric, None, None)
        assert len(df) == 1
        assert df["slice_type"].iloc[0] == "none"
        assert df["slice_value"].iloc[0] == "all"
        assert df["segment_name"].iloc[0] == "none"
        assert df["segment_value"].iloc[0] == "all"
        assert df["metric_value"].iloc[0] == pytest.approx(270.0)

    def test_time_filter_applied(self):
        expr, df = self._run(
            _txn_metric,
            None,
            None,
            time_window=("2026-01-12", "2026-01-20"),
            period_start="2026-01-12",
            period_end="2026-01-20",
        )
        # Half-open [start, end): only 2026-01-15 row (user 2, US/desktop, amount=50) matches
        assert df["metric_value"].iloc[0] == pytest.approx(50.0)

    def test_period_metadata(self):
        expr, df = self._run(
            _txn_metric,
            None,
            None,
            period_type="all_time",
            period_start="2026-01-01",
            period_end="2026-02-01",
        )
        assert df["period_type"].iloc[0] == "all_time"
        assert df["period_start_date"].iloc[0] == "2026-01-01"
        assert df["period_end_date"].iloc[0] == "2026-02-01"

    def test_period_none_values(self):
        expr, df = self._run(_txn_metric, None, None)
        assert pd.isna(df["period_start_date"].iloc[0])


# ---------------------------------------------------------------------------
# 9b. Segment DIM join — execution-based (no more string qualification checks)
# ---------------------------------------------------------------------------


class TestSegmentDimJoin:
    """DIM-join correctness tests (plan 20; rewritten for Plan 34's Ibis join)."""

    def _run(self, segment, segment_join_key=None):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=segment,
            period_type="all_time",
            period_start=None,
            period_end=None,
            segment_join_key=segment_join_key,
        )
        return expr.execute()

    def test_dim_join_produces_correct_segment_values(self):
        df = self._run(_user_tier_segment)
        assert set(df["segment_value"]) == {"premium", "free"}
        assert all(df["segment_name"] == "user_tier")
        assert df["metric_value"].notna().all()

    def test_no_segment_no_join_needed(self):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        df = expr.execute()
        assert len(df) == 1
        assert df["metric_value"].iloc[0] == pytest.approx(270.0)

    def test_explicit_join_key_used(self):
        setup_sql = """
        CREATE TABLE transactions AS
        SELECT * FROM (VALUES
            (1, 100.0),
            (2, 50.0),
            (3, 80.0),
            (4, 40.0)
        ) AS t(buyer_id, amount);
        CREATE TABLE dim_users AS
        SELECT * FROM (VALUES
            (1, 'premium'),
            (2, 'free'),
            (3, 'premium'),
            (4, 'free')
        ) AS t(user_id, subscription_tier)
        """
        manager = _make_manager(setup_sql)
        connector = manager.get_connection("duckdb")
        seg = SegmentSpec(
            name="tier",
            source="duckdb://analytics.db/dim_users",
            entity_id="user_id",
            join_keys=("buyer_id",),
            values=_user_tier_segment.values,
        )
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=seg,
            period_type="all_time",
            period_start=None,
            period_end=None,
            segment_join_key="buyer_id",
        )
        df = expr.execute()
        assert set(df["segment_value"]) == {"premium", "free"}
        assert df["metric_value"].notna().all()

    def test_default_join_key_falls_back_to_entity_id(self):
        df = self._run(_user_tier_segment, segment_join_key=None)
        # Falls back to entity_id ("user_id"), which is the fact table's own PK here too
        assert set(df["segment_value"]) == {"premium", "free"}


# ---------------------------------------------------------------------------
# 10. _resolve_slice_components
# ---------------------------------------------------------------------------


class TestResolveSliceComponents:
    """Tests for QueryBuilder._resolve_slice_components()."""

    def test_none_returns_none(self):
        result = QueryBuilder._resolve_slice_components(None, None)
        assert result is None

    def test_leaf_spec_returns_list_of_one(self):
        ss = make_slice("geo")
        result = QueryBuilder._resolve_slice_components(ss, None)
        assert result == [ss]

    def test_composite_spec_resolves_from_cache(self):
        from aitaem.specs.loader import SpecCache
        from aitaem.specs.slice import SliceValue

        geo = SliceSpec(name="geo", values=(SliceValue(name="USA", where="country='USA'"),))
        device = SliceSpec(
            name="device", values=(SliceValue(name="mobile", where="device='mobile'"),)
        )
        composite = SliceSpec(name="geo_x_device", cross_product=("geo", "device"))

        cache = SpecCache()
        cache.add(geo)
        cache.add(device)
        cache.add(composite)

        result = QueryBuilder._resolve_slice_components(composite, cache)
        assert result == [geo, device]

    def test_composite_spec_missing_cache_raises(self):
        composite = SliceSpec(name="geo_x_device", cross_product=("geo", "device"))
        with pytest.raises(QueryBuildError, match="requires a SpecCache"):
            QueryBuilder._resolve_slice_components(composite, None)


# ---------------------------------------------------------------------------
# 11. _build_queries_for_metric
# ---------------------------------------------------------------------------


class TestBuildQueriesForMetric:
    def test_one_segment_returns_two_queries(self):
        # 1 segment + no-segment baseline, no slices = 2 queries
        seg = make_segment("platform")
        metric = make_metric()
        manager = _make_generic_manager()
        connector = manager.get_connection("duckdb")
        queries = QueryBuilder._build_queries_for_metric(
            metric=metric,
            connector=connector,
            slice_specs=None,
            segment_spec=seg,
            segment_join_key=None,
            time_window=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        assert len(queries) == 2

    def test_no_segments_returns_two_queries(self):
        # 1 slice spec + no-slice baseline = (1+1) × 1 = 2
        metric = make_metric()
        manager = _make_generic_manager()
        connector = manager.get_connection("duckdb")
        queries = QueryBuilder._build_queries_for_metric(
            metric=metric,
            connector=connector,
            slice_specs=[make_slice()],
            segment_spec=None,
            segment_join_key=None,
            time_window=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        assert len(queries) == 2


# ---------------------------------------------------------------------------
# 12. build_queries — integration
# ---------------------------------------------------------------------------


class TestBuildQueriesIntegration:
    def test_one_source_one_metric_no_segments(self):
        metric = make_metric()
        manager = _make_generic_manager()
        groups = QueryBuilder.build_queries(
            [metric], slice_specs=None, segment_spec=None, connection_manager=manager
        )
        assert len(groups) == 1
        assert len(groups[0].expressions) == 1

    def test_one_source_two_metrics_one_segment(self):
        m1 = make_metric("revenue")
        m2 = make_ratio_metric("ctr")
        seg = make_segment("platform")
        manager = _make_generic_manager()
        groups = QueryBuilder.build_queries(
            [m1, m2], slice_specs=None, segment_spec=seg, connection_manager=manager
        )
        assert len(groups) == 1
        # 2 metrics × (1 segment + 1 no-segment baseline) = 4 queries
        assert len(groups[0].expressions) == 4

    def test_two_sources_correct_grouping(self):
        source2 = "duckdb://ad_campaigns.duckdb/orders"
        m1 = make_metric("revenue", source=DUCKDB_URI)
        m2 = make_metric("orders", source=source2)
        m3 = make_metric("clicks", source=DUCKDB_URI)
        manager = _make_generic_manager()
        groups = QueryBuilder.build_queries(
            [m1, m2, m3], slice_specs=None, segment_spec=None, connection_manager=manager
        )
        assert len(groups) == 2
        sources = {g.source for g in groups}
        assert sources == {DUCKDB_URI, source2}
        counts = {g.source: len(g.expressions) for g in groups}
        assert counts[DUCKDB_URI] == 2  # 2 metrics × 1 query each
        assert counts[source2] == 1

    def test_raises_on_empty_metrics(self):
        manager = _make_generic_manager()
        with pytest.raises(QueryBuildError, match="metric_specs must not be empty"):
            QueryBuilder.build_queries(
                [], slice_specs=None, segment_spec=None, connection_manager=manager
            )

    def test_time_window_filters_by_metric_timestamp_col(self):
        manager = _make_generic_manager()
        metric = make_metric()  # has timestamp_col="event_ts"
        groups = QueryBuilder.build_queries(
            [metric],
            slice_specs=None,
            segment_spec=None,
            connection_manager=manager,
            time_window=("2026-01-01", "2026-02-01"),
        )
        assert len(groups) == 1
        sql = ibis.to_sql(groups[0].expressions[0], dialect="duckdb")
        assert "2026-01-01" in sql
        assert "2026-02-01" in sql

    def test_build_queries_composite_slice_without_cache_raises(self):
        """QueryBuildError raised with clear message when composite slice used without cache."""
        composite = SliceSpec(name="geo_device", cross_product=("geo", "device"))
        metric = make_metric()
        manager = _make_generic_manager()
        with pytest.raises(QueryBuildError, match="requires a SpecCache"):
            QueryBuilder.build_queries(
                [metric],
                slice_specs=[composite],
                segment_spec=None,
                connection_manager=manager,
                spec_cache=None,
            )


# ---------------------------------------------------------------------------
# 13. by_entity
# ---------------------------------------------------------------------------

SETUP_SQL_ENTITY = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    ('u1', 'd1', TIMESTAMP '2026-01-05', 100.0),
    ('u1', 'd2', TIMESTAMP '2026-01-15', 200.0),
    ('u2', 'd1', TIMESTAMP '2026-02-05', 150.0),
    ('u2', 'd2', TIMESTAMP '2026-02-15', 250.0),
    ('u3', 'd1', TIMESTAMP '2026-03-05',  50.0)
) AS t(user_id, device_id, event_ts, amount)
"""

_entity_metric = MetricSpec(
    name="revenue",
    source="duckdb://analytics.db/transactions",
    numerator="SUM(amount)",
    timestamp_col="event_ts",
    entities=["user_id", "device_id"],
)


class TestByEntity:
    def _run(self, metric, by_entity=None, slices=None, segment=None, time_window=None):
        manager = _make_manager(SETUP_SQL_ENTITY)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=metric,
            connector=connector,
            slice_specs=slices,
            segment_spec=segment,
            period_type="all_time",
            period_start=None,
            period_end=None,
            time_window=time_window,
            by_entity=by_entity,
        )
        return expr, expr.execute()

    def test_no_by_entity_entity_id_is_null(self):
        expr, df = self._run(_entity_metric)
        assert "entity_id" in df.columns
        assert df["entity_id"].isna().all()

    def test_no_by_entity_entity_id_is_typed_string_not_bare_null(self):
        """Regression: a bare ibis.null() (untyped) for entity_id compiles fine
        against DuckDB but fails against BigQuery — pyarrow can't cast the
        int64 BigQuery assigns to an untyped NULL literal back into the
        'null' dtype ibis's schema expects (ArrowNotImplementedError:
        Unsupported cast from int64 to null using function cast_null).
        entity_id must carry an explicit string dtype, like metric_format/
        period_start_date/period_end_date already do via _lit_or_null."""
        expr, _ = self._run(_entity_metric)
        assert str(expr.schema()["entity_id"]) == "string"

    def test_by_entity_column_in_select(self):
        expr, df = self._run(_entity_metric, by_entity="user_id")
        assert "entity_id" in df.columns
        assert set(df["entity_id"]) == {"u1", "u2", "u3"}

    def test_by_entity_one_row_per_entity(self):
        expr, df = self._run(_entity_metric, by_entity="user_id")
        assert len(df) == 3

    def test_by_entity_metric_value_is_per_entity_aggregate(self):
        expr, df = self._run(_entity_metric, by_entity="user_id")
        totals = df.set_index("entity_id")["metric_value"]
        assert totals["u1"] == pytest.approx(300.0)
        assert totals["u2"] == pytest.approx(400.0)
        assert totals["u3"] == pytest.approx(50.0)

    def test_by_entity_with_slice(self):
        geo_slice = SliceSpec(
            name="geography",
            values=(
                SliceValue(name="NA", where="user_id IN ('u1', 'u2')"),
                SliceValue(name="Other", where="user_id = 'u3'"),
            ),
        )
        expr, df = self._run(_entity_metric, by_entity="user_id", slices=[geo_slice])
        assert "entity_id" in df.columns
        assert set(df["entity_id"]) == {"u1", "u2", "u3"}
        assert set(df["slice_value"]) == {"NA", "Other"}

    def test_by_entity_monthly_one_row_per_entity_per_month(self):
        manager = _make_manager(SETUP_SQL_ENTITY)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_entity_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="monthly",
            period_start=None,
            period_end=None,
            time_window=("2026-01-01", "2026-04-01"),
            by_entity="user_id",
        )
        df = expr.execute()
        # u1: Jan only (2 rows), u2: Feb only (2 rows), u3: Mar only (1 row) → 3 rows
        assert len(df) == 3
        assert set(df["entity_id"]) == {"u1", "u2", "u3"}


class TestByEntityBuildQueriesValidation:
    """build_queries() validates by_entity against metric.entities.

    Validation happens before any connector lookup, so a fresh (unconfigured)
    manager works fine for the error-path tests.
    """

    def test_by_entity_not_in_entities_raises(self):
        metric = MetricSpec(
            name="revenue",
            source=DUCKDB_URI,
            numerator="SUM(amount)",
            timestamp_col="event_ts",
            entities=["user_id"],
        )
        manager = _make_generic_manager()
        with pytest.raises(QueryBuildError, match="by_entity='device_id'"):
            QueryBuilder.build_queries(
                [metric],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                by_entity="device_id",
            )

    def test_by_entity_with_entities_none_raises(self):
        metric = make_metric()  # entities=None
        manager = _make_generic_manager()
        with pytest.raises(QueryBuildError, match="by_entity='user_id'"):
            QueryBuilder.build_queries(
                [metric],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                by_entity="user_id",
            )

    def test_by_entity_valid_passes(self):
        metric = MetricSpec(
            name="revenue",
            source=DUCKDB_URI,
            numerator="SUM(amount)",
            timestamp_col="event_ts",
            entities=["user_id", "device_id"],
        )
        manager = _make_generic_manager()
        groups = QueryBuilder.build_queries(
            [metric],
            slice_specs=None,
            segment_spec=None,
            connection_manager=manager,
            by_entity="user_id",
        )
        assert len(groups) == 1

    def test_by_entity_none_skips_validation(self):
        metric = make_metric()  # entities=None, by_entity=None → ok
        manager = _make_generic_manager()
        groups = QueryBuilder.build_queries(
            [metric],
            slice_specs=None,
            segment_spec=None,
            connection_manager=manager,
            by_entity=None,
        )
        assert len(groups) == 1


# ---------------------------------------------------------------------------
# 14. Wildcard slice — _build_wildcard_slice_expr and execution
# ---------------------------------------------------------------------------

SETUP_SQL_WILDCARD = """
CREATE TABLE campaigns AS
SELECT * FROM (VALUES
    ('c1', 'SaaS',    'Search',  100.0),
    ('c2', 'Fintech', 'Display', 200.0),
    ('c3', 'SaaS',    'Video',   150.0),
    ('c4', 'EdTech',  'Search',   50.0),
    ('c5', NULL,      'Display',  80.0)
) AS t(campaign_id, industry, campaign_type, spend)
"""

_wildcard_metric = MetricSpec(
    name="total_spend",
    source="duckdb://analytics.db/campaigns",
    numerator="SUM(spend)",
    timestamp_col="event_ts",
)

_wildcard_slice = SliceSpec(name="industry", column="industry")
_wildcard_campaign_type_slice = SliceSpec(name="campaign_type", column="campaign_type")
_leaf_campaign_type_slice = SliceSpec(
    name="campaign_type",
    values=(
        SliceValue(name="Search", where="campaign_type = 'Search'"),
        SliceValue(name="Display", where="campaign_type = 'Display'"),
    ),
)


class TestBuildWildcardSliceExpr:
    """Unit tests for _build_wildcard_slice_expr."""

    def test_emits_cast_expression(self):
        expr = QueryBuilder._build_wildcard_slice_expr(_wildcard_slice, "_slice_industry")
        assert "CAST(industry AS VARCHAR)" in expr
        assert "AS _slice_industry" in expr

    def test_does_not_contain_case_when(self):
        expr = QueryBuilder._build_wildcard_slice_expr(_wildcard_slice, "_slice_industry")
        assert "CASE" not in expr
        assert "WHEN" not in expr


class TestWildcardSliceExecution:
    """End-to-end DuckDB execution tests for wildcard slices."""

    def _compute(self, slice_specs, segment_spec=None):
        manager = _make_manager(SETUP_SQL_WILDCARD)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_wildcard_metric,
            connector=connector,
            slice_specs=slice_specs,
            segment_spec=segment_spec,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        return expr.execute()

    def test_wildcard_excludes_null_column_values(self):
        df = self._compute([_wildcard_slice])
        assert None not in df["slice_value"].values
        assert df["slice_value"].isna().sum() == 0

    def test_wildcard_produces_one_row_per_distinct_value(self):
        df = self._compute([_wildcard_slice])
        assert set(df["slice_value"]) == {"SaaS", "Fintech", "EdTech"}
        assert len(df) == 3

    def test_wildcard_metric_values_correct(self):
        df = self._compute([_wildcard_slice])
        row = df[df["slice_value"] == "SaaS"].iloc[0]
        assert row["metric_value"] == pytest.approx(250.0)  # 100 + 150
        row = df[df["slice_value"] == "Fintech"].iloc[0]
        assert row["metric_value"] == pytest.approx(200.0)
        row = df[df["slice_value"] == "EdTech"].iloc[0]
        assert row["metric_value"] == pytest.approx(50.0)

    def test_wildcard_slice_type_is_slice_name(self):
        df = self._compute([_wildcard_slice])
        assert (df["slice_type"] == "industry").all()

    def test_wildcard_two_slices_cross_product(self):
        df = self._compute([_wildcard_slice, _wildcard_campaign_type_slice])
        # Each combination of (industry, campaign_type) that is non-NULL in both columns
        # SaaS×Search, SaaS×Video, Fintech×Display, EdTech×Search → 4 rows
        assert len(df) == 4
        assert (df["slice_type"] == "industry|campaign_type").all()
        values = set(df["slice_value"])
        assert "SaaS|Search" in values
        assert "SaaS|Video" in values
        assert "Fintech|Display" in values
        assert "EdTech|Search" in values

    def test_wildcard_mixed_with_leaf_slice(self):
        df = self._compute([_wildcard_slice, _leaf_campaign_type_slice])
        assert df["metric_value"].notna().all()
        assert set(df["slice_type"]) == {"industry|campaign_type"}


# ---------------------------------------------------------------------------
# 15. BigQuery-dialect compilation — direct regression test for Gap A
# ---------------------------------------------------------------------------


class TestBigQueryDialectCompilation:
    """Gap A: non-all_time queries used to emit VALUES-with-column-list SQL,
    which BigQuery's grammar rejects. These now compile cleanly."""

    def test_non_all_time_compiles_under_bigquery_dialect(self):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="monthly",
            period_start=None,
            period_end=None,
            time_window=("2026-01-01", "2026-03-01"),
        )
        sql = ibis.to_sql(expr, dialect="bigquery")
        assert "VALUES" not in sql.upper().replace("BOOLVALUES", "")  # no VALUES CTE

    def test_non_all_time_with_segment_compiles_under_bigquery_dialect(self):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=[_geo_slice],
            segment_spec=_user_tier_segment,
            period_type="weekly",
            period_start=None,
            period_end=None,
            time_window=("2026-01-01", "2026-02-01"),
        )
        # Must not raise
        ibis.to_sql(expr, dialect="bigquery")

    def test_all_time_compiles_under_bigquery_dialect(self):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=[_geo_slice],
            segment_spec=_user_tier_segment,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        ibis.to_sql(expr, dialect="bigquery")

    def test_non_all_time_compiles_under_postgres_dialect(self):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="daily",
            period_start=None,
            period_end=None,
            time_window=("2026-01-01", "2026-01-05"),
        )
        ibis.to_sql(expr, dialect="postgres")


# ---------------------------------------------------------------------------
# 16. Period-boundary edge case — half-open [period_start, period_end)
# ---------------------------------------------------------------------------


class TestPeriodBoundaryHalfOpen:
    """A row exactly at period_end belongs to the next period, not the current
    one; a row exactly at period_start belongs to the current period."""

    SETUP = """
    CREATE TABLE transactions AS
    SELECT * FROM (VALUES
        (1, TIMESTAMP '2026-01-01 00:00:00', 10.0),  -- exactly period_start of Jan
        (2, TIMESTAMP '2026-02-01 00:00:00', 20.0),  -- exactly period_end of Jan / start of Feb
        (3, TIMESTAMP '2026-01-15 00:00:00', 30.0)   -- middle of Jan
    ) AS t(id, transaction_date, amount)
    """

    def test_boundary_row_excluded_from_current_included_in_next(self):
        manager = _make_manager(self.SETUP)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="monthly",
            period_start=None,
            period_end=None,
            time_window=("2026-01-01", "2026-03-01"),
        )
        df = expr.execute()
        by_period = df.set_index("period_start_date")["metric_value"]
        # Jan bucket: id=1 (exactly start) + id=3 (middle) = 40; id=2 excluded (goes to Feb)
        assert by_period["2026-01-01 00:00:00"] == pytest.approx(40.0)
        # Feb bucket: id=2 (exactly at what was Jan's period_end) = 20
        assert by_period["2026-02-01 00:00:00"] == pytest.approx(20.0)

    def test_all_time_time_window_boundary_half_open(self):
        manager = _make_manager(self.SETUP)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="all_time",
            period_start="2026-01-01",
            period_end="2026-02-01",
            time_window=("2026-01-01", "2026-02-01"),
        )
        df = expr.execute()
        # id=1 (>= start) + id=3 (middle) included; id=2 (== end) excluded
        assert df["metric_value"].iloc[0] == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# 17. Fragment-splicing — parse coverage across dialects + round-trip fidelity
# ---------------------------------------------------------------------------


class TestFragmentSplicingDialectCoverage:
    """Non-trivial numerator/where fragments compile via Table.sql() under
    each supported dialect — real signal on sqlglot's dialect coverage."""

    def test_numerator_with_case_when_compiles_all_dialects(self):
        metric = MetricSpec(
            name="conditional_revenue",
            source="duckdb://analytics.db/transactions",
            numerator="SUM(CASE WHEN amount > 50 THEN amount ELSE 0 END)",
            timestamp_col="transaction_date",
        )
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=metric,
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        for dialect in ("duckdb", "bigquery", "postgres"):
            ibis.to_sql(expr, dialect=dialect)

    def test_where_with_null_handling_compiles_all_dialects(self):
        slice_spec = SliceSpec(
            name="high_value",
            values=(
                SliceValue(
                    name="big", where="amount IS NOT NULL AND amount > 75"
                ),
                SliceValue(name="small", where="amount IS NOT NULL AND amount <= 75"),
            ),
        )
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=[slice_spec],
            segment_spec=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        for dialect in ("duckdb", "bigquery", "postgres"):
            ibis.to_sql(expr, dialect=dialect)

    def test_segment_where_reemits_in_target_dialect(self):
        """Segment where used to hardcode duckdb dialect on reprint; now it
        re-emits in the query's actual target dialect."""
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=_txn_metric,
            connector=connector,
            slice_specs=None,
            segment_spec=_user_tier_segment,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        bq_sql = ibis.to_sql(expr, dialect="bigquery")
        # BigQuery quotes identifiers with backticks, not DuckDB's double quotes
        assert '"subscription_tier"' not in bq_sql


class TestFragmentSplicingRoundTripFidelity:
    """The real regression surface for fragment splicing: Table.sql() is a
    parse-and-reprint pass (even same-dialect), not a passthrough. Backend-
    specific constructs must survive intact — this is a tripwire against a
    future sqlglot upgrade silently mangling one."""

    def test_bigquery_safe_divide_survives_round_trip(self):
        # Same-dialect parse+reprint via sqlglot — the exact mechanism
        # Table.sql() uses internally — must preserve this construct's token.
        import sqlglot

        fragment = "SAFE_DIVIDE(amount, 2)"
        reprinted = sqlglot.parse_one(fragment, read="bigquery").sql(dialect="bigquery")
        assert "SAFE_DIVIDE" in reprinted

    def test_bigquery_countif_survives_round_trip(self):
        import sqlglot

        fragment = "COUNTIF(amount > 50)"
        reprinted = sqlglot.parse_one(fragment, read="bigquery").sql(dialect="bigquery")
        assert "COUNTIF" in reprinted

    def test_duckdb_list_aggregate_survives_round_trip(self):
        import sqlglot

        fragment = "list_aggregate(amount, 'sum')"
        reprinted = sqlglot.parse_one(fragment, read="duckdb").sql(dialect="duckdb")
        assert "list_aggregate" in reprinted.lower()
