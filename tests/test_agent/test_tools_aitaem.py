"""aitaem group, with the v2 tests of validate_spec, commit_spec, and delete_spec carried over to
ToolResult and artifacts."""

from __future__ import annotations

from pathlib import Path

import fsspec
import pytest

from aitaem import ConnectionManager, MetricCompute, SliceSpec, SpecCache
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools.aitaem import (
    ConnectionParams,
    MetricComputeParams,
    aitaem_toolset,
    check_compatibility,
    commit_spec,
    compute_metrics,
    connect_source,
    delete_spec,
    find_specs,
    list_specs,
    read_spec,
    validate_spec,
)
from aitaem.agent.trace import Status
from tests.test_agent.conftest import EXAMPLES, make_ctx

SOURCE = "duckdb://ad_campaigns.duckdb/ad_campaigns"

CPC_YAML = f"""\
metric:
  name: cpc
  description: Cost per click
  source: {SOURCE}
  numerator: "SUM(ad_spend)"
  denominator: "SUM(clicks)"
  timestamp_col: date
"""

COUNTRY_YAML = """\
slice:
  name: country
  values:
    - name: US
      where: "country = 'USA'"
"""


def test_toolset_names() -> None:
    assert sorted(aitaem_toolset.tools) == [
        "check_compatibility",
        "commit_spec",
        "compute_metrics",
        "connect_source",
        "delete_spec",
        "find_specs",
        "list_specs",
        "read_spec",
        "validate_spec",
    ]


# ── connect_source ─────────────────────────────────────────────────────────


def test_connect_source_duckdb(make_deps) -> None:
    deps = make_deps(connection_manager=ConnectionManager())
    params = ConnectionParams(
        backend_type="duckdb", config={"path": str(EXAMPLES / "data/ad_campaigns.duckdb")}
    )
    result = connect_source(make_ctx(deps), params)
    assert result.status == Status.ok
    assert result.facts["backends"] == ["duckdb"]
    assert deps.connection_manager.requires_compute_lock


def test_connect_source_existing_backend_is_error(deps: SessionDeps) -> None:
    params = ConnectionParams(backend_type="duckdb", config={"path": ":memory:"})
    result = connect_source(make_ctx(deps), params)
    assert result.status == Status.error
    assert "already exists" in result.summary


def test_connect_source_postgres_refused(make_deps) -> None:
    deps = make_deps(connection_manager=ConnectionManager())
    params = ConnectionParams(backend_type="postgres", config={"host": "db"})
    result = connect_source(make_ctx(deps), params)
    assert result.status == Status.refused
    assert "ConnectionManager passed to AitaemAgent" in result.summary
    assert deps.connection_manager.backend_types == []


# ── list_specs, find_specs, read_spec ──────────────────────────────────────


def test_list_specs(deps: SessionDeps) -> None:
    result = list_specs(make_ctx(deps))
    assert len(result.facts["metric"]) == 6
    assert {s["name"] for s in result.facts["slice"]} == {"campaign_type", "geo", "industry"}
    only = list_specs(make_ctx(deps), spec_type="segment")
    assert list(only.facts) == ["segment"]


@pytest.mark.parametrize(
    ("spec_type", "query", "first"),
    [
        ("metric", "ctr", "ctr"),  # name
        ("metric", "click through", "ctr"),  # description
        ("metric", "total revenue", "total_revenue"),  # name and description
        ("slice", "geographic regions", "geo"),  # description
        ("segment", "platform", "platform"),  # name and entity
        ("segment", "dim_platforms", "platform"),  # source
    ],
)
def test_find_specs_ranks_matches(
    deps: SessionDeps, spec_type: str, query: str, first: str
) -> None:
    result = find_specs(make_ctx(deps), spec_type, query)  # type: ignore[arg-type]
    assert result.status == Status.ok
    assert result.facts["matches"][0]["name"] == first


def test_find_specs_entities_and_limit(make_deps, example_specs: SpecCache) -> None:
    example_specs.add(
        __import__("aitaem").MetricSpec.from_yaml(
            f"metric:\n  name: spend_per_country\n  source: {SOURCE}\n"
            '  numerator: "SUM(ad_spend)"\n  timestamp_col: date\n  entities: [country]\n'
        )
    )
    deps = make_deps(spec_cache=example_specs)
    result = find_specs(make_ctx(deps), "metric", "country", limit=1)
    assert [m["name"] for m in result.facts["matches"]] == ["spend_per_country"]
    assert len(find_specs(make_ctx(deps), "metric", "revenue", limit=2).facts["matches"]) == 2


def test_find_specs_matches_source(deps: SessionDeps) -> None:
    result = find_specs(make_ctx(deps), "metric", "ad_campaigns")
    assert len(result.facts["matches"]) == 6


def test_find_specs_no_match_is_empty(deps: SessionDeps) -> None:
    result = find_specs(make_ctx(deps), "metric", "churn")
    assert result.status == Status.empty
    assert result.facts["matches"] == []


def test_read_spec(deps: SessionDeps) -> None:
    result = read_spec(make_ctx(deps), "metric", "ctr")
    assert "SUM(clicks)" in result.facts["yaml"]
    assert deps.store.get(result.artifact_id).type_tag == "spec"


def test_read_spec_unknown_is_error(deps: SessionDeps) -> None:
    assert read_spec(make_ctx(deps), "slice", "nope").status == Status.error


# ── check_compatibility ────────────────────────────────────────────────────


def _full_scan(deps: SessionDeps) -> dict[tuple[str, str], bool]:
    scan = MetricCompute(deps.spec_cache, deps.connection_manager).scan()
    return {(r.metric_name, r.spec_name): r.compatible for r in scan.results}


def test_check_compatibility_matches_full_scan(make_deps, example_specs: SpecCache) -> None:
    example_specs.add(
        SliceSpec.from_yaml("slice:\n  name: geo_x_industry\n  cross_product: [geo, industry]\n")
    )
    example_specs.add(
        SliceSpec.from_yaml(
            "slice:\n  name: by_device\n  values:\n    - name: m\n      where: \"device = 'm'\"\n"
        )
    )
    deps = make_deps(spec_cache=example_specs)
    result = check_compatibility(
        make_ctx(deps),
        metrics=["ctr", "roas"],
        slices=["geo_x_industry", "by_device"],
        segments=["platform"],
    )
    full = _full_scan(deps)
    got = {(v["metric"], v["spec"]): v["compatible"] for v in result.facts["verdicts"]}
    assert got == {k: full[k] for k in got}
    assert len(got) == 6  # components geo and industry are not reported
    assert got[("ctr", "by_device")] is False
    assert result.facts["skipped_metrics"] == []


def test_check_compatibility_unknown_name_is_error(deps: SessionDeps) -> None:
    result = check_compatibility(make_ctx(deps), metrics=["ctr"], slices=["nope"])
    assert result.status == Status.error


# ── validate_spec ──────────────────────────────────────────────────────────


def test_validate_spec_valid(deps: SessionDeps) -> None:
    result = validate_spec(make_ctx(deps), "metric", CPC_YAML)
    assert result.status == Status.ok
    assert result.facts == {"name": "cpc", "issues": [], "warnings": []}
    stored = deps.store.read(result.artifact_id)
    assert stored["referenced_columns"]["numerator"] == ["ad_spend"]


def test_validate_spec_invalid_yaml(deps: SessionDeps) -> None:
    result = validate_spec(make_ctx(deps), "metric", "metric:\n  name: x\n")
    assert result.status == Status.error
    assert result.facts["issues"]


def test_validate_spec_existing_name_warns(deps: SessionDeps) -> None:
    yaml_text = CPC_YAML.replace("name: cpc", "name: ctr")
    result = validate_spec(make_ctx(deps), "metric", yaml_text)
    assert result.status == Status.ok
    assert "replaces the existing metric 'ctr'" in result.facts["warnings"][0]


@pytest.mark.parametrize(
    ("refs", "message"), [("[geo, nope]", "unknown slice"), ("[geo, gxi]", "composite")]
)
def test_validate_spec_composite_references(
    make_deps, example_specs: SpecCache, refs: str, message: str
) -> None:
    example_specs.add(
        SliceSpec.from_yaml("slice:\n  name: gxi\n  cross_product: [geo, industry]\n")
    )
    deps = make_deps(spec_cache=example_specs)
    yaml_text = f"slice:\n  name: combo\n  cross_product: {refs}\n"
    result = validate_spec(make_ctx(deps), "slice", yaml_text)
    assert result.status == Status.error
    assert message in result.facts["issues"][0]["message"]


def test_validate_spec_composite_all_refs_present(deps: SessionDeps) -> None:
    yaml_text = "slice:\n  name: combo\n  cross_product: [geo, industry]\n"
    assert validate_spec(make_ctx(deps), "slice", yaml_text).status == Status.ok


def test_validate_spec_column_missing(deps: SessionDeps) -> None:
    result = validate_spec(make_ctx(deps), "metric", CPC_YAML.replace("ad_spend", "spend"))
    assert result.status == Status.error
    assert "'spend' not found" in result.facts["issues"][0]["message"]


def test_validate_spec_table_not_found_blocks(deps: SessionDeps) -> None:
    yaml_text = CPC_YAML.replace("/ad_campaigns\n", "/no_such_table\n")
    result = validate_spec(make_ctx(deps), "metric", yaml_text)
    assert result.status == Status.error
    assert "not found" in result.facts["issues"][0]["message"]


def test_validate_spec_connection_failure_warns(deps: SessionDeps) -> None:
    yaml_text = CPC_YAML.replace(SOURCE, "bigquery://proj/ds/events")
    result = validate_spec(make_ctx(deps), "metric", yaml_text)
    assert result.status == Status.ok
    assert "Column check skipped" in result.facts["warnings"][0]
    assert "bigquery://proj/ds.events" in deps.store.read(result.artifact_id)["yaml"]


# ── commit_spec, delete_spec ───────────────────────────────────────────────


def test_commit_spec_adds_and_saves(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    draft = validate_spec(ctx, "metric", CPC_YAML)
    result = commit_spec(ctx, draft.artifact_id)
    assert result.facts == {"spec_type": "metric", "name": "cpc", "action": "added"}
    assert "cpc" in deps.spec_cache.metrics
    saved = Path(deps.spec_dir) / "metrics"
    assert (saved / "cpc.yaml").exists()
    assert len(list(saved.glob("*.yaml"))) == 7
    assert SpecCache.from_yaml(metric_paths=str(saved)).get_metric("cpc").denominator


def test_commit_spec_updates(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    yaml_text = CPC_YAML.replace("name: cpc", "name: ctr")
    result = commit_spec(ctx, validate_spec(ctx, "metric", yaml_text).artifact_id)
    assert result.facts["action"] == "updated"
    assert deps.spec_cache.get_metric("ctr").numerator == "SUM(ad_spend)"


def test_commit_spec_to_memory_url(make_deps, example_specs: SpecCache) -> None:
    deps = make_deps(spec_cache=example_specs)
    deps.spec_dir = "memory://agent-specs"
    ctx = make_ctx(deps)
    commit_spec(ctx, validate_spec(ctx, "slice", COUNTRY_YAML).artifact_id)
    assert fsspec.filesystem("memory").exists("/agent-specs/slices/country.yaml")
    fsspec.filesystem("memory").rm("/agent-specs", recursive=True)


def test_commit_spec_invalid_draft_leaves_cache_unchanged(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    draft = validate_spec(ctx, "metric", CPC_YAML.replace("ad_spend", "spend"))
    result = commit_spec(ctx, draft.artifact_id)
    assert result.status == Status.error
    assert "cpc" not in deps.spec_cache.metrics
    assert not (Path(deps.spec_dir) / "metrics").exists()


def test_commit_spec_rejects_other_artifacts(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    other = list_specs(ctx)
    assert commit_spec(ctx, other.artifact_id).status == Status.error
    assert commit_spec(ctx, "a999").status == Status.error


def test_commit_spec_rolls_back_on_save_failure(deps: SessionDeps, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    deps.spec_dir = blocker
    ctx = make_ctx(deps)
    result = commit_spec(ctx, validate_spec(ctx, "metric", CPC_YAML).artifact_id)
    assert result.status == Status.error
    assert "cpc" not in deps.spec_cache.metrics


def test_delete_spec_removes_spec_and_file(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    commit_spec(ctx, validate_spec(ctx, "slice", COUNTRY_YAML).artifact_id)
    slices = Path(deps.spec_dir) / "slices"
    assert (slices / "country.yaml").exists()

    result = delete_spec(ctx, "slice", "country")

    assert result.status == Status.ok
    assert "country" not in deps.spec_cache.slices
    assert not (slices / "country.yaml").exists()


def test_delete_spec_unknown_or_referenced_is_error(make_deps, example_specs: SpecCache) -> None:
    example_specs.add(
        SliceSpec.from_yaml("slice:\n  name: gxi\n  cross_product: [geo, industry]\n")
    )
    deps = make_deps(spec_cache=example_specs)
    ctx = make_ctx(deps)
    assert delete_spec(ctx, "metric", "nope").status == Status.error
    assert delete_spec(ctx, "slice", "geo").status == Status.error
    assert "geo" in deps.spec_cache.slices


# ── compute_metrics ────────────────────────────────────────────────────────


def test_compute_metrics_stores_lazy_result(deps: SessionDeps) -> None:
    params = MetricComputeParams(metrics=["ctr", "roas"], slices=["geo"])
    result = compute_metrics(make_ctx(deps), params)
    ref = deps.store.get(result.artifact_id)
    assert (ref.kind, ref.type_tag) == ("ibis", "metric_result")
    assert result.summary == "ctr, roas by geo, all_time"
    direct = MetricCompute(deps.spec_cache, deps.connection_manager).compute(
        metrics=["ctr", "roas"], slices=["geo"]
    )
    key = ["metric_name", "slice_value"]
    got = deps.store.read(result.artifact_id).to_pyarrow().sort_by([(k, "ascending") for k in key])
    assert got.equals(direct.to_pyarrow().sort_by([(k, "ascending") for k in key]))


def test_compute_metrics_error(deps: SessionDeps) -> None:
    params = MetricComputeParams(metrics=["ctr"], period_type="monthly")
    result = compute_metrics(make_ctx(deps), params)
    assert result.status == Status.error
