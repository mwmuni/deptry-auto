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

# Auto-detect build environment and set up if needed, then scan and install
deptry-auto --auto-bootstrap

# Prefer a specific build system (msvc, cmake, ninja, meson)
deptry-auto --build-pref ninja

# Prefer specific build system with auto-bootstrap
deptry-auto --build-pref cmake --auto-bootstrap --install-timeout 600
```

### Building Packages from Source

Some packages (like `scikit-image`, `opencv-python`) may need to be compiled on Windows if pre-built wheels aren't available for your Python version (especially Python 3.14).

The easiest way to handle this is with the `--auto-bootstrap` flag:

```bash
# Auto-detect build environment and set up if needed, then scan and install
deptry-auto --auto-bootstrap --install-timeout 600
```

For more details on manual setup, see the Bootstrap Guide below.

What happens:

1. `deptry` runs with JSON output enabled (using `python -m deptry --json-output ...`).
2. Any issues with code `DEP001` (missing dependency) are collected, and packages that already live inside the
   project tree are ignored.
3. Each remaining missing package is added to the target project with `uv add <package>`.
4. If batch installation fails, the tool falls back to installing packages individually.
5. Platform constraints and build environment issues are detected and reported.

### Special cases

- Imports that only exist on MicroPython builds (`machine`, `micropython`, `rp2`, `ucollections`, `ujson`, `uselect`, `ustruct`, `utime`, `neopixel`) are skipped automatically.
- Every `uv add` command is capped by `--install-timeout` (default 300s). When the timeout is reached, deptry-auto stops that attempt, switches to the next candidate name, and ultimately reports the packages that still need manual attention.
- If one installation fails, `deptry-auto` now continues with the remaining packages, tries fallback names where applicable, and reports any failures at the end.

`deptry` exits with code `1` when it finds issues, so `deptry-auto` tolerates both `0` (clean) and `1` (issues) but still halts for any other failure. Use `--dry-run` when you only need a report of the missing dependencies.

## Bootstrap Guide

When packages like `scikit-image` or `opencv-python` don't have pre-built wheels for your platform or Python version, they need to be compiled from source. This requires build tools.

### Automatic Build Environment Detection

`deptry-auto` can automatically detect your build environment and set it up:

```bash
# Auto-detect build environment and install missing tools, then scan and install dependencies
deptry-auto --auto-bootstrap

# Or with longer timeout for compilation
deptry-auto --auto-bootstrap --install-timeout 600
```

This will:

1. Check for MSVC, CMake, Ninja, Meson, and NumPy
2. Automatically install pip-based tools (CMake, Ninja, Meson, NumPy) if missing
3. Detect Visual Studio installations and provide activation instructions
4. Proceed to scan and install missing dependencies

### Build System Preferences

By default, `deptry-auto` will use the first available build system it finds. You can specify a preference:

```bash
# Prefer Ninja as the build system
deptry-auto --build-pref ninja

# Prefer CMake with auto-bootstrap
deptry-auto --build-pref cmake --auto-bootstrap

# Strategy: tries local tools first, falls back to alternatives if preferred unavailable
deptry-auto --build-pref msvc  # Will use MSVC if available, otherwise tries other tools
```

Available build systems: `msvc`, `cmake`, `ninja`, `meson`

The resolution strategy:

1. **Find available local tools** on your system
2. **If preferred is specified**, use it if available
3. **Otherwise use first available** tool (fails gracefully if none available)
4. **With `--auto-bootstrap`**, missing tools are installed automatically

If auto-bootstrap doesn't work or you prefer manual control:

This installs:

- **CMake** - Build system generator
- **Ninja** - Fast C++ build system
- **Meson** - Modern build system (used by scikit-image, etc.)
- **NumPy** - Dependency for scientific packages

### What You Still Need Manually

**Microsoft Visual C++ Compiler (MSVC)** - Cannot be installed via pip, requires:

1. Download [Visual Studio Community Edition](https://visualstudio.microsoft.com/downloads/) (free)
2. Run installer and select "Desktop development with C++"
3. After installation, activate MSVC in your terminal:

   ```powershell
   "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
   ```

### Alternative: Use Older Python

If you're on Python 3.14, consider using Python 3.13 or 3.12 which have more pre-built wheels:

```bash
# Check what's available for your Python version
uv run python tests/test_imports.py
```

### Alternative: Use Conda

Conda has pre-built wheels for scientific packages:

```bash
conda install scikit-image opencv-python easyocr
```

## Testing

Verify your setup works:

```bash
# Test imports and build environment
uv run python tests/test_imports.py
```

This checks:

- Python version and platform
- Available build tools (MSVC, CMake, Ninja)
- Installed packages
- Specifically tests scikit-image

See `tests/README.md` for more details.

## Development

```bash
uv sync --group dev            # install dependencies
uv run pre-commit install      # install pre-commit hooks
uv run deptry-auto --dry-run .
```

The local pre-commit hook automatically bumps the patch version whenever a file under `src/` is staged. The first
commit attempt will therefore fail after the hook updates `pyproject.toml`; stage the modified file and re-run `git commit`.

Run the CLI against a throwaway project if you want to observe `uv add` in action.
