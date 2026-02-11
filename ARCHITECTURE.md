# aitaem Library Architecture Design

## Executive Summary

This document defines the architecture for **aitaem** (All Interesting Things Are Essentially Metrics), a Python library for generating data insights from OLAP databases and CSV/Parquet files through declarative YAML specifications.

**Key Design Principles**:
- **Import depth ≤ 2**: All functionality accessible via `from aitaem import X` or `from aitaem.module import X`
- **LLM-friendly**: Standardized output format, familiar APIs, leveraging widely-known technologies
- **Lazy evaluation**: Specs loaded on-demand, queries built but not executed until needed
- **SQL-native**: Use DuckDB/SQL syntax directly in YAML specs (no custom DSL)
- **Loosely coupled**: Slices and segments are independent, reusable specifications

---

## Technology Choices

### 1. Query Abstraction Layer: **Ibis with DuckDB Backend**

**Decision**: Use Ibis as the primary query abstraction layer, with DuckDB as the default backend.

**Rationale**:
- **Backend portability**: Write once, run on ClickHouse, Druid, BigQuery, etc.
- **Mature OLAP support**: Native ClickHouse backend (since 2017) and Druid backend (2023+)
- **DuckDB federation**: Can leverage DuckDB's ATTACH for federated queries to PostgreSQL, MySQL
- **Negligible overhead**: <5% performance overhead; query execution in native engines
- **Future-proof**: Add new backends without refactoring query logic
- **LLM-friendly**: Pythonic API easier for LLMs than SQL string generation

**Implementation**:
- DuckDB as primary backend for CSV/Parquet and local analytics
- ClickHouse/Druid connectors for OLAP databases
- DuckDB federation for operational databases (Postgres, MySQL) via ATTACH

### 2. Output Format: **pandas (default) with polars support**

**Decision**: pandas as default DataFrame output, with polars as first-class alternative.

**Rationale**:
- **LLM-friendly (critical)**: pandas has abundant training data; LLMs struggle with polars syntax
- **Ubiquity**: Universal adoption, gentler learning curve for analysts
- **Stability**: Minimal breaking changes vs polars' evolving API
- **Ibis alignment**: pandas is Ibis's default output format
- **Polars option**: Easy to support via `output_format='polars'` for performance-sensitive users

**Implementation**:
- Default: `compute()` returns pandas DataFrame
- Optional: `compute(..., output_format='polars')` returns polars DataFrame
- Both leverage zero-copy Arrow conversion from DuckDB/Ibis

---

## Standard Output Format

All `compute()` calls return a single DataFrame in this standardized long format:

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| `period_type` | str | Time granularity | 'daily', 'weekly', 'monthly', 'all_time' |
| `period_start_date` | date/str | Start of period | '2026-02-01' |
| `period_end_date` | date/str | End of period | '2026-02-08' |
| `metric_name` | str | Name of the metric | 'click_rate', 'revenue' |
| `slice_type` | str | Dimension(s) sliced (pipe-delimited) | 'geo', 'geo\|device', 'none' |
| `slice_value` | str | Value(s) of slice (pipe-delimited) | 'US', 'US\|mobile', 'all' |
| `segment_name` | str | Segment applied | 'premium_users', 'all_users' |
| `metric_value` | float | Computed metric value | 0.045, 12500.0 |

**Benefits**:
- **LLM integration**: Consistent format simplifies prompt engineering
- **Visualization**: Easy to pivot, filter, and plot
- **Combining analyses**: Stack results from multiple queries
- **Narrow & deep**: Scalable format for large result sets

**Example Row**:
```python
['weekly', '2026-02-01', '2026-02-08', 'click_rate', 'geo|device', 'US|mobile', 'premium_users', 0.045]
```

---

## Module Architecture

```
aitaem/
├── __init__.py              # Top-level imports (depth-1 access)
├── insights.py              # PRIMARY USER INTERFACE
├── connection.py            # ConnectionManager for multiple backends
├── specs/                   # YAML specification parsing
│   ├── __init__.py
│   ├── metric.py           # MetricSpec class
│   ├── slice.py            # SliceSpec class
│   ├── segment.py          # SegmentSpec class
│   └── loader.py           # Spec loading from files/strings
├── query/                   # Query building and execution
│   ├── __init__.py
│   ├── builder.py          # Convert specs → Ibis expressions
│   ├── optimizer.py        # Group metrics by table for efficiency
│   └── executor.py         # Execute queries with lazy evaluation
├── connectors/              # Backend connections
│   ├── __init__.py
│   ├── base.py             # Abstract Connector interface
│   └── ibis_connector.py   # Ibis-based multi-backend connector
└── utils/                   # Utilities
    ├── __init__.py
    ├── validation.py        # YAML validation with clear errors
    ├── exceptions.py        # Custom exception classes
    └── formatting.py        # DataFrame formatting/conversion
```

### Import Patterns (Depth ≤ 2)

```python
# Depth 1 - Primary interface
from aitaem import compute, connect

# Depth 2 - Specific functionality
from aitaem.insights import MetricCompute
from aitaem.specs import MetricSpec, SliceSpec, SegmentSpec
from aitaem.connectors import IbisConnector
```

---

## Module Details

### 1. `insights.py` - Primary User Interface

**Purpose**: Main entry point for users to compute metrics.

**Key Classes**:

#### `MetricCompute`
```python
class MetricCompute:
    def __init__(self, metric_paths=None, slice_paths=None, segment_paths=None):
        """
        Initialize with paths for lazy loading.
        Specs are loaded on-demand during compute().

        Args:
            metric_paths: str or list[str] - paths to metric YAML files/directories
            slice_paths: str or list[str] - paths to slice YAML files/directories
            segment_paths: str or list[str] - paths to segment YAML files/directories
        """

    @classmethod
    def from_yaml(cls, metric_paths, slice_paths=None, segment_paths=None):
        """Create instance from YAML file paths."""

    @classmethod
    def from_string(cls, metric_yaml, slice_yaml=None, segment_yaml=None):
        """Create instance from YAML strings."""

    def compute(self, metrics, slices=None, segments=None,
                time_window=None, filters=None, output_format='pandas'):
        """
        Compute one or more metrics with optional slicing and segmentation.

        Args:
            metrics: str or list[str] - metric name(s) to compute
            slices: str or list[str] or None - slice name(s) to apply
            segments: str or list[str] or None - segment name(s) to apply
            time_window: tuple or None - (start_date, end_date) for period filter
            filters: dict or None - additional filters to apply
            output_format: 'pandas' or 'polars' - output DataFrame type

        Returns:
            DataFrame in standard format (see Standard Output Format section)
        """
```

**Top-level convenience functions**:
```python
def compute(metrics, slices=None, segments=None, **kwargs):
    """One-shot computation from spec names (requires prior setup)."""

def connect(connection_string, backend='duckdb', **kwargs):
    """Establish global backend connection."""
```

**Design Decisions**:
- **Single `compute()` method**: Accepts both single string and list of metric names
- **Lazy spec loading**: Specs loaded on-demand, cached for session
- **Standard output**: Always returns single DataFrame in standardized format
- **No metadata in output**: Keep DataFrame simple (metadata support in Phase 2)

**Usage Example**:
```python
from aitaem import connect
from aitaem.insights import MetricCompute

# Establish connection
connect('duckdb://analytics.db')

# Load specs (lazy - files read during compute)
mc = MetricCompute.from_yaml(
    metric_paths='metrics/',
    slice_paths='slices/',
    segment_paths='segments/'
)

# Compute single metric
df = mc.compute('revenue', slices='country')

# Compute multiple metrics (same slicing)
df = mc.compute(
    metrics=['revenue', 'orders', 'conversion_rate'],
    slices=['country', 'device'],  # Cross-product: country|device
    segments='premium_users',
    time_window=('2026-01-01', '2026-02-01'),
    output_format='pandas'
)

# Result is single DataFrame:
#   period_type | period_start_date | period_end_date | metric_name | slice_type | slice_value | segment_name | metric_value
#   all_time    | 2026-01-01        | 2026-02-01      | revenue     | country|device | US|mobile | premium_users | 125000.50
#   all_time    | 2026-01-01        | 2026-02-01      | revenue     | country|device | US|desktop | premium_users | 87500.25
#   ...
```

---

### 2. `connection.py` - Connection Manager

**Purpose**: Manage multiple backend connections throughout a session.

**Key Classes**:

#### `ConnectionManager`
```python
class ConnectionManager:
    """
    Singleton managing backend connections.
    Supports multiple simultaneous connections (e.g., DuckDB + ClickHouse).
    """

    def add_connection(self, name, connection_string, backend_type='auto', **kwargs):
        """
        Add a new backend connection.

        Args:
            name: str - identifier for this connection
            connection_string: str - connection URI (e.g., 'duckdb://db.db', 'clickhouse://host:port')
            backend_type: str - 'duckdb', 'clickhouse', 'druid', or 'auto' (infer from URI)
        """

    def get_connection(self, name_or_uri):
        """
        Retrieve connection by name or URI.
        If URI not registered, create on-demand.
        """

    def get_connection_for_source(self, source_uri):
        """
        Get appropriate connection for a metric's source table.
        Automatically creates connection if needed.
        """
```

**Design Decisions**:
- **Global singleton**: One manager per session
- **Lazy connection creation**: Connections established on first use
- **Multi-backend support**: Can query DuckDB + ClickHouse simultaneously
- **Auto-detection**: Infer backend type from URI scheme

**Usage Example**:
```python
from aitaem import connect

# Primary connection (global default)
connect('duckdb://local.db')

# Additional connections registered as needed
# (Auto-created when metrics reference new sources)
# e.g., metric with source='clickhouse://prod.example.com/events'
```

---

### 3. `specs/` - YAML Specification Parsing

**Purpose**: Parse and validate YAML specifications for metrics, slices, and segments.

#### 3.1 `specs/metric.py` - MetricSpec

**YAML Schema**:
```yaml
metric:
  name: homepage_click_rate
  description: Click-through rate for homepage impressions
  source: duckdb://analytics.db/events  # Connection URI + table
  aggregation: ratio  # sum, avg, count, ratio, etc.
  numerator: "SUM(CASE WHEN event_type = 'click' AND page = 'home_page' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' AND page = 'home_page' THEN 1 ELSE 0 END)"
```

For simple aggregations:
```yaml
metric:
  name: total_revenue
  description: Sum of all transaction amounts
  source: duckdb://analytics.db/transactions
  aggregation: sum
  numerator: "SUM(amount)"
  # denominator omitted for sum/count aggregations
```

**Class Definition**:
```python
class MetricSpec:
    """
    Parsed and validated metric specification.
    """
    name: str
    description: str
    source: str  # URI: backend://path/table
    aggregation: str  # sum, avg, count, ratio, min, max
    numerator: str  # SQL expression
    denominator: str | None  # SQL expression (None for sum/count)

    @classmethod
    def from_yaml(cls, yaml_string_or_path):
        """Load and validate metric spec from YAML."""

    def validate(self):
        """Validate spec fields and SQL expressions."""

    def to_ibis_expression(self, table):
        """Convert to Ibis aggregation expression."""
```

**Design Decisions**:
- **SQL expressions**: Use DuckDB SQL syntax directly (CASE, SUM, COUNT, etc.)
- **numerator/denominator**: Explicit split for ratios; denominator optional for simple aggregations
- **Validation**: Check SQL syntax, required fields, source URI format
- **No joins in Phase 1**: Single source table per metric

#### 3.2 `specs/slice.py` - SliceSpec

**YAML Schema**:
```yaml
slice:
  name: country
  description: Geographic breakdown by country groups
  values:
    - name: US
      where: "country_code = 'US'"
    - name: EU
      where: "country_code IN ('DE', 'ES', 'FR', 'IT')"
    - name: ROW
      where: "country_code NOT IN ('US', 'DE', 'ES', 'FR', 'IT')"
```

**Class Definition**:
```python
class SliceSpec:
    """
    Parsed and validated slice specification.
    Defines how to break down a metric by dimension values.
    """
    name: str
    description: str
    values: list[SliceValue]  # Each with name and WHERE condition

    @dataclass
    class SliceValue:
        name: str
        where: str  # SQL WHERE condition

    @classmethod
    def from_yaml(cls, yaml_string_or_path):
        """Load and validate slice spec from YAML."""

    def validate(self):
        """Validate spec fields and SQL WHERE conditions."""

    def to_ibis_filters(self, table):
        """Convert each slice value to Ibis filter expression."""
```

**Design Decisions**:
- **Reusable**: Same slice can apply to any metric with compatible columns
- **SQL WHERE syntax**: Direct SQL conditions (e.g., `country_code IN (...)`)
- **Named values**: Each slice value (US, EU, ROW) has explicit name and filter
- **Validation**: Check SQL syntax for WHERE conditions

#### 3.3 `specs/segment.py` - SegmentSpec

**YAML Schema**:
```yaml
segment:
  name: premium_users
  description: Users on premium or enterprise subscription plans
  where: "subscription_tier IN ('premium', 'enterprise') AND status = 'active'"
```

**Class Definition**:
```python
class SegmentSpec:
    """
    Parsed and validated segment specification.
    Defines cohort filter applied to all metrics.
    """
    name: str
    description: str
    where: str  # SQL WHERE condition

    @classmethod
    def from_yaml(cls, yaml_string_or_path):
        """Load and validate segment spec from YAML."""

    def validate(self):
        """Validate spec fields and SQL WHERE condition."""

    def to_ibis_filter(self, table):
        """Convert to Ibis filter expression."""
```

**Design Decisions**:
- **Simple WHERE for Phase 1**: Basic cohort filtering
- **Phase 2 extensions**: HAVING clauses, subqueries for complex segments
- **Reusable**: Same segment applies to any metric with compatible columns

#### 3.4 `specs/loader.py` - Spec Loading

**Functions**:
```python
def load_spec_from_file(path: str, spec_type: type) -> MetricSpec | SliceSpec | SegmentSpec:
    """Load single spec from YAML file."""

def load_spec_from_string(yaml_string: str, spec_type: type) -> MetricSpec | SliceSpec | SegmentSpec:
    """Load single spec from YAML string."""

def load_specs_from_directory(directory: str, spec_type: type) -> dict[str, Spec]:
    """Load all specs of given type from directory (lazy iterator)."""

class SpecCache:
    """
    Cache for loaded specs.
    Lazy loading: specs loaded on first access, cached for session.
    """
    def get_metric(self, name: str) -> MetricSpec
    def get_slice(self, name: str) -> SliceSpec
    def get_segment(self, name: str) -> SegmentSpec
```

**Design Decisions**:
- **Lazy loading**: Specs loaded on first access during `compute()`
- **Caching**: Loaded specs cached for session to avoid re-parsing
- **Clear errors**: `SpecNotFoundError` if spec not found in configured paths
- **Phase 2 DB support**: Future extension to load specs from database

---

### 4. `query/` - Query Building and Execution

#### 4.1 `query/builder.py` - QueryBuilder

**Purpose**: Convert metric/slice/segment specs into Ibis expressions.

**Key Class**:
```python
class QueryBuilder:
    """
    Builds Ibis query expressions from specs.
    """

    def build_metric_query(self, metric_spec: MetricSpec,
                          slice_specs: list[SliceSpec] | None,
                          segment_spec: SegmentSpec | None,
                          time_window: tuple | None) -> ibis.Expr:
        """
        Build complete Ibis expression for metric computation.

        Steps:
        1. Get table from metric source
        2. Apply segment filter (if provided)
        3. Apply time window filter (if provided)
        4. Apply slices (generate rows for each slice value combination)
        5. Compute metric aggregation (numerator/denominator)
        6. Return expression in standard output format
        """

    def build_slice_expression(self, table, slice_specs: list[SliceSpec]) -> ibis.Expr:
        """
        Build slice cross-product (e.g., country x device).
        Returns expression with slice_type and slice_value columns.
        """

    def parse_sql_expression(self, sql_expr: str, table) -> ibis.Expr:
        """
        Parse SQL expression (from numerator/denominator) into Ibis expression.
        Uses Ibis's SQL string parsing or direct expression API.
        """
```

**Design Decisions**:
- **Ibis expressions**: All queries built as Ibis expressions (backend-agnostic)
- **SQL expression parsing**: Leverage Ibis's ability to parse SQL strings
- **Standard output generation**: Builder adds standard columns (period_type, metric_name, etc.)
- **Cross-product slicing**: Multiple slices create hierarchical combinations

#### 4.2 `query/optimizer.py` - QueryOptimizer

**Purpose**: Optimize multi-metric queries by grouping by source table.

**Key Class**:
```python
class QueryOptimizer:
    """
    Optimizes batch metric computation.
    Groups metrics by source table for efficient execution.
    """

    def optimize(self, metric_specs: list[MetricSpec],
                 slice_specs: list[SliceSpec] | None,
                 segment_spec: SegmentSpec | None) -> list[QueryGroup]:
        """
        Group metrics by source table.
        Metrics from same table computed in single query.
        Metrics from different tables executed in parallel.

        Returns:
            List of QueryGroups, each containing metrics from same source
        """

    @dataclass
    class QueryGroup:
        source: str  # Table URI
        metrics: list[MetricSpec]
        query_expr: ibis.Expr  # Combined query for all metrics in group
```

**Design Decisions**:
- **Group by source table**: Metrics from same source table combined into single query
- **Parallel execution**: Different source tables queried in parallel
- **Shared filters**: Slices and segments applied once per table

#### 4.3 `query/executor.py` - QueryExecutor

**Purpose**: Execute Ibis queries and format results.

**Key Class**:
```python
class QueryExecutor:
    """
    Executes Ibis queries and formats results in standard output format.
    """

    def execute(self, query_groups: list[QueryGroup],
                output_format: str = 'pandas') -> DataFrame:
        """
        Execute query groups (in parallel if multiple sources).
        Combine results into single DataFrame in standard format.

        Args:
            query_groups: List of optimized query groups
            output_format: 'pandas' or 'polars'

        Returns:
            Single DataFrame with all results in standard format
        """

    def execute_single_query(self, query_expr: ibis.Expr) -> DataFrame:
        """Execute single Ibis expression."""

    def format_results(self, results: list[DataFrame]) -> DataFrame:
        """Combine and format multiple query results into standard output."""
```

**Design Decisions**:
- **Lazy execution**: Queries built but not executed until `execute()` called
- **Parallel execution**: Multiple source tables queried concurrently
- **Format standardization**: All results formatted into standard output schema
- **Output conversion**: Convert Ibis results to pandas/polars as requested

---

### 5. `connectors/` - Backend Connections

#### 5.1 `connectors/base.py` - Abstract Connector

**Purpose**: Define interface for backend connectors.

**Key Class**:
```python
class Connector(ABC):
    """
    Abstract base class for backend connectors.
    """

    @abstractmethod
    def connect(self, connection_string: str, **kwargs):
        """Establish connection to backend."""

    @abstractmethod
    def get_table(self, table_name: str) -> ibis.Table:
        """Get Ibis table reference."""

    @abstractmethod
    def execute(self, expr: ibis.Expr, output_format: str) -> DataFrame:
        """Execute Ibis expression and return DataFrame."""

    @abstractmethod
    def close(self):
        """Close connection."""
```

#### 5.2 `connectors/ibis_connector.py` - Ibis Connector

**Purpose**: Ibis-based multi-backend connector implementation.

**Key Class**:
```python
class IbisConnector(Connector):
    """
    Ibis-based connector supporting multiple backends.
    """

    def __init__(self, backend_type: str = 'duckdb'):
        """
        Initialize connector for specific backend.

        Args:
            backend_type: 'duckdb', 'clickhouse', 'druid', etc.
        """
        self.backend_type = backend_type
        self.connection = None

    def connect(self, connection_string: str, **kwargs):
        """
        Establish Ibis connection.

        Examples:
            - DuckDB: 'duckdb://analytics.db'
            - ClickHouse: 'clickhouse://host:port/database'
            - Druid: 'druid://host:port/druid/v2/sql'
        """
        # Parse connection string
        # Create Ibis backend connection
        # Store connection

    def get_table(self, table_name: str) -> ibis.Table:
        """Get Ibis table reference from backend."""
        return self.connection.table(table_name)

    def execute(self, expr: ibis.Expr, output_format: str) -> DataFrame:
        """
        Execute Ibis expression.

        Returns:
            pandas or polars DataFrame based on output_format
        """
        if output_format == 'pandas':
            return expr.to_pandas()
        elif output_format == 'polars':
            return expr.to_polars()

    def attach_database(self, name: str, connection_string: str):
        """
        Attach external database (DuckDB federation).
        Only supported on DuckDB backend.
        """
        if self.backend_type == 'duckdb':
            self.connection.raw_sql(f"ATTACH '{connection_string}' AS {name}")
```

**Design Decisions**:
- **Ibis abstraction**: Single implementation supports all Ibis backends
- **Backend auto-detection**: Infer backend from connection string URI scheme
- **DuckDB federation**: Support ATTACH for multi-database queries
- **Zero-copy conversions**: Leverage Arrow for efficient pandas/polars conversion

---

### 6. `utils/` - Utilities

#### 6.1 `utils/validation.py` - YAML Validation

**Purpose**: Validate YAML specs with clear, helpful error messages.

**Key Functions**:
```python
def validate_metric_spec(spec_dict: dict) -> ValidationResult:
    """
    Validate metric YAML structure and content.

    Checks:
    - Required fields present (name, source, aggregation, numerator)
    - Valid aggregation type
    - SQL syntax in numerator/denominator
    - Source URI format
    """

def validate_slice_spec(spec_dict: dict) -> ValidationResult:
    """
    Validate slice YAML structure and content.

    Checks:
    - Required fields present (name, values)
    - Each value has name and where clause
    - SQL syntax in WHERE conditions
    """

def validate_segment_spec(spec_dict: dict) -> ValidationResult:
    """
    Validate segment YAML structure and content.

    Checks:
    - Required fields present (name, where)
    - SQL syntax in WHERE condition
    """

@dataclass
class ValidationResult:
    """Result of validation with errors and suggestions."""
    valid: bool
    errors: list[ValidationError]

@dataclass
class ValidationError:
    """Single validation error with context."""
    field: str
    message: str
    line_number: int | None
    suggestion: str | None
```

**Design Decisions**:
- **Fail fast**: Validation happens at spec loading time
- **Clear errors**: Include field name, line number, error message, suggestions
- **SQL validation**: Check SQL syntax using DuckDB parser
- **Helpful suggestions**: E.g., "Did you mean 'aggregation: sum' instead of 'aggregate: sum'?"

#### 6.2 `utils/exceptions.py` - Custom Exceptions

**Purpose**: Define clear exception hierarchy for library errors.

**Key Classes**:
```python
class AitaemError(Exception):
    """Base exception for all aitaem errors."""

class SpecValidationError(AitaemError):
    """Raised when YAML spec validation fails."""
    def __init__(self, spec_type: str, errors: list[ValidationError]):
        self.spec_type = spec_type
        self.errors = errors

class SpecNotFoundError(AitaemError):
    """Raised when spec cannot be found in configured paths."""
    def __init__(self, spec_type: str, name: str, searched_paths: list[str]):
        self.spec_type = spec_type
        self.name = name
        self.searched_paths = searched_paths

class ConnectionError(AitaemError):
    """Raised when backend connection fails."""

class QueryExecutionError(AitaemError):
    """Raised when query execution fails."""
```

**Design Decisions**:
- **Clear hierarchy**: All exceptions inherit from `AitaemError`
- **Rich context**: Exceptions include relevant details for debugging
- **User-friendly messages**: Error messages guide users to solutions

#### 6.3 `utils/formatting.py` - DataFrame Formatting

**Purpose**: Format query results into standard output schema.

**Key Functions**:
```python
def format_to_standard_output(results: list[DataFrame],
                              metric_names: list[str],
                              slice_specs: list[SliceSpec] | None,
                              segment_name: str | None,
                              time_window: tuple | None) -> DataFrame:
    """
    Format query results into standard output schema.

    Adds columns:
    - period_type, period_start_date, period_end_date
    - metric_name, slice_type, slice_value
    - segment_name, metric_value
    """

def convert_output_format(df: DataFrame, target_format: str) -> DataFrame:
    """
    Convert DataFrame between pandas and polars.
    Uses zero-copy Arrow conversion when possible.
    """
```

---

## YAML Specification Examples

### Complete Metric Example (Ratio)

```yaml
metric:
  name: homepage_ctr
  description: Click-through rate for homepage impressions
  source: duckdb://analytics.db/events
  aggregation: ratio
  numerator: "SUM(CASE WHEN event_type = 'click' AND page = 'home_page' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' AND page = 'home_page' THEN 1 ELSE 0 END)"
```

### Complete Metric Example (Sum)

```yaml
metric:
  name: total_revenue
  description: Sum of all transaction amounts
  source: duckdb://analytics.db/transactions
  aggregation: sum
  numerator: "SUM(amount)"
```

### Complete Metric Example (Average)

```yaml
metric:
  name: avg_order_value
  description: Average transaction amount per order
  source: clickhouse://prod.example.com/orders
  aggregation: avg
  numerator: "SUM(total_amount)"
  denominator: "COUNT(*)"
```

### Complete Slice Example

```yaml
slice:
  name: geography
  description: Geographic breakdown by major regions
  values:
    - name: North America
      where: "country_code IN ('US', 'CA', 'MX')"
    - name: Europe
      where: "country_code IN ('DE', 'FR', 'UK', 'ES', 'IT')"
    - name: Asia Pacific
      where: "country_code IN ('CN', 'JP', 'IN', 'AU', 'SG')"
    - name: Rest of World
      where: "country_code NOT IN ('US', 'CA', 'MX', 'DE', 'FR', 'UK', 'ES', 'IT', 'CN', 'JP', 'IN', 'AU', 'SG')"
```

### Complete Segment Example

```yaml
segment:
  name: high_value_customers
  description: Customers with lifetime value > $1000 and active status
  where: "lifetime_value > 1000 AND customer_status = 'active'"
```

---

## Usage Workflow

### 1. Setup Connection

```python
from aitaem import connect

# Establish default backend connection
connect('duckdb://analytics.db')
```

### 2. Load Specs

```python
from aitaem.insights import MetricCompute

# Load specs from YAML files (lazy loading)
mc = MetricCompute.from_yaml(
    metric_paths='config/metrics/',
    slice_paths='config/slices/',
    segment_paths='config/segments/'
)
```

### 3. Compute Metrics

```python
# Compute single metric
df = mc.compute('revenue')

# Compute with slice
df = mc.compute('revenue', slices='geography')

# Compute multiple metrics with multiple slices and segment
df = mc.compute(
    metrics=['revenue', 'orders', 'avg_order_value'],
    slices=['geography', 'device_type'],  # Cross-product
    segments='high_value_customers',
    time_window=('2026-01-01', '2026-02-01'),
    output_format='pandas'
)
```

### 4. Work with Results

```python
# Standard format DataFrame
print(df.head())
#   period_type | period_start_date | period_end_date | metric_name | slice_type           | slice_value        | segment_name         | metric_value
#   all_time    | 2026-01-01        | 2026-02-01      | revenue     | geography|device_type | North America|mobile | high_value_customers | 245000.75
#   all_time    | 2026-01-01        | 2026-02-01      | revenue     | geography|device_type | North America|desktop | high_value_customers | 187500.50
#   ...

# Filter to specific metric
revenue_df = df[df['metric_name'] == 'revenue']

# Pivot for visualization
pivot_df = df.pivot_table(
    index='slice_value',
    columns='metric_name',
    values='metric_value'
)

# Convert to polars for performance
import polars as pl
polars_df = pl.from_pandas(df)
```

---

## Implementation Phases

### Phase 1: Core Functionality (Current Design)

**Scope**:
- Single source table per metric
- Simple aggregations (sum, avg, count, ratio)
- Slices with SQL WHERE conditions
- Segments with SQL WHERE conditions
- DuckDB and ClickHouse backends via Ibis
- pandas and polars output
- File-based YAML spec loading
- Standard output format

**Deliverables**:
- All modules defined above
- Comprehensive unit tests
- Example YAML specs
- Basic documentation

### Phase 2: Advanced Features (Future)

**Potential additions**:
- Multi-table metrics (joins)
- Metric dependencies (derived metrics referencing other metrics)
- Complex segments (HAVING clauses, subqueries)
- Database-backed spec storage
- Result caching
- Time-series-specific optimizations
- Additional backends (Druid, BigQuery, Snowflake)
- Metadata in output (query timing, SQL generated)
- Async API for non-blocking execution

---

## Critical Files to Create

Based on this architecture, here are the critical files needed for implementation:

### Package Structure
```
aitaem/
├── __init__.py                          # Top-level imports, connect() function
├── insights.py                          # MetricCompute class, compute() function
├── connection.py                        # ConnectionManager singleton
├── specs/
│   ├── __init__.py                      # Export MetricSpec, SliceSpec, SegmentSpec
│   ├── metric.py                        # MetricSpec class
│   ├── slice.py                         # SliceSpec class
│   ├── segment.py                       # SegmentSpec class
│   └── loader.py                        # SpecCache, loading functions
├── query/
│   ├── __init__.py                      # Export QueryBuilder, QueryOptimizer, QueryExecutor
│   ├── builder.py                       # QueryBuilder class
│   ├── optimizer.py                     # QueryOptimizer class
│   └── executor.py                      # QueryExecutor class
├── connectors/
│   ├── __init__.py                      # Export Connector, IbisConnector
│   ├── base.py                          # Connector abstract base class
│   └── ibis_connector.py                # IbisConnector implementation
└── utils/
    ├── __init__.py                      # Export validation, exceptions, formatting
    ├── validation.py                    # Validation functions and classes
    ├── exceptions.py                    # Custom exception classes
    └── formatting.py                    # DataFrame formatting functions
```

### Configuration and Documentation
```
├── pyproject.toml                       # Package configuration with dependencies
├── README.md                            # User-facing documentation (update)
├── CLAUDE.md                            # Development guidance (update)
├── examples/
│   ├── metrics/                         # Example metric YAML files
│   ├── slices/                          # Example slice YAML files
│   ├── segments/                        # Example segment YAML files
│   └── quickstart.ipynb                 # Jupyter notebook with examples
└── tests/
    ├── test_specs/                      # Tests for spec parsing
    ├── test_query/                      # Tests for query building
    ├── test_connectors/                 # Tests for backend connectors
    └── test_integration/                # End-to-end integration tests
```

### Dependencies (pyproject.toml)

```toml
[project]
name = "aitaem"
version = "0.1.0"
description = "Declarative metrics library for OLAP databases and CSV files"
dependencies = [
    "ibis-framework[duckdb]>=9.0.0",  # Core query abstraction
    "pyyaml>=6.0",                     # YAML parsing
    "pandas>=2.0.0",                   # Default output format
    "polars>=1.0.0",                   # Alternative output format
    "pyarrow>=14.0.0",                 # Zero-copy conversions
]

[project.optional-dependencies]
clickhouse = ["ibis-framework[clickhouse]>=9.0.0"]
druid = ["ibis-framework[druid]>=9.0.0"]
dev = [
    "pytest>=7.0.0",
    "pytest-cov>=4.0.0",
    "black>=23.0.0",
    "ruff>=0.1.0",
    "mypy>=1.0.0",
]
all = ["aitaem[clickhouse,druid,dev]"]
```

---

## Verification Plan

### Unit Tests

**Spec Parsing** (`tests/test_specs/`):
- Valid YAML parsing for metrics, slices, segments
- Validation error detection and messages
- SQL syntax validation
- Lazy loading and caching

**Query Building** (`tests/test_query/`):
- Ibis expression generation from specs
- Query optimization (grouping by table)
- Standard output formatting
- Cross-product slicing

**Connectors** (`tests/test_connectors/`):
- DuckDB connection and query execution
- ClickHouse connection (if available)
- Output format conversion (pandas/polars)

### Integration Tests

**End-to-end workflows** (`tests/test_integration/`):
1. Load sample data into DuckDB
2. Define metrics, slices, segments in YAML
3. Compute metrics with various combinations
4. Verify standard output format
5. Test pandas and polars output
6. Test multi-metric optimization
7. Test error handling (missing specs, invalid SQL, etc.)

### Manual Testing

**Example notebook** (`examples/quickstart.ipynb`):
- Connect to sample database
- Load example specs
- Compute metrics with various slices/segments
- Visualize results
- Demonstrate LLM-friendly workflow

---

## Design Rationale Summary

| Decision | Rationale |
|----------|-----------|
| **Ibis + DuckDB** | Backend portability, mature OLAP support, DuckDB federation, negligible overhead, LLM-friendly API |
| **pandas default** | LLM-friendly (abundant training data), ubiquity, stability, ecosystem compatibility |
| **Standard output format** | LLM integration, visualization simplicity, combining analyses, scalable narrow format |
| **Lazy spec loading** | Scalability for Phase 2 DB storage, fail fast on missing specs, session caching |
| **Single compute() method** | Simpler API, handles both single and multiple metrics elegantly |
| **SQL expressions in YAML** | No custom DSL, familiar syntax, direct mapping to backends, LLM-friendly |
| **numerator/denominator split** | Explicit ratio definition, supports complex CASE expressions, clear semantics |
| **Query optimization** | Efficient multi-metric computation, group by table, parallel execution |
| **Custom exceptions** | Clear error messages, rich context for debugging, user-friendly guidance |

---

## Next Steps

Once this architecture is approved:

1. **Create package structure**: Set up directories and `__init__.py` files
2. **Define dependencies**: Write `pyproject.toml` with Ibis, pandas, polars
3. **Implement specs module**: Start with YAML parsing and validation
4. **Implement connectors**: DuckDB connector first (ClickHouse later)
5. **Implement query builder**: Convert specs to Ibis expressions
6. **Implement insights API**: MetricCompute class and compute() method
7. **Write tests**: Unit and integration tests alongside implementation
8. **Create examples**: Example YAMLs and quickstart notebook
9. **Document**: Update README with usage guide

---

**End of Architecture Design Document**
