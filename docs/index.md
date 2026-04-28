# AITÆM: All Interesting Things Are Essentially Metrics

[![CI](https://github.com/chaturv3di/aitaem/actions/workflows/ci.yml/badge.svg)](https://github.com/chaturv3di/aitaem/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/aitaem)](https://pypi.org/project/aitaem/)
[![Python versions](https://img.shields.io/pypi/pyversions/aitaem)](https://pypi.org/project/aitaem/)
[![Docs](https://img.shields.io/badge/docs-github.io-blue)](https://chaturv3di.github.io/aitaem)

**aitaem** (pronounced *i-tame*) is a Python library for generating data insights from OLAP databases or local CSV files. It provides a declarative API for defining and computing metrics, slices, segments, and time series, and is designed to be LLM-friendly.

## Why?

Business leaders, PMs, EMs, and individual contributors constantly need deep understanding of their businesses and products. The most common way to get "data insights" is to rely on a data scientist or analyst to dive into the data. Why is this a problem?

1. **Practically** — Dashboards are limited. There is always a new question that existing dashboards cannot answer.
2. **Operationally** — It is wasteful for a DS or BA to dive into source tables and rewrite SQL queries to compute the same customized metrics repeatedly.
3. **Scientifically** — Ad-hoc analysis accuracy depends on the individual. The same analysis done by different people can yield different results.
4. **Organisationally** — Inter-org trust should be built on *processes and tooling*, not on individuals.

## What?

The library has two core components:

1. **Specifications** — A simple declarative YAML structure to define metric specs, slice/breakdown specs, and segment specs.
2. **Computation** — A small collection of Python classes with compact signatures that compute the metrics.

Additionally, there are utilities to connect to various data backends simultaneously. Supported backends: **DuckDB** (built-in), **BigQuery**, and **PostgreSQL** (optional extras).

## Three-Line Quick Start

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute

cache = SpecCache.from_yaml(metric_paths="examples/metrics/",
                            slice_paths="examples/slices/",
                            segment_paths="examples/segments/")
conn = ConnectionManager.from_yaml("examples/connections.yaml")
df = MetricCompute(cache, conn).compute(metrics="ctr", slices="campaign_type")
```

[:fontawesome-solid-rocket: Get Started](getting-started.md){ .md-button .md-button--primary }
[:fontawesome-brands-github: View on GitHub](https://github.com/chaturv3di/aitaem){ .md-button }
