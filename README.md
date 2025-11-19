# deptry-auto

Scan a Python project with [deptry](https://deptry.com/) and automatically add any missing runtime dependencies with `uv add`.

## Installation

Install the package into the project you would like to manage (or into a dedicated tools environment):

```bash
uv add deptry-auto
```

> ℹ️ `deptry-auto` expects `uv` to be available on your `PATH`.

## Usage

Run the CLI from the directory that contains the `pyproject.toml`, or pass the target path explicitly:

```bash
# Scan the current project and apply fixes
deptry-auto

# Scan another project from anywhere
deptry-auto path/to/other-project

# Preview the changes without modifying the project
deptry-auto --dry-run

# Wait up to 2 minutes per dependency install
deptry-auto --install-timeout 120
```

What happens:

1. `deptry` runs with JSON output enabled (using `python -m deptry --json-output ...`).
2. Any issues with code `DEP001` (missing dependency) are collected, and packages that already live inside the
   project tree are ignored.
3. Each remaining missing package is added to the target project with `uv add <package>`.

### Special cases

- The installer automatically queries the PyPI XML-RPC API for likely package matches when an import name is missing (so `PIL` resolves to `Pillow`, `cv2` to `opencv-python`, and similar cases work without manual overrides).
- Imports that only exist on MicroPython builds (`machine`, `micropython`, `rp2`, `ucollections`, `ujson`, `uselect`, `ustruct`, `utime`, `neopixel`) are skipped automatically.
- Every `uv add` command is capped by `--install-timeout` (default 300s). When the timeout is reached, deptry-auto stops that attempt, switches to the next candidate name, and ultimately reports the packages that still need manual attention.
- If one installation fails, `deptry-auto` now continues with the remaining packages, tries fallback names where applicable, and reports any failures at the end.

`deptry` exits with code `1` when it finds issues, so `deptry-auto` tolerates both `0` (clean) and `1` (issues) but still halts for any other failure. Use `--dry-run` when you only need a report of the missing dependencies.

## Development

```bash
uv sync --group dev            # install dependencies
uv run pre-commit install      # install pre-commit hooks
uv run deptry-auto --dry-run .
```

The local pre-commit hook automatically bumps the patch version whenever a file under `src/` is staged. The first
commit attempt will therefore fail after the hook updates `pyproject.toml`; stage the modified file and re-run `git commit`.

Run the CLI against a throwaway project if you want to observe `uv add` in action.
