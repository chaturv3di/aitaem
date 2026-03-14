# Plan: Package Documentation on GitHub Pages

## Goal

Set up automated, versioned Python package documentation hosted on GitHub Pages, built with **MkDocs + Material theme** and auto-deployed on every push to `main`.

---

## Tool Choice: MkDocs + Material

| Criterion | MkDocs Material | Sphinx | pdoc |
|---|---|---|---|
| Setup effort | Low | High | Very low |
| Appearance | Modern, polished | Dated (default) | Basic |
| Markdown support | Native | RST-first | Markdown |
| API autodoc | mkdocstrings | autodoc | Built-in |
| GitHub Pages deploy | Built-in `gh-deploy` | Manual | Manual |
| Community | Large, active | Large | Small |

MkDocs with the Material theme is the right choice: minimal config, beautiful output, first-class GitHub Pages support via `mkdocs gh-deploy`, and `mkdocstrings` for auto-generating API docs from Python docstrings.

---

## Architecture

```
docs/
├── index.md                  # Home page (drawn from README intro)
├── getting-started.md        # Installation + quick start
├── user-guide/
│   ├── specs.md              # Writing YAML specs (MetricSpec, SliceSpec, SegmentSpec)
│   ├── connectors.md         # Connecting to DuckDB / BigQuery / CSV
│   └── computing-metrics.md  # Using MetricCompute
├── api/
│   ├── index.md              # API reference overview
│   ├── insights.md           # MetricCompute autodoc
│   ├── specs.md              # SpecCache + Spec classes autodoc
│   └── connectors.md         # ConnectionManager autodoc
└── changelog.md              # Pulled from git tags / CHANGELOG
mkdocs.yml                    # MkDocs configuration
.github/workflows/docs.yml    # Deploy workflow
```

---

## Implementation Steps

### Step 1 — Add documentation dependencies

Add to `pyproject.toml` under `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
docs = [
    "mkdocs>=1.6",
    "mkdocs-material>=9.5",
    "mkdocstrings[python]>=0.27",
    "mkdocs-autorefs>=1.2",
]
```

Install locally with `uv pip install -e ".[docs]"`.

**Verification**: `mkdocs --version` succeeds.

---

### Step 2 — Add docstrings to public API

Audit and add Google-style docstrings to all public classes and methods:
- `aitaem/insights.py` — `MetricCompute`
- `aitaem/specs/loader.py` — `SpecCache`
- `aitaem/specs/metric.py`, `slice.py`, `segment.py` — spec dataclasses
- `aitaem/connectors/connection.py` — `ConnectionManager`

Docstring format (Google style, compatible with mkdocstrings):

```python
def compute(self, metric_names: list[str]) -> pd.DataFrame:
    """Compute the requested metrics.

    Args:
        metric_names: Names of metrics defined in the loaded specs.

    Returns:
        A DataFrame with one row per metric and columns for value,
        slice, segment, and time period.

    Raises:
        MetricNotFoundError: If a requested metric is not in the spec cache.
    """
```

**Verification**: `mkdocstrings` renders all API pages without warnings.

---

### Step 3 — Create `mkdocs.yml`

Create `mkdocs.yml` at the project root:

```yaml
site_name: aitaem
site_description: All Interesting Things Are Essentially Metrics
site_url: https://<github-org>.github.io/aitaem
repo_url: https://github.com/<github-org>/aitaem
repo_name: aitaem
edit_uri: edit/main/docs/

theme:
  name: material
  palette:
    - scheme: default
      primary: indigo
      accent: indigo
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - scheme: slate
      primary: indigo
      accent: indigo
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
  features:
    - navigation.tabs
    - navigation.sections
    - navigation.top
    - search.suggest
    - search.highlight
    - content.code.copy

plugins:
  - search
  - mkdocstrings:
      handlers:
        python:
          options:
            docstring_style: google
            show_source: true
            show_root_heading: true
            show_signature_annotations: true
            members_order: source

nav:
  - Home: index.md
  - Getting Started: getting-started.md
  - User Guide:
      - Writing Specs: user-guide/specs.md
      - Connectors: user-guide/connectors.md
      - Computing Metrics: user-guide/computing-metrics.md
  - API Reference:
      - Overview: api/index.md
      - MetricCompute: api/insights.md
      - Specs: api/specs.md
      - Connectors: api/connectors.md
  - Changelog: changelog.md

markdown_extensions:
  - admonition
  - pymdownx.details
  - pymdownx.superfences
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.inlinehilite
  - pymdownx.snippets
  - pymdownx.tabbed:
      alternate_style: true
  - attr_list
  - md_in_html
```

**Note**: Replace `<github-org>` with the actual GitHub username/org before committing.

**Verification**: `mkdocs build --strict` exits 0 with no warnings.

---

### Step 4 — Write documentation content

Create Markdown files under `docs/`:

#### `docs/index.md`
- Project tagline and one-paragraph description
- Badges (CI, PyPI, Python version) — same as README
- "Why aitaem?" — pulled from README
- Links to Getting Started and API Reference

#### `docs/getting-started.md`
- Installation (`pip install aitaem` and optional extras)
- Complete working example using DuckDB (copy from README Quick Start)
- Link to examples/ directory

#### `docs/user-guide/specs.md`
- YAML spec format for MetricSpec, SliceSpec, SegmentSpec
- Annotated examples for each spec type
- Validation rules and error messages

#### `docs/user-guide/connectors.md`
- Supported backends (DuckDB, BigQuery, CSV via DuckDB)
- `ConnectionManager` configuration examples
- How to add a new backend

#### `docs/user-guide/computing-metrics.md`
- `MetricCompute` lifecycle (load specs → connect → compute)
- Filtering and slicing examples
- Output DataFrame schema

#### `docs/api/` pages
Each API page uses mkdocstrings autodoc directives:

```markdown
# MetricCompute

::: aitaem.MetricCompute
```

#### `docs/changelog.md`
- Manual changelog, one section per release, in reverse chronological order
- Link to GitHub Releases page for full diff

**Verification**: `mkdocs serve` runs locally and all pages render correctly with no broken links.

---

### Step 5 — GitHub Actions deploy workflow

Create `.github/workflows/docs.yml`:

```yaml
name: Deploy Docs

on:
  push:
    branches: [main]
  workflow_dispatch:          # allow manual re-deploy

permissions:
  contents: write             # needed to push to gh-pages branch

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0      # full history for git-revision-date plugin

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install ".[docs]"

      - name: Build and deploy docs
        run: mkdocs gh-deploy --force
```

This pushes the built site to the `gh-pages` branch of the same repo.

**Verification**: After merging to `main`, the Actions run succeeds and the site is live at `https://<github-org>.github.io/aitaem`.

---

### Step 6 — Enable GitHub Pages

In the repository settings:
1. Navigate to **Settings → Pages**
2. Set **Source** to `Deploy from a branch`
3. Set **Branch** to `gh-pages`, folder `/` (root)
4. Save

This is a one-time manual step — subsequent deployments are fully automatic.

**Verification**: The GitHub Pages URL resolves and shows the MkDocs site.

---

### Step 7 — Add docs badge to README

Add a badge to `README.md`:

```markdown
[![Docs](https://img.shields.io/badge/docs-github.io-blue)](https://<github-org>.github.io/aitaem)
```

**Verification**: Badge renders correctly in the README on GitHub.

---

## Definition of Done

- [ ] `mkdocs build --strict` passes locally with zero warnings
- [ ] All public classes and methods have docstrings rendered in the API reference
- [ ] `docs.yml` workflow deploys successfully on push to `main`
- [ ] GitHub Pages URL is live and accessible
- [ ] Docs badge added to README
- [ ] Future pushes to `main` automatically update the live docs

---

## Dependencies Added

| Package | Version | Purpose |
|---|---|---|
| `mkdocs` | >=1.6 | Documentation site builder |
| `mkdocs-material` | >=9.5 | Modern Material theme |
| `mkdocstrings[python]` | >=0.27 | Auto-generate API docs from docstrings |
| `mkdocs-autorefs` | >=1.2 | Cross-references between API pages |

All added under `[project.optional-dependencies.docs]` in `pyproject.toml`.
