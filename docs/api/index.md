# API Reference

All public classes are importable directly from `aitaem`:

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute
```

## Class Overview

| Class | Module | Purpose |
|-------|--------|---------|
| [`MetricCompute`](insights.md) | `aitaem.insights` | Primary interface — compute metrics |
| [`SpecCache`](specs.md) | `aitaem.specs.loader` | Load and cache YAML specs |
| [`MetricSpec`](specs.md#aitaem.specs.metric.MetricSpec) | `aitaem.specs.metric` | Metric spec dataclass |
| [`SliceSpec`](specs.md#aitaem.specs.slice.SliceSpec) | `aitaem.specs.slice` | Slice spec dataclass |
| [`SegmentSpec`](specs.md#aitaem.specs.segment.SegmentSpec) | `aitaem.specs.segment` | Segment spec dataclass |
| [`ConnectionManager`](connectors.md) | `aitaem.connectors.connection` | Manage backend connections |
