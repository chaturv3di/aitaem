# API Reference

Core classes, types, and exceptions are importable directly from `aitaem`:

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute
from aitaem import PeriodType, VALID_PERIOD_TYPES
from aitaem import AitaemError, SpecNotFoundError, QueryBuildError  # etc.
```

Helpers are importable from `aitaem.helpers`:

```python
from aitaem.helpers import load_csvs_to_duckdb
```

## Class Overview

| Class | Module | Purpose |
|-------|--------|---------|
| [`MetricCompute`](insights.md) | `aitaem.insights` | Primary interface — compute metrics |
| [`SpecCache`](specs.md) | `aitaem.specs.loader` | Load and cache YAML specs |
| [`MetricSpec`](specs.md#aitaem.specs.metric.MetricSpec) | `aitaem.specs.metric` | Metric spec dataclass |
| [`SliceSpec`](specs.md#aitaem.specs.slice.SliceSpec) | `aitaem.specs.slice` | Slice spec dataclass |
| [`SegmentSpec`](specs.md#aitaem.specs.segment.SegmentSpec) | `aitaem.specs.segment` | Segment spec dataclass |
| [`ConnectionManager`](connectors.md) | `aitaem.connectors.connection` | Manage backend connections (DuckDB, BigQuery, PostgreSQL) |

## Constants and Types

| Symbol | Type | Purpose |
|--------|------|---------|
| `VALID_PERIOD_TYPES` | `frozenset[str]` | Set of valid `period_type` values |
| `PeriodType` | `Literal[...]` | Type alias for `period_type` — use in annotations and Pydantic models |

```python
from aitaem import PeriodType, VALID_PERIOD_TYPES
# PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly"]
# VALID_PERIOD_TYPES = frozenset({"all_time", "daily", "weekly", "monthly", "yearly"})
```

## Exceptions

All exceptions inherit from `AitaemError` and are importable from `aitaem`.

| Exception | Raised when |
|-----------|-------------|
| `AitaemError` | Base class for all aitaem errors |
| `AitaemConnectionError` | Backend connection fails |
| `ConnectionNotFoundError` | Requested backend is not configured |
| `TableNotFoundError` | Table does not exist in the backend |
| `ConfigurationError` | Configuration is invalid or incomplete |
| `InvalidURIError` | Source URI is malformed |
| `UnsupportedBackendError` | Backend type is not supported |
| `QueryBuildError` | Query construction fails (invalid specs, bad period_type, etc.) |
| `QueryExecutionError` | Query execution fails |
| `SpecValidationError` | A YAML spec fails validation or a duplicate spec name is loaded |
| `SpecNotFoundError` | A named spec is not in the cache |

## Helpers Overview

| Function | Module | Purpose |
|----------|--------|---------|
| [`load_csvs_to_duckdb`](helpers.md) | `aitaem.helpers` | Load CSV file(s) into a DuckDB database |
