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
```

What happens:

1. `deptry` runs with JSON output enabled (using `python -m deptry --json-output ...`).
2. Any issues with code `DEP001` (missing dependency) are collected, and packages that already live inside the
   project tree are ignored.
3. Each remaining missing package is added to the target project with `uv add <package>`.

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
