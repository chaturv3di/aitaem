# aitaem Library Architecture Design

## Executive Summary

This document defines the architecture for **aitaem** (All Interesting Things Are Essentially Metrics), a Python library for generating data insights from OLAP databases and CSV/Parquet files through declarative YAML specifications.

**Key Design Principles**:
- **Import depth ≤ 2**: All functionality accessible via `from aitaem import X` or `from aitaem.module import X`
- **LLM-friendly**: Standardized output format, familiar APIs, leveraging widely-known technologies
- **Lazy evaluation**: Specs loaded on-demand, queries built but not executed until needed
- **SQL-native**: Use DuckDB/SQL syntax directly in YAML specs (no custom DSL)
- **Loosely coupled**: Specifications of slices and segments are independent of metrics; modular, reusable specifications

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
- Bigquery/ClickHouse connectors for OLAP databases
- DuckDB federation for operational databases (Postgres, MySQL) via ATTACH

### 2. Output Format: **pandas (default) with polars support**

**Decision**: pandas as default DataFrame output, with polars as first-class alternative.

**Rationale**:
- **LLM-friendly (critical)**: pandas has abundant training data; LLMs struggle with polars syntax
- **Ubiquity**: Universal adoption, gentler learning curve for analysts
- **Stability**: Minimal breaking changes vs polars' evolving API
- **Ibis alignment**: pandas is Ibis's default output format
- **Polars option**: Easy to support via `output_format='polars'` for performance-sensitive users. Not a priority for phase 1

**Implementation**:
- Default: `compute()` returns pandas DataFrame
- Optional: `compute(..., output_format='polars')` returns polars DataFrame. Postpone until phase 2.
- Both leverage zero-copy Arrow conversion from DuckDB/Ibis

---

## Standard Output Format

All `compute()` calls return a single DataFrame in this standardized long format:

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| `period_type` | str | Time granularity | 'daily', 'weekly', 'monthly', 'all_time' |
| `period_start_date` | date/str | Start of period | '2026-02-01' |
| `period_end_date` | date/str | End of period | '2026-02-08' |
| `metric_name` | str | Name of the metric | 'click_rate', 'revenue', etc. |
| `slice_type` | str | Dimension(s) sliced (pipe-delimited) | 'geo', 'geo\|device', 'none' |
| `slice_value` | str | Value(s) of slice (pipe-delimited) | 'US', 'US\|mobile', 'all' |
| `segment_name` | str | Segment applied | 'user_tier', 'login_status' |
| `segment_value` | str | Segment applied | 'premium_users', 'all_users', 'logged_in', 'visitors', etc. |
| `metric_value` | float | Computed metric value | 0.045, 12500.0, etc. |

**Benefits**:
- **LLM integration**: Consistent format simplifies prompt engineering
- **Visualization**: Easy to pivot, filter, and plot
- **Combining analyses**: Stack results from multiple queries
- **Narrow & deep**: Scalable format for large result sets

**Example Row**:
```python
['weekly', '2026-02-01', '2026-02-08', 'click_rate', 'geo|device', 'US|mobile', 'user_tier', 'premium_users', 0.045]
```

---

## Module Architecture

```
aitaem/
├── __init__.py              # Top-level imports (depth-1 access)
├── insights.py              # PRIMARY USER INTERFACE
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
│   ├── connection.py       # ConnectionManager for multiple backends
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
from aitaem import compute, set_connections

# Depth 2 - Specific functionality
from aitaem.insights import MetricCompute
from aitaem.specs import MetricSpec, SliceSpec, SegmentSpec
from aitaem.connectors import IbisConnector, ConnectionManager
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
        Uses global ConnectionManager set via set_connections().

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

def set_connections(connections_yaml_path):
    """
    Load backend connections from YAML file.
    Sets global ConnectionManager instance.

    Args:
        connections_yaml_path: Path to connections.yaml file
    """
```

**Design Decisions**:
- **Single `compute()` method**: Accepts both single string and list of metric names
- **Lazy spec loading**: Specs loaded on-demand, cached for session
- **Standard output**: Always returns single DataFrame in standardized format
- **No metadata in output**: Keep DataFrame simple (metadata support in Phase 2)

**Usage Example**:
```python
from aitaem import set_connections
from aitaem.insights import MetricCompute

# Load backend connections from YAML
set_connections('config/connections.yaml')

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

### 2. `connectors/connection.py` - Connection Manager

**Purpose**: Manage multiple backend connections throughout a session.

**Key Classes**:

#### `ConnectionManager`
```python
class ConnectionManager:
    """
    Manages backend connections throughout a session.
    Supports multiple backend types (one connection per backend).
    """

    @classmethod
    def from_yaml(cls, yaml_path: str) -> 'ConnectionManager':
        """
        Load connections from YAML file.

        YAML format:
            duckdb:
              path: analytics.db
            bigquery:
              project_id: my-project
              credentials_path: ~/.config/gcloud/credentials.json
            clickhouse:
              host: prod.example.com
              database: events
              user: ${CLICKHOUSE_USER}
              password: ${CLICKHOUSE_PASSWORD}

        Supports environment variable substitution: ${VAR_NAME}
        """

    def add_connection(self, backend_type: str, **config):
        """
        Add a backend connection.

        Args:
            backend_type: 'duckdb', 'clickhouse', 'bigquery', etc.
            **config: Backend-specific configuration (credentials, paths, etc.)
        """

    def get_connection(self, backend_type: str) -> IbisConnector:
        """
        Retrieve connection by backend type.
        Returns IbisConnector instance for the backend.
        Raises ConnectionNotFoundError if backend not configured.
        """

    def get_connection_for_source(self, source_uri: str) -> IbisConnector:
        """
        Get appropriate connection for a metric's source URI.
        Parses URI to extract backend type, returns corresponding connection.

        Example:
            source_uri: 'bigquery://my-project.analytics.transactions'
            → Extracts 'bigquery' → Returns bigquery connection
        """

    @classmethod
    def get_global(cls) -> 'ConnectionManager':
        """
        Get global ConnectionManager instance.
        Set via set_connections() function.
        """
```

**Design Decisions**:
- **YAML-based configuration**: Separates infrastructure config from logic
- **One connection per backend**: Fully qualified table names handle multi-table scenarios
- **Environment variable substitution**: Secure credential management (${VAR_NAME})
- **Global singleton**: Set once via set_connections(), accessible throughout session
- **Explicit connection setup**: Fail fast with clear errors if connections missing

**Usage Example**:
```python
from aitaem.connectors import ConnectionManager

# Load connections from YAML
conn_mgr = ConnectionManager.from_yaml('connections.yaml')

# Or build programmatically
conn_mgr = ConnectionManager()
conn_mgr.add_connection('duckdb', path='analytics.db')
conn_mgr.add_connection('bigquery',
                       project_id='my-project',
                       credentials_path='~/.config/gcloud/credentials.json')

# Access global instance (set via set_connections)
conn_mgr = ConnectionManager.get_global()
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

**Purpose**: Build optimized Ibis query expressions from metric specifications, grouping metrics by source table for efficient batch execution.

**Key Class**:
```python
class QueryBuilder:
    """
    Builds optimized Ibis query expressions from metric specifications.
    Groups metrics by source table for efficient batch execution.
    All methods are static - no instance state required (Ibis expressions are lazy).
    """

    @staticmethod
    def build_queries(
        metric_specs: list[MetricSpec],
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        time_window: tuple | None
    ) -> list[QueryGroup]:
        """
        Build optimized queries for multiple metrics.

        Process:
        1. Group metrics by source table (optimization)
        2. For each group, build combined Ibis expression
        3. Generate standard output columns (period_type, metric_name, etc.)
        4. Return QueryGroup objects with lazy Ibis expressions

        Metrics from same source table are computed in single query.
        Metrics from different source tables are returned as separate QueryGroups
        for parallel execution.

        Returns:
            List of QueryGroups, each containing:
            - source: str (table URI)
            - metrics: list[MetricSpec]
            - query_expr: ibis.Expr (lazy expression, not executed)
        """

    @staticmethod
    def _build_single_metric_expression(
        metric_spec: MetricSpec,
        table: ibis.Table,
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        time_window: tuple | None
    ) -> ibis.Expr:
        """
        Internal helper to build Ibis expression for a single metric.

        Steps:
        1. Apply segment filter (if provided)
        2. Apply time window filter (if provided)
        3. Apply slices (generate rows for each slice value combination)
        4. Compute metric aggregation (numerator/denominator)
        5. Add standard output columns

        Returns lazy Ibis expression (not executed).
        Used by build_queries() when constructing grouped queries.
        """

    @staticmethod
    def _group_by_source(metric_specs: list[MetricSpec]) -> dict[str, list[MetricSpec]]:
        """
        Internal helper to group metrics by source table URI.

        Returns:
            Dict mapping source URI to list of metrics from that source
            Example: {'duckdb://analytics.db/events': [metric1, metric2],
                     'bigquery://project.dataset.table': [metric3]}
        """

    @staticmethod
    def _build_slice_expression(
        table: ibis.Table,
        slice_specs: list[SliceSpec]
    ) -> ibis.Expr:
        """
        Build slice cross-product (e.g., country x device).
        Returns expression with slice_type and slice_value columns.
        """

    @staticmethod
    def _parse_sql_expression(sql_expr: str, table: ibis.Table) -> ibis.Expr:
        """
        Parse SQL expression (from numerator/denominator) into Ibis expression.
        Uses Ibis's SQL string parsing or direct expression API.
        """

    @dataclass
    class QueryGroup:
        """Container for grouped metrics and their combined query expression."""
        source: str  # Table URI
        metrics: list[MetricSpec]
        query_expr: ibis.Expr  # Combined lazy query for all metrics in group
```

**Design Decisions**:
- **Unified builder + optimizer**: Consolidates query building and optimization into single class
- **Static methods**: No instance state needed (Ibis expressions are lazy, no connection required)
- **Grouping by source**: Metrics from same source table combined into single query
- **Clear separation**: QueryBuilder builds expressions, QueryExecutor executes them
- **Standard output generation**: Builder adds standard columns (period_type, metric_name, etc.)
- **Cross-product slicing**: Multiple slices create hierarchical combinations

#### 4.2 `query/executor.py` - QueryExecutor

**Purpose**: Execute Ibis queries and format results, with graceful handling of missing connections.

**Key Class**:
```python
class QueryExecutor:
    """
    Executes Ibis queries and formats results in standard output format.
    Uses global ConnectionManager for backend connections.
    """

    def execute(
        self,
        query_groups: list[QueryBuilder.QueryGroup],
        output_format: str = 'pandas'
    ) -> DataFrame:
        """
        Execute query groups (in parallel if multiple sources).
        Combine results into single DataFrame in standard format.
        Uses global ConnectionManager set via set_connections().

        Args:
            query_groups: List of QueryGroups from QueryBuilder.build_queries()
            output_format: 'pandas' or 'polars'

        Returns:
            Single DataFrame with all results in standard format.
            If some connections missing, returns partial results with warnings logged.
        """

    def _execute_query_group(
        self,
        query_group: QueryBuilder.QueryGroup,
        output_format: str
    ) -> DataFrame | None:
        """
        Execute a single query group.
        Uses global ConnectionManager to get backend connections.

        Returns:
            DataFrame with results, or None if connection unavailable.
            Logs warning if connection missing/failed.

        Example:
            try:
                conn_mgr = ConnectionManager.get_global()
                connector = conn_mgr.get_connection_for_source(query_group.source)
                result_df = connector.execute(query_group.query_expr, output_format)
                return result_df
            except ConnectionNotFoundError as e:
                logger.warning(f"Skipping metrics - connection not found: {e}")
                return None
        """

    def execute_single_query(self, query_expr: ibis.Expr) -> DataFrame:
        """Execute single Ibis expression."""

    def format_results(self, results: list[DataFrame]) -> DataFrame:
        """Combine and format multiple query results into standard output."""
```

**Design Decisions**:
- **Global ConnectionManager**: Accesses connections via ConnectionManager.get_global()
- **Partial computation**: Gracefully handles missing connections, returns partial results
- **Warning logs**: Failed metrics logged with clear warnings (Phase 1)
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
            backend_type: 'duckdb', 'clickhouse', 'bigquery', etc.
        """
        self.backend_type = backend_type
        self.connection = None

    def connect(self, connection_string: str, **kwargs):
        """
        Establish Ibis connection.

        Examples:
            - DuckDB: 'duckdb://analytics.db'
            - ClickHouse: 'clickhouse://host:port/database'
            - BigQuery: 'bigquery://host:port/druid/v2/sql'
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

### Connection Configuration Example

**connections.yaml**:
```yaml
# DuckDB - Local analytics database
duckdb:
  path: analytics.db
  # Optional: read_only, config options

# BigQuery - Cloud data warehouse
bigquery:
  project_id: my-gcp-project
  # Option 1: Use gcloud CLI credentials (default)
  # Option 2: Specify credentials file
  credentials_path: ~/.config/gcloud/application_default_credentials.json
  # Option 3: Inline credentials JSON
  # credentials_json: {...}

# ClickHouse - Production events database
clickhouse:
  host: prod.example.com
  port: 9000
  database: events
  # Environment variable substitution for security
  user: ${CLICKHOUSE_USER}
  password: ${CLICKHOUSE_PASSWORD}
  # Optional: secure, compression, etc.
```

**Usage with ConnectionManager**:
```python
from aitaem import set_connections
from aitaem.insights import MetricCompute

# Load connections from YAML (one-time setup)
set_connections('connections.yaml')

# Initialize MetricCompute
mc = MetricCompute.from_yaml(metric_paths='metrics/')

# Compute metrics across multiple backends (uses global connections)
df = mc.compute(['duckdb_metric', 'bigquery_metric', 'clickhouse_metric'])
```

**Advanced: Direct ConnectionManager Access**:
```python
from aitaem.connectors import ConnectionManager

# For advanced use cases, access global ConnectionManager directly
conn_mgr = ConnectionManager.get_global()

# Or create/use custom ConnectionManager instance (not typical)
custom_conn_mgr = ConnectionManager.from_yaml('connections.yaml')
```

**Connection-to-Metric Mapping**:
- Metric source URI: `bigquery://my-gcp-project.analytics.transactions`
- Extracted backend type: `bigquery`
- ConnectionManager looks up: `connections.yaml → bigquery → {...}`
- One connection per backend type handles all metrics of that type
- Fully qualified table names (project.dataset.table) specify exact tables within backend

---

## Usage Workflow

### 1. Setup Connection

```python
from aitaem import set_connections
# Or: from aitaem.connectors import ConnectionManager

# Load all backend connections from YAML
set_connections('config/connections.yaml')

# Or use ConnectionManager directly (advanced)
conn_mgr = ConnectionManager.from_yaml('config/connections.yaml')
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

# If some connections unavailable, partial results returned with warnings
# Example warning: "Skipping metric 'clickhouse_metric' - connection 'clickhouse' not configured"
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
- YAML-based connection management
- Partial computation with warning logs

**Deliverables**:
- All modules defined above
- Comprehensive unit tests
- Example YAML specs (metrics, slices, segments, connections)
- Basic documentation
- Connection YAML specification and parsing
- ConnectionManager.from_yaml() implementation
- Environment variable substitution in connection configs
- Partial computation with warning logs for missing connections

### Phase 2: Advanced Features (Future)

**Potential additions**:
- Multi-table metrics (joins)
- Metric dependencies (derived metrics referencing other metrics)
- Complex segments (HAVING clauses, subqueries)
- Database-backed spec storage
- Result caching
- Time-series-specific optimizations
- Additional backends (Druid, BigQuery, Snowflake)
- Metadata in output (query timing, SQL generated, failed metrics with detailed reasons)
- Async API for non-blocking execution
- Interactive credential prompting for missing auth
- Connection pooling and retry logic

---

## Critical Files to Create

Based on this architecture, here are the critical files needed for implementation:

### Package Structure
```
aitaem/
├── __init__.py                          # Top-level imports, set_connections() function
├── insights.py                          # MetricCompute class, compute() function
├── specs/
│   ├── __init__.py                      # Export MetricSpec, SliceSpec, SegmentSpec
│   ├── metric.py                        # MetricSpec class
│   ├── slice.py                         # SliceSpec class
│   ├── segment.py                       # SegmentSpec class
│   └── loader.py                        # SpecCache, loading functions
├── query/
│   ├── __init__.py                      # Export QueryBuilder, QueryExecutor (no QueryOptimizer)
│   ├── builder.py                       # QueryBuilder class (merged with optimization logic)
│   └── executor.py                      # QueryExecutor class (partial computation handling)
├── connectors/
│   ├── __init__.py                      # Export Connector, IbisConnector, ConnectionManager
│   ├── base.py                          # Connector abstract base class
│   ├── connection.py                    # ConnectionManager class (YAML parsing, env var substitution)
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
│   ├── connections.yaml                 # Example connection configuration
│   ├── connections.template.yaml        # Template with comments for user setup
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
| **ConnectionManager with YAML** | Separates infrastructure from logic, secure credential management, explicit setup, supports multi-backend cleanly |
| **One connection per backend** | Simplifies configuration, fully qualified table names handle multi-table scenarios, aligns with typical backend usage |
| **Unified QueryBuilder** | Eliminates artificial separation, optimization is part of building, clearer responsibilities, better testability |
| **Static QueryBuilder methods** | Ibis expressions are lazy (no connection needed), pure transformation, no instance state required |
| **Partial computation** | Graceful degradation when connections unavailable, user gets computable results, warnings logged (Phase 1 metadata Phase 2) |
| **Custom exceptions** | Clear error messages, rich context for debugging, user-friendly guidance |

---

**End of Architecture Design Document**
