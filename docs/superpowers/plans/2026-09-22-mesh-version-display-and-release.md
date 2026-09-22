# Mesh Version Display and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Add a runtime Mesh version to the injected browser status bar and establish a tag-based, CI-verified versioning workflow.

**Architecture:** `src/__init__.py` is the single version source. `pyproject.toml` reads it dynamically for package metadata. `src/main.py` replaces a JSON-safe version placeholder in `TRANSPORT_ADAPTER` before injecting the adapter into HTML; the browser only renders the supplied value. Git tags and `MESH_VERSION` control release installs, while `main` remains the development default.

**Tech Stack:** Python 3.11+, setuptools, FastAPI HTML injection, browser JavaScript template, Bash installer, pytest, GitHub Actions.

## Global Constraints

- Display format: `OpenCode Mesh v0.1.0`.
- Single source: `src/__init__.py` defines `__version__ = "0.1.0"`.
- Protocol field `transport-manifest.version = 1` remains a protocol version and is not changed.
- Installer defaults to `main`; `MESH_VERSION=v0.1.0` selects a release tag.
- Existing terminal, CSRF, RTT, and transport reliability fixes must remain intact.
- Project comments remain English where code comments are added; user-facing documentation remains Chinese/English consistent with the existing file.

## Task 1: Establish Runtime Version Source

**Files:**
- Modify: `src/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_mesh_reliability.py`

**Interfaces:**
- Produces `src.__version__: str`.
- Produces package metadata version through setuptools dynamic attribute loading.

- [ ] **Step 1: Write failing version-source tests**

Add tests that import `src.__version__`, require semantic version `0.1.0`, and require `pyproject.toml` to declare a dynamic `version` field sourced from `src.__version__`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_mesh_reliability.py -k 'version_source or pyproject_uses_dynamic_src_version'
```

Expected: failure because `src.__init__` has no `__version__` and `pyproject.toml` still contains a literal version.

- [ ] **Step 3: Implement the single source**

Set `src/__init__.py` to:

```python
__version__ = "0.1.0"
```

Change `pyproject.toml` to use:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
dynamic = ["version"]

[tool.setuptools.dynamic]
version = {attr = "src.__version__"}

[tool.setuptools.packages.find]
where = ["."]
include = ["src*"]
```

Retain the existing project name, description, Python floor, dependencies, and entry point.

- [ ] **Step 4: Run focused tests and package metadata verification**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_mesh_reliability.py -k version_source
.venv/bin/python -m pip install -e . -q
.venv/bin/python -c 'import importlib.metadata; assert importlib.metadata.version("opencode-mesh") == "0.1.0"; print("metadata version OK")'
```

Expected: focused tests pass and metadata prints `metadata version OK`.

## Task 2: Inject and Render the Browser Version

**Files:**
- Modify: `src/static_adapter.py`
- Modify: `src/main.py`
- Test: `tests/test_mesh_reliability.py`

**Interfaces:**
- `inject_mesh_bar(body: bytes) -> bytes` replaces `__OCM_VERSION_JSON__` with JSON-encoded `src.__version__`.
- Injected JavaScript defines `MESH_VERSION` and renders `OpenCode Mesh v<version>` in `.ocm-title`.

- [ ] **Step 1: Write failing injection tests**

Add tests that call `inject_mesh_bar` with HTML containing `</head>` and assert the output contains `OpenCode Mesh`, the current version, and no `__OCM_VERSION_JSON__` placeholder. Add a test that calling injection twice does not duplicate the adapter.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_mesh_reliability.py -k 'mesh_bar_injects_runtime_version or mesh_bar_injection_is_idempotent_with_version'
```

Expected: failure because the adapter has no version placeholder and `inject_mesh_bar` performs no replacement.

- [ ] **Step 3: Implement version injection**

In `src/static_adapter.py`, add a JSON-safe placeholder to the adapter and render a version element/text next to the existing title without changing transport indicators.

In `src/main.py`, import `__version__` and replace `__OCM_VERSION_JSON__` with `json.dumps(__version__)` before UTF-8 encoding the adapter. Keep the existing idempotent injection behavior.

- [ ] **Step 4: Run focused tests and JS syntax validation**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_mesh_reliability.py -k 'mesh_bar_injects_runtime_version or mesh_bar_injection_is_idempotent_with_version'
python3 - <<'PY'
from pathlib import Path
import re
text = Path('src/static_adapter.py').read_text()
script = re.search(r'TRANSPORT_ADAPTER = r"""\n(.*?)\n"""', text, re.S).group(1)
Path('/tmp/opencode/transport-adapter-check.js').write_text(re.sub(r'^<script[^>]*>\n|\n</script>$', '', script))
PY
node --check /tmp/opencode/transport-adapter-check.js
```

Expected: tests pass and `node --check` exits successfully.

## Task 3: Version-Aware Installer and Release Documentation

**Files:**
- Modify: `scripts/install.sh`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Create: `CHANGELOG.md`

**Interfaces:**
- `MESH_VERSION` selects a Git tag or branch; unset defaults to `main`.
- Installer prints the selected source/version.

- [ ] **Step 1: Write installer behavior checks**

Add a shell-level static test or test commands that verify `MESH_VERSION` is used and that tag values build `refs/tags/<version>` URLs while `main` builds `refs/heads/main`.

- [ ] **Step 2: Run the installer checks and confirm RED**

Run the check against the current script and confirm it fails because the script hardcodes `VERSION="main"` and `refs/heads`.

- [ ] **Step 3: Implement release selection**

Use `MESH_VERSION="${MESH_VERSION:-main}"`; choose `refs/tags/${MESH_VERSION}` for values beginning with `v`, otherwise use `refs/heads/${MESH_VERSION}`. Print the selected version/source after resolving it.

- [ ] **Step 4: Document the workflow**

Document development installs from `main`, release installs from `v0.1.0`, version bump rules, tag naming, and rollback to a prior tag. Add `CHANGELOG.md` with an `Unreleased` section and the initial `0.1.0` section.

- [ ] **Step 5: Run installer syntax and documentation checks**

Run:

```bash
bash -n scripts/install.sh
```

Expected: exit 0.

## Task 4: CI and Release Validation

**Files:**
- Create: `.github/workflows/test.yml`
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Add CI workflow**

Run pytest on pushes and pull requests using Python 3.11 and `pip install -e .`.

- [ ] **Step 2: Add tag validation workflow**

On `v*` tags, parse the tag and assert it equals `src.__version__`, then run the full test suite.

- [ ] **Step 3: Validate workflow YAML and local commands**

Run the full local test suite and inspect both workflow files for exact Python/install/test commands.

## Task 5: Integrate, Tag, Deploy, and Push

**Files:**
- All files from Tasks 1–4 plus existing uncommitted reliability fixes.

- [ ] **Step 1: Run the complete verification suite**

Run:

```bash
.venv/bin/python -m pytest -q
bash -n scripts/install.sh
```

- [ ] **Step 2: Review status and diff**

Run `git status --short`, `git diff --check`, and `git diff --stat`; ensure no credentials, local config, caches, or unrelated worktree files are staged.

- [ ] **Step 3: Commit the implementation**

Use a Conventional Commit such as:

```bash
git add .
git commit -m "feat: add mesh version display and release workflow"
```

- [ ] **Step 4: Create and push the initial release tag**

After the commit is verified, create and push `v0.1.0`:

```bash
git push origin v0.1.0
```

- [ ] **Step 5: Deploy runtime files and verify services**

Copy the updated `src/main.py` and `src/static_adapter.py` to the VPS gateway, restart `opencode-mesh-gateway.service`, restart the WSL agent, verify both services are active, and load the injected HTML to confirm the displayed version.
