# Plan 06: PyPI Publishing with GitHub Actions CI/CD

## Context

The `aitaem` library is ready for public distribution. This plan covers all steps required to publish the package to PyPI, including package metadata, CI/CD automation, and one-time external configurations.

**Decisions captured from requirements:**
- **CI/CD platform**: GitHub Actions
- **PyPI authentication**: Trusted Publishing (OIDC) — no long-lived API tokens stored as secrets
- **TestPyPI**: Skipped — publish directly to production PyPI
- **Publish trigger**: GitHub Release published event

---

## Gap Analysis

| Component | Status | Action |
|-----------|--------|--------|
| `pyproject.toml` metadata | ⚠️ Incomplete | Add `authors`, `license`, `keywords`, `classifiers`, `readme`, `[project.urls]` |
| `pyproject.toml` dev deps | ⚠️ Missing `build` | Add `build>=1.0.0` for building distribution packages |
| `.github/workflows/ci.yml` | ❌ Missing | PR/push quality gates: pytest (matrix), ruff, mypy |
| `.github/workflows/publish.yml` | ❌ Missing | Release-triggered publish to PyPI via OIDC |
| PyPI Trusted Publisher | ❌ Missing | One-time setup on pypi.org (manual, external) |
| GitHub branch protection | ❌ Missing | Require CI status checks before merge (manual, external) |
| `CLAUDE.md` | ⚠️ Outdated | Update release process to note `gh release create` triggers auto-publish |

---

## Sub-Features (Ordered by Dependency)

### Sub-Feature 1: Enhance `pyproject.toml` metadata

PyPI requires or strongly recommends several metadata fields beyond what is currently present.

**Fields to add to `[project]`:**
- `readme = "README.md"` — renders the README on the PyPI project page
- `license = {file = "LICENSE"}` — declares the license (LICENSE file already exists)
- `authors = [{name = "<author-name>", email = "<author-email>"}]`
- `keywords = ["metrics", "analytics", "OLAP", "data", "insights", "ibis"]`
- `classifiers = [...]` — standardized PyPI classifiers:
  - `"Development Status :: 3 - Alpha"` (adjust to Beta/Stable when appropriate)
  - `"Intended Audience :: Developers"`
  - `"Intended Audience :: Science/Research"`
  - `"Topic :: Scientific/Engineering :: Information Analysis"`
  - `"License :: OSI Approved :: <License Name>"` (match LICENSE file)
  - `"Programming Language :: Python :: 3"`
  - `"Programming Language :: Python :: 3.9"`
  - `"Programming Language :: Python :: 3.10"`
  - `"Programming Language :: Python :: 3.11"`
  - `"Programming Language :: Python :: 3.12"`

**New section `[project.urls]` to add:**
```toml
[project.urls]
Homepage = "https://github.com/<owner>/aitaem"
Repository = "https://github.com/<owner>/aitaem"
Issues = "https://github.com/<owner>/aitaem/issues"
```

**Dev dependency addition:**
- Add `build>=1.0.0` to `[project.optional-dependencies].dev`

**Verification**: `python -m build --sdist --wheel` completes without errors; `dist/` contains a `.tar.gz` and a `.whl` file.

---

### Sub-Feature 2: Create `.github/workflows/ci.yml`

CI workflow that runs on every push and every pull request targeting `main`.

**Trigger**:
- `push` to any branch
- `pull_request` targeting `main`

**Jobs**:

**Job 1 — `test`** (Python matrix: 3.9, 3.11, 3.12):
1. Checkout code
2. Set up Python (matrix version)
3. Install uv: `pip install uv`
4. Install package with dev deps: `uv pip install -e ".[dev]"`
5. Run tests with coverage: `python -m pytest --cov=aitaem tests/`

**Job 2 — `lint`** (single Python version, 3.12):
1. Checkout code
2. Set up Python 3.12
3. Install uv: `pip install uv`
4. Install dev deps: `uv pip install -e ".[dev]"`
5. Run ruff: `ruff check aitaem/ tests/`

**Job 3 — `type-check`** (single Python version, 3.12):
1. Checkout code
2. Set up Python 3.12
3. Install uv: `pip install uv`
4. Install dev deps: `uv pip install -e ".[dev]"`
5. Run mypy: `mypy aitaem/`

**Verification**: Open a test PR; all three jobs (test × 3 versions, lint, type-check) appear in the PR checks and turn green.

---

### Sub-Feature 3: Create `.github/workflows/publish.yml`

Publish workflow triggered automatically when a GitHub Release is published.

**Trigger**: `release: types: [published]`

**Required job-level permissions** (enables OIDC token exchange with PyPI):
```yaml
permissions:
  id-token: write
  contents: read
```

**Optional but recommended — GitHub Environment**: Reference a GitHub Environment named `pypi` in the publish job (`environment: pypi`). This allows adding required reviewers as a deployment protection rule — a human must approve each PyPI publish before the upload proceeds. If a GitHub Environment is used, the Trusted Publisher configuration on pypi.org must reference the same environment name.

**Steps**:
1. Checkout code
2. Set up Python 3.12
3. Install `build`: `pip install build`
4. Build distributions: `python -m build` (produces `dist/*.tar.gz` and `dist/*.whl`)
5. Publish to PyPI: `uses: pypa/gh-action-pypi-publish@release/v1` (no password; OIDC handles auth)

**Verification**: After Sub-Feature 4 (external PyPI setup) is complete, publish a GitHub Release; confirm the `publish.yml` Actions run succeeds and the package version appears on pypi.org.

---

### Sub-Feature 4: External — Configure PyPI Trusted Publishing (manual)

> **Who**: Repository owner | **Where**: pypi.org | **When**: Before first publish

This one-time setup allows PyPI to accept uploads from the GitHub Actions publish workflow without an API token.

**Steps**:
1. Log in to [pypi.org](https://pypi.org)
2. Go to **Account Settings → Publishing** (direct link: https://pypi.org/manage/account/publishing/)
3. Click **"Add a new pending publisher"** (the `aitaem` project does not yet exist on PyPI, so use the pending publisher form rather than a project-level form)
4. Fill in the form:
   - **PyPI Project Name**: `aitaem`
   - **Owner**: `<your-github-username-or-org>`
   - **Repository name**: `aitaem`
   - **Workflow filename**: `publish.yml`
   - **Environment name**: `pypi` (if using a GitHub Environment per Sub-Feature 3; otherwise leave blank)
5. Click **Add**

**Result**: PyPI will trust OIDC tokens issued by GitHub Actions from the specified workflow and repository, and accept uploads from it — no API token needed.

**Note**: After the first successful publish, the pending publisher automatically converts to a linked publisher visible in the project's Publishing settings.

---

### Sub-Feature 5: External — Configure GitHub Branch Protection (manual)

> **Who**: Repository owner | **Where**: GitHub repository Settings | **When**: After `ci.yml` runs at least once (so check names are visible)

**Steps**:
1. Go to GitHub repository → **Settings → Branches**
2. Click **"Add branch protection rule"** for the pattern `main`
3. Enable the following:
   - ✅ **Require status checks to pass before merging**
     - Search for and add these required checks (names match job IDs in `ci.yml`):
       - `test (3.9)`
       - `test (3.11)`
       - `test (3.12)`
       - `lint`
       - `type-check`
   - ✅ **Require branches to be up to date before merging** (recommended)
4. Save changes

**Optional — Create GitHub `pypi` deployment environment** (if using environment protection in Sub-Feature 3):
1. Go to **Settings → Environments → New environment**, name it `pypi`
2. Add **Required reviewers** (yourself or a team) to gate production deploys
3. Optionally restrict to the `main` branch only

**Result**: No code merges to `main` without passing CI; no PyPI publish proceeds without reviewer approval (if environment is configured).

---

### Sub-Feature 6: Update `CLAUDE.md` Release Process

Update the **Release Process** section in `CLAUDE.md` to reflect the new automated publish step.

**Change**: After step 7 (`gh release create`), add a note clarifying that:
- Publishing the GitHub Release automatically triggers the `publish.yml` workflow
- The workflow builds and uploads the package to PyPI via OIDC
- The Actions tab on GitHub can be used to monitor publish progress and debug failures
- If a `pypi` GitHub Environment with required reviewers is configured, a reviewer must approve the deployment in the Actions UI before the upload proceeds

---

## Critical Files Summary

### Files to create/modify in this repository

| File | Change Type | Description |
|------|-------------|-------------|
| `pyproject.toml` | Modify | Add required PyPI metadata fields + `build` dev dep |
| `.github/workflows/ci.yml` | Create | PR/push quality gate: pytest matrix, ruff, mypy |
| `.github/workflows/publish.yml` | Create | Release-triggered PyPI publish via OIDC |
| `CLAUDE.md` | Modify | Update release process section |

### External (manual) steps — no files in this repo

| Platform | Step | Sub-Feature |
|----------|------|-------------|
| pypi.org | Configure Trusted Publisher (pending publisher form) | Sub-Feature 4 |
| GitHub repo settings | Add branch protection rules for `main` | Sub-Feature 5 |
| GitHub repo settings | (Optional) Create `pypi` deployment environment with required reviewers | Sub-Feature 5 |

---

## Verification Plan

1. **Local build check**: `python -m build` produces valid `.tar.gz` and `.whl` in `dist/` — no errors
2. **CI check**: Open a test PR; all GitHub Actions jobs in `ci.yml` turn green (test × 3 versions, lint, type-check)
3. **Publish check**: After external setup (Sub-Feature 4 and 5), publish a GitHub Release following the existing release process in `CLAUDE.md`; confirm the `publish.yml` workflow run succeeds and the package appears on pypi.org at the correct version
