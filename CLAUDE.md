# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**aitaem** (All Interesting Things Are Essentially Metrics) is a Python library for generating data insights from OLAP databases or local CSV files. It provides a declarative API for defining and computing metrics, slices, segments, and time series, and is designed to be LLM-friendly.

The core consists of:
1. **Specifications**: Declarative structures to define metric specs, slice/breakdown specs, and segment specs
2. **Computation**: Python classes that compute the metrics
3. **Utilities**: Connections to various data backends and visualization/rendering helpers

## Development Environment Setup

```bash
# Set Python version using pyenv
pyenv local 3.x.x  # Replace with desired Python version

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate

# Install development dependencies (once established)
pip install -e ".[dev]"
```

## Common Commands

(These will be added as the project structure is established. Expected categories:)
- Running tests
- Linting and formatting
- Building/packaging
- Running development server (if applicable)

## Architecture Notes

(To be documented as the codebase grows. Expected areas:)
- Specification system design
- Computation engine architecture
- Backend connector pattern
- Visualization/rendering pipeline

## Key Dependencies

(To be determined based on implementation choices)
