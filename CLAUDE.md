# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**aitaem** (All Interesting Things Are Essentially Metrics) is a Python library for generating data insights from OLAP databases or local CSV files. It provides a declarative API for defining and computing metrics, slices, segments, and time series, and is designed to be LLM-friendly.

### Architecture
Reference architecture of the project, subject to updates.

```
.python-version               # Top level file containing python version
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
├── helpers/                 # User-facing convenience functions
│   ├── __init__.py
│   └── csv_to_duckdb.py    # load_csvs_to_duckdb utility
└── utils/                   # Internal utilities (not user-facing)
│   ├── __init__.py
│   ├── validation.py        # YAML validation with clear errors
│   ├── exceptions.py        # Custom exception classes
│   └── formatting.py        # DataFrame formatting/conversion
tests/                         # All test cases go here
├── test_insights.py         # Test cases for the primary interface
├── test_insights_XYZ.py     # Additional/specific test cases for the primary interface
├── ...                      # Other root-level/integration tests
├── test_connectors/
│   ├── __init__.py
│   ├── ...                  # Test cases for connectors module
├── test_query/
│   ├── __init__.py
│   ├── ...                  # Test cases for query
├── test_utils/
│   ├── __init__.py
│   ├── ...                  # Test cases for utils
└── test_specs/
    ├── __init__.py
    └── ...                  # Test cases for specs module
```

## Development Instructions
Strictly follow the instructions in the relevant section(s).

### Documentation Instructions
After any change that adds or removes a public-facing module or function:
1. Check `aitaem/__init__.py` and each subpackage's `__init__.py` (e.g. `aitaem/helpers/__init__.py`) for new or removed exports
2. Create or remove the corresponding page under `docs/api/`
3. Add or remove the page from the `nav` in `mkdocs.yml`
4. Update `docs/api/index.md` to reflect the change in the overview tables
5. Add a `docs/changelog.md` entry for the release

### Common Instructions
These instructions apply regardless of the nature of implementation task, whether it's implementing a plan, an ad-hoc feature, a bug-fix, or anything else.

0. Feel free to use agents/teammates for steps that can be executed in parallel
1. Always double check your work. Create a checklist of all the claims you make and tick off claims that you are able to verify.
2. Focus only on the task at hand. Do not create additional scope of work.
3. Always validate the correctness of the implementation using a test case.
4. CRITICAL: Never make any assumptions. Ask clarifying questions. Make sure you have complete information and are aligned with me on the exact requirements and next steps.
5. Use the following testing strategy and feel free to use agents/teammates for this.
    1. First look for existing test(s) that can be used to validate the implementation
    2. Implement new test case(s) if no existing ones suffice
    3. Execute the test(s) using `pytest` and leverage `pytest-cov` to ensure test coverage
    4. If all the test cases pass, create a git commit automatically with a brief description of changes
    5. If any tests fail, think deeply about the root cause and the amount of change needed to fix it
    6. Automatically debug the code if the debugging is limited to the most recent code changes
    7. If debugging requires changes to code/files which are outside of the scope of the current task/context, provide a justification for the proposed changes and ask for permission BEFORE making those bugfixes
6. If a new Python package is required, then add the dependency in `pyproject.toml` file.
    1. Install any dependencies ONLY USING `uv pip install <package>`
    2. It is safe to assume that `uv` is already available
7. Use `ruff` for formatting and linting
8. When managing context throughout implementation, follow these principles:
    1. Always preserve the current task description and checklist.
    2. Keep the full list of modified files.
    3. Retain any active test commands and their results.
    4. Summarize deep debugging logs but keep key decisions.

### Implementing Plans
Strictly follow the instructions below.
0. Always expect a path to a plan document to be provided whenever asked to implement a plan
    1. Plan documents reside in the [plans folder](./plans/); an exact plan document will be pointed out
    2. Expect one plan document at a time
    3. Always stay consistent with the plan document which is provided
1. Break down each class and each feature into multiple sub-features.
    1. The definition of a sub-feature is a minimal functionality that can be tested
    2. From the perspective of a class, each method could be a sub-feature. However, it is possible that some methods can also be implemented incrementally with each incremental addition being a sub-feature
    3. Add a package dependency in the `pyproject.toml` file only if the current sub-feature requires it
2. Think about sub-features critically before starting the implementation
    1. Create a logical order of (sub-)features to implement. If A depends on B, then implement B first and then A
    2. Implement the sub-feature which is next in line
    3. Follow the testing strategy defined above to test the sub-feature
3. After each sub-feature implementation, proactively manage context to stay focused

## Release Process
When creating a release:

  1. Create a `release/vX.Y.Z` branch from `main`
  2. Commit the version bump in `pyproject.toml` on that branch
  3. Push the branch and open a PR into `main`
  4. Wait for PR approval and merge — do NOT proceed until the PR is merged
  5. After merge, create an annotated tag on `main`: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
  6. Push the tag: `git push origin vX.Y.Z`
  7. Create a GitHub release using `gh release create`
  8. Publishing the GitHub release automatically triggers the `publish.yml` GitHub Actions workflow, which builds the package and uploads it to PyPI via Trusted Publishing (OIDC). A reviewer must approve the deployment in the GitHub Actions UI (under the `pypi` environment) before the upload proceeds. Monitor progress in the repository's Actions tab.

  Never commit version bumps or release prep directly to `main`.

## Common Commands
- Running tests: `python -m pytest`
- Dependency management: `uv`
- Linting and formatting: `ruff`
- Building/packaging: `hatchling`
- Running development server (if applicable)
