# Main module for deptry-auto
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import xmlrpc.client
import urllib.request
import ssl
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Set, Tuple
from difflib import SequenceMatcher

try:
    from .bootstrap import bootstrap_build_environment, activate_msvc_environment
except ImportError:
    bootstrap_build_environment = None  # type: ignore
    activate_msvc_environment = None  # type: ignore

MISSING_DEP_CODE = "DEP001"
PYPI_RPC_URL = "https://pypi.org/pypi"
MAPPING_URL = "https://raw.githubusercontent.com/bndr/pipreqs/master/pipreqs/mapping"
_PLATFORM_CONSTRAINTS: Set[str] = set()  # Global to collect platform constraints
_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}
_PACKAGE_NAME_OVERRIDES: Dict[str, str] = {
    "pil": "Pillow",
    "cv2": "opencv-python",
    "serial": "pyserial",
    "paho": "paho-mqtt",
    "skimage": "scikit-image",
}
_SKIPPED_PACKAGES = {
    "machine",
    "micropython",
    "rp2",
    "ucollections",
    "ujson",
    "uselect",
    "ustruct",
    "utime",
    "neopixel",
}

_REQUIRES_PYTHON_REGEX = re.compile(r'^(\s*requires-python\s*=\s*")(.*?)(")\s*$', re.MULTILINE)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a project with deptry and add missing dependencies using uv."
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=".",
        help="Path to the project root (defaults to current directory).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the dependencies that would be added.",
    )
    parser.add_argument(
        "--install-timeout",
        type=int,
        default=300,
        help=(
            "Maximum seconds to wait for a single 'uv add' command before moving "
            "on (use 0 to disable)."
        ),
    )
    parser.add_argument(
        "--auto-bootstrap",
        action="store_true",
        help="Automatically set up build environment with pip tools before resolving dependencies.",
    )
    parser.add_argument(
        "--build-pref",
        type=str,
        default=None,
        choices=["msvc", "ninja", "cmake", "meson"],
        help="Preferred build system (defaults to auto-detect local tools, then try alternatives).",
    )
    return parser.parse_args(argv)


def _check_build_tool_available(tool: str) -> bool:
    """Check if a specific build tool is available on the system."""
    tool_commands: Dict[str, List[str]] = {
        "msvc": ["cl.exe", "/?"],
        "cmake": ["cmake", "--version"],
        "ninja": ["ninja", "--version"],
        "meson": [sys.executable, "-m", "meson", "--version"],
    }
    
    if tool not in tool_commands:
        return False
    
    try:
        result = subprocess.run(
            tool_commands[tool],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def _find_available_build_tools() -> List[str]:
    """Find all available build tools on the system."""
    available = []
    for tool in ["msvc", "cmake", "ninja", "meson"]:
        if _check_build_tool_available(tool):
            available.append(tool)
    return available


def _resolve_build_system(preferred: str | None = None) -> str | None:
    """
    Resolve which build system to use.
    
    Strategy:
    1. If preferred is MSVC, attempt to activate it (even if not in PATH)
    2. If preferred is specified and available, use it
    3. Otherwise, find first available local tool
    4. If no local tools, return None (caller can install via bootstrap)
    
    Args:
        preferred: Preferred build system ('msvc', 'ninja', 'cmake', 'meson')
    
    Returns:
        The resolved build system name, or None if none available
    """
    # Special handling for MSVC: attempt activation even if not in PATH
    if preferred == "msvc":
        print("[build-sys] Attempting to activate MSVC environment...")
        if activate_msvc_environment is not None:
            try:
                if activate_msvc_environment():
                    print("[build-sys] [OK] MSVC environment activated and ready for use")
                    return "msvc"
                else:
                    print("[build-sys] [WARN] Could not activate MSVC, trying alternatives...")
            except Exception as e:
                print(f"[build-sys] [WARN] Error activating MSVC: {e}")
    
    available = _find_available_build_tools()
    
    if not available:
        return None
    
    selected = None
    if preferred and preferred in available:
        print(f"[build-sys] Using preferred build system: {preferred}")
        selected = preferred
    elif preferred and preferred not in available:
        print(f"[build-sys] Preferred {preferred} not available, using: {available[0]}")
        selected = available[0]
    else:
        selected = available[0]
    
    return selected


def _check_build_requirements() -> Dict[str, bool]:
    """Check if build tools are available on the system."""
    requirements: Dict[str, bool] = {}
    
    for tool in ["msvc", "cmake", "ninja", "meson"]:
        requirements[tool] = _check_build_tool_available(tool)
    
    # Check for NumPy (needed for many packages)
    try:
        __import__("numpy")
        requirements["numpy"] = True
    except ImportError:
        requirements["numpy"] = False
    
    return requirements


def _report_build_requirements(project_root: Path) -> None:
    """Check and report on build requirements for source builds."""
    print("\n[build-env] Checking build environment for source compilation...")
    requirements = _check_build_requirements()
    
    missing = [k for k, v in requirements.items() if not v]
    
    if not missing:
        print("[build-env] [OK] All build tools are available")
        return
    
    print(f"[build-env] [FAIL] Missing build tools: {', '.join(missing)}")
    print("\n[build-env] To build packages from source on Windows, you need:")
    print("[build-env] 1. Microsoft Visual C++ Compiler (MSVC)")
    print("[build-env]    - Download: https://visualstudio.microsoft.com/downloads/")
    print("[build-env]    - Select 'Desktop development with C++'")
    print("[build-env] 2. CMake")
    print("[build-env]    - Install via: pip install cmake")
    print("[build-env] 3. Ninja build system")
    print("[build-env]    - Install via: pip install ninja")
    print("[build-env] 4. Meson build system")
    print("[build-env]    - Install via: pip install meson")
    print("\n[build-env] Alternatively, use pre-built wheels by:")
    print("[build-env] - Using an older Python version (3.12 or 3.13)")
    print("[build-env] - Waiting for wheels to be built for Python 3.14")
    print("[build-env] - Using conda which has pre-built packages")


def _detect_current_platform() -> str | None:
    """Detect the current platform in uv constraint format."""
    system = sys.platform
    
    if system == "win32":
        # Windows: win32
        return "sys_platform == 'win32'"
    elif system == "darwin":
        # macOS: darwin
        return "sys_platform == 'darwin'"
    elif system == "linux":
        # Linux: linux
        return "sys_platform == 'linux'"
    else:
        return None


def _extract_platform_requirements_from_error(error_message: str) -> Set[str]:
    """Extract platform requirements mentioned in uv error messages."""
    requirements: Set[str] = set()
    
    # Look for patterns like: sys_platform == 'win32' and platform_machine == 'AMD64'
    pattern = r"['\"]?sys_platform == '[^']+\'(?:\s+and\s+platform_machine == '[^']+\')?['\"]?"
    matches = re.findall(pattern, error_message)
    for match in matches:
        requirements.add(match.strip('"\''))
    
    return requirements


def _update_pyproject_with_constraints(project_root: Path, constraints: Set[str]) -> bool:
    """Update pyproject.toml with required-environments constraints."""
    if not constraints:
        return False
    
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        print(f"[platforms] pyproject.toml not found at {project_root}")
        return False
    
    try:
        with open(pyproject_path, "rb") as f:
            content = tomllib.load(f)
    except Exception as e:
        print(f"[platforms] Could not read pyproject.toml: {e}")
        return False
    
    # Initialize uv tool config if not present
    if "tool" not in content:
        content["tool"] = {}
    if "uv" not in content["tool"]:
        content["tool"]["uv"] = {}
    
    # Get existing required-environments or create empty list
    existing = content["tool"]["uv"].get("required-environments", [])
    if isinstance(existing, str):
        existing = [existing]
    elif not isinstance(existing, list):
        existing = []
    
    # Add new constraints that aren't already present
    for constraint in constraints:
        if constraint not in existing:
            existing.append(constraint)
    
    content["tool"]["uv"]["required-environments"] = existing
    
    # Fallback: print what should be added (automatic write requires tomli_w which may not be available)
    print("[platforms] Recommended platform constraints to add to your pyproject.toml:")
    print("[platforms] Under [tool.uv] section, add:")
    for constraint in constraints:
        print(f'[platforms]   required-environments = ["{constraint}"]')
    return True


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
    
    # Resolve build system preference
    resolved_build_sys = _resolve_build_system(args.build_pref)
    if resolved_build_sys:
        print(f"[deptry-auto] Resolved build system: {resolved_build_sys}")
    
    # Handle auto-bootstrap: detect environment issues and set up if needed
    if args.auto_bootstrap:
        print("[deptry-auto] Auto-detecting build environment...")
        build_status = _check_build_requirements()
        missing_tools = [k for k, v in build_status.items() if not v]
        
        if missing_tools:
            print(f"[deptry-auto] Missing build tools: {', '.join(missing_tools)}")
            print("[deptry-auto] Attempting automatic setup...")
            
            if bootstrap_build_environment is None:
                print("[deptry-auto] Warning: bootstrap module not available")
            else:
                try:
                    # Pass preferred build system (exclude 'msvc' since bootstrap handles it separately)
                    preferred_for_bootstrap = args.build_pref if args.build_pref != "msvc" else None
                    bootstrap_build_environment(auto_install=True, preferred=preferred_for_bootstrap)
                    print("[deptry-auto] Build environment setup complete")
                    # Re-resolve after bootstrap
                    resolved_build_sys = _resolve_build_system(args.build_pref)
                except Exception as e:
                    print(f"[deptry-auto] Warning: Build environment setup had issues: {e}")
        else:
            print("[deptry-auto] Build environment ready")
    
    project_root = Path(args.project_root).resolve()

    if not project_root.exists():
        print(f"Project path '{project_root}' does not exist.")
        sys.exit(2)

    if not project_root.is_dir():
        print(f"Project path '{project_root}' is not a directory.")
        sys.exit(2)

    print(f"Scanning '{project_root}' for missing dependencies...")
    report = _run_deptry_scan(project_root)
    missing_packages = _extract_missing_packages(report)
    missing_packages, local_packages = _filter_local_packages(missing_packages, project_root)
    missing_packages, skipped_packages = _filter_skipped_packages(missing_packages)

    if local_packages:
        print(
            "Ignoring locally provided packages: "
            + ", ".join(sorted(local_packages))
        )

    if skipped_packages:
        print(
            "Skipping packages that cannot be installed automatically: "
            + ", ".join(sorted(skipped_packages))
        )

    if not missing_packages:
        print("No missing dependencies found.")
        return

    print(f"Found missing dependencies: {', '.join(sorted(missing_packages))}")
    install_timeout = args.install_timeout if args.install_timeout > 0 else None
    
    if args.dry_run:
        # Show what would be installed in dry-run mode
        for package in sorted(missing_packages):
            install_name = _candidate_install_names(package)[0]
            if install_name != package:
                print(
                    f"[dry-run] uv add {install_name}  # resolved from '{package}'"
                )
            else:
                print(f"[dry-run] uv add {package}")
        print("Dry run complete. No dependencies were modified.")
    else:
        # Resolve all candidates first, then install in one batch
        package_to_install: Dict[str, str] = {}
        for package in sorted(missing_packages):
            candidates = _candidate_install_names(package)
            package_to_install[package] = candidates[0]
        
        failures = _add_dependencies_batch(
            package_to_install, project_root, install_timeout
        )
        
        if failures:
            print(
                "Failed to add the following packages: "
                + ", ".join(failures)
            )
            # Check if build environment is needed
            _report_build_requirements(project_root)
            # Report any platform constraints that were discovered
            if _PLATFORM_CONSTRAINTS:
                print("\n[platforms] Platform constraints detected during installation:")
                _update_pyproject_with_constraints(project_root, _PLATFORM_CONSTRAINTS)
            sys.exit(1)
        else:
            print("Finished adding missing dependencies.")
            # Report any platform constraints that were discovered
            if _PLATFORM_CONSTRAINTS:
                print("\n[platforms] Platform constraints detected during installation:")
                _update_pyproject_with_constraints(project_root, _PLATFORM_CONSTRAINTS)


def _run_deptry_scan(project_root: Path) -> List[dict]:
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    output_file = Path(temp_file.name)
    temp_file.close()
    try:
        cmd = [
            sys.executable,
            "-m",
            "deptry",
            "--json-output",
            str(output_file),
            ".",
        ]
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        # deptry exits with code 1 when issues are found, so treat 0/1 as success.
        if result.returncode not in (0, 1):
            print("deptry failed to scan the project.")
            if result.stdout:
                print(result.stdout.strip())
            if result.stderr:
                print(result.stderr.strip())
            sys.exit(result.returncode)

        if not output_file.exists():
            print("deptry did not produce a JSON report.")
            sys.exit(1)

        with output_file.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        print("Error parsing deptry JSON output.")
        sys.exit(1)
    finally:
        output_file.unlink(missing_ok=True)


def _extract_missing_packages(report: Iterable[dict]) -> Set[str]:
    missing: Set[str] = set()
    for entry in report:
        error = entry.get("error", {})
        if error.get("code") != MISSING_DEP_CODE:
            continue
        module = entry.get("module")
        if not module:
            continue
        missing.add(module.split(".")[0])
    return missing


def _add_dependencies_batch(
    package_to_install: Dict[str, str], project_root: Path, timeout: int | None
) -> List[str]:
    """Install all packages with smart fallback to individual installs on failure."""
    install_names = list(package_to_install.values())
    print(f"\n[batch] Adding {len(install_names)} dependencies via uv: {', '.join(install_names)}")
    print(f"[batch] Running: uv add {' '.join(install_names)}")
    
    try:
        result = subprocess.run(
            ["uv", "add"] + install_names,
            cwd=project_root,
            text=True,
            timeout=timeout,
            env=os.environ,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Timed out after {timeout}s while installing dependencies."
        )
        print("Attempting individual package installations...")
        return _add_dependencies_individually(package_to_install, project_root, timeout)
    
    if result.returncode == 0:
        print(f"[batch] [OK] All {len(install_names)} packages installed successfully")
        return []
    
    print(f"[batch] [FAIL] Batch install failed (exit code {result.returncode})")
    # and retry with only the viable ones
    problematic_packages = _identify_problematic_packages_in_batch(
        result, package_to_install
    )
    
    if problematic_packages:
        # Retry batch with remaining packages
        viable_packages = {
            pkg: name for pkg, name in package_to_install.items()
            if pkg not in problematic_packages
        }
        
        if viable_packages:
            print(
                f"[batch] Identified unresolvable packages: {', '.join(problematic_packages)}"
            )
            print(f"[batch] Retrying batch with {len(viable_packages)} remaining packages...")
            failures = _add_dependencies_batch(viable_packages, project_root, timeout)
            # Add the skipped packages to the failures list
            failures.extend(problematic_packages)
            return failures
    
    # Couldn't identify specific problematic packages, try individually
    print("[batch] Could not identify specific problematic packages")
    print(f"[individual] Attempting to install {len(package_to_install)} packages individually...")
    return _add_dependencies_individually(package_to_install, project_root, timeout)


def _add_dependencies_individually(
    package_to_install: Dict[str, str], project_root: Path, timeout: int | None
) -> List[str]:
    """Install packages individually with candidate resolution."""
    failures: List[str] = []
    total = len(package_to_install)
    for idx, (package, install_name) in enumerate(package_to_install.items(), 1):
        print(f"\n[individual] [{idx}/{total}] Installing '{package}' as '{install_name}'...")
        if not _try_install_with_candidates(package, install_name, project_root, timeout):
            print(f"[individual] [{idx}/{total}] [FAIL] Failed to install '{package}'")
            failures.append(package)
        else:
            print(f"[individual] [{idx}/{total}] [OK] Successfully installed '{package}'")
    return failures


def _identify_problematic_packages_in_batch(
    result: subprocess.CompletedProcess[str], package_to_install: Dict[str, str]
) -> Set[str]:
    """Try to identify which packages in a batch caused the failure."""
    message = (result.stdout or "") + "\n" + (result.stderr or "")
    lowered = message.casefold()
    
    # Look for package names mentioned in error messages
    problematic: Set[str] = set()
    
    for package, install_name in package_to_install.items():
        # Check if this specific package name appears in an error
        if (install_name.casefold() in lowered or 
            package.casefold() in lowered or
            install_name.lower().replace("_", "-") in lowered or
            package.lower().replace("_", "-") in lowered):
            # Look for "not found" or "unsatisfiable" keywords near the package name
            if "not found" in lowered or "unsatisfiable" in lowered:
                problematic.add(package)
    
    return problematic


def _try_install_with_candidates(
    package: str, preferred_name: str, project_root: Path, timeout: int | None
) -> bool:
    """Try to install a package with candidate resolution."""
    candidates = _candidate_install_names(package)
    # Prefer the already-resolved name first
    if preferred_name in candidates:
        candidates.remove(preferred_name)
    candidates.insert(0, preferred_name)
    
    attempted: List[str] = []
    for candidate_idx, candidate in enumerate(candidates, 1):
        attempted.append(candidate)
        print(f"  [candidate {candidate_idx}/{len(candidates)}] Trying: uv add {candidate}")
        if _try_install_candidate(candidate, package, project_root, timeout):
            return True

    print(
        f"  [candidates] Exhausted all {len(attempted)} candidates for '{package}': "
        + ", ".join(attempted)
    )
    return False


def _try_install_candidate(
    candidate: str, original_package: str, project_root: Path, timeout: int | None
) -> bool:
    """Try to install a single candidate with fallback strategies."""
    
    # Strategy 0: Try to install pre-built wheel first (no build)
    # This prioritizes Conda/wheels and avoids slow/failed builds if possible.
    print(f"  [fast-path] Trying to install '{candidate}' using pre-built wheels...")
    if _try_install_command(["uv", "add", "--no-build-package", candidate, candidate], candidate, original_package, project_root, timeout):
        return True
    
    print("  [fast-path] Pre-built wheel not found or not compatible. Falling back to build...")

    # Strategy 1: Try downgrading Python version (prioritizing pre-built wheels)
    # If current python doesn't have a wheel, maybe an older one does.
    if _try_downgrading_python(candidate, project_root, timeout, no_build=True):
        return True

    # Try basic install (allowing build)
    if _try_install_command(["uv", "add", candidate], candidate, original_package, project_root, timeout):
        return True
    
    # If it failed, check what kind of failure
    try:
        result = _run_command_streaming(
            ["uv", "add", candidate],
            cwd=project_root,
            timeout=timeout,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        print(
            f"Timed out after {timeout}s while installing '{candidate}'."
            " Trying another candidate..."
        )
        return False
    
    # Check if it's an unresolvable dependency issue - skip this candidate
    if _looks_like_unresolvable_dependency(result):
        print(f"Cannot resolve dependencies for '{candidate}'.")
        print("Trying another candidate...")
        return False
    
    # Check if it's a build failure - skip this candidate and try next
    if _looks_like_build_failure(result):
        print(f"Build failure with '{candidate}' (compilation errors).")
        
        # Strategy 1: Try scientific-python nightly wheels (PREFERRED)
        # Many scientific packages publish nightly wheels that support newer Python versions
        print("  [robust-build] Trying scientific-python nightly wheels...")
        nightly_index = "https://pypi.anaconda.org/scientific-python-nightly-wheels/simple"
        # We use --index to ensure the source is persisted in pyproject.toml
        if _try_install_command(["uv", "add", "--index", nightly_index, candidate], candidate, original_package, project_root, timeout):
            return True

        # Strategy 2: Try pre-releases (often have fixes for newer Python versions)
        print(f"  [robust-build] Nightly wheels failed. Trying standard pre-release version for '{candidate}'...")
        if _try_install_command(["uv", "add", "--prerelease=allow", candidate], candidate, original_package, project_root, timeout):
            return True

        # Strategy 3: Try no-build-isolation with updated build tools
        # This helps when the package's build-system requirements are too old for the current Python
        print("  [robust-build] Pre-release failed. Attempting build with local environment (no-build-isolation)...")
        
        # Install common build deps into current env to support the build
        print("  [robust-build] Installing modern build dependencies (Cython, NumPy, etc.)...")
        try:
            # We use 'uv pip install' to install into the current environment
            _run_command_streaming(
                ["uv", "pip", "install", "Cython>=3.0.0", "numpy>=2.0.0", "setuptools>=65.0.0", "wheel", "meson-python", "ninja"],
                cwd=project_root,
                env=dict(os.environ)
            )
        except Exception:
            pass 

        if _try_install_command(["uv", "add", "--no-build-isolation", candidate], candidate, original_package, project_root, timeout):
            return True

        # Strategy 4: Try downgrading Python version (allowing build)
        # If build fails on current python, maybe it works on older python
        if _try_downgrading_python(candidate, project_root, timeout, no_build=False):
            return True

        print("This package may not have pre-built wheels for your platform and source build failed.")
        print("Trying another candidate...")
        return False
    
    # Check if it's a platform/wheel compatibility issue
    if _looks_like_platform_error(result):
        print(f"Platform compatibility issue with '{candidate}'.")
        # Extract and collect platform constraints from error message
        platform_reqs = _extract_platform_requirements_from_error(result.stdout or "" + "\n" + result.stderr or "")
        if platform_reqs:
            _PLATFORM_CONSTRAINTS.update(platform_reqs)
            print(f"[platforms] Extracted constraint: {', '.join(platform_reqs)}")
        print("Trying without pre-built wheels...")
        # Try with --no-build to allow building from source
        if _try_install_command(["uv", "add", "--no-build", candidate], candidate, original_package, project_root, timeout):
            return True
        print(f"Failed to build '{candidate}' from source.")

        # Try downgrading Python version
        if _try_downgrading_python(candidate, project_root, timeout):
            return True
    
    # Check if it's a missing distribution
    if not _looks_like_missing_distribution(result):
        print(f"uv add exited with {result.returncode} for '{candidate}'.")
        return False
    
    print(
        f"Package '{candidate}' was not found on PyPI."
        " Trying another candidate..."
    )
    return False


def _try_install_command(
    cmd: List[str], candidate: str, original_package: str, project_root: Path, timeout: int | None
) -> bool:
    """Execute an install command and return success status."""
    cmd_str = " ".join(cmd)
    try:
        print(f"    [exec] {cmd_str}")
        result = _run_command_streaming(
            cmd,
            cwd=project_root,
            timeout=timeout,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        print(
            f"    [timeout] Timed out after {timeout}s while installing '{candidate}'."
        )
        return False
    
    _relay_process_output(result)
    if result.returncode == 0:
        if candidate != original_package:
            print(f"    [success] Installed '{original_package}' using '{candidate}'")
        else:
            print(f"    [success] Installed '{candidate}'")
        return True
    
    return False



def _filter_local_packages(
    packages: Iterable[str], project_root: Path
) -> Tuple[Set[str], Set[str]]:
    missing: Set[str] = set()
    local: Set[str] = set()
    local_identifiers: Set[str] | None = None
    for package in packages:
        if _module_resides_in_project(package, project_root):
            local.add(package)
            continue
        if local_identifiers is None:
            local_identifiers = _collect_local_identifiers(project_root)
        if _looks_like_local_identifier(package, local_identifiers):
            local.add(package)
        else:
            missing.add(package)
    return missing, local


def _filter_skipped_packages(packages: Iterable[str]) -> Tuple[Set[str], Set[str]]:
    installable: Set[str] = set()
    skipped: Set[str] = set()
    for package in packages:
        if _is_skipped_package(package):
            skipped.add(package)
        else:
            installable.add(package)
    return installable, skipped


def _module_resides_in_project(package: str, project_root: Path) -> bool:
    normalized = _normalize_import_name(package)
    import_paths = _project_import_paths(project_root)
    with _temporary_sys_path(import_paths):
        try:
            spec = importlib.util.find_spec(normalized)
        except ModuleNotFoundError:
            return False

    if spec is None:
        return False

    origins: List[Path] = []
    if spec.origin and spec.origin not in {"built-in", "frozen"}:
        origins.append(Path(spec.origin))
    if spec.submodule_search_locations:
        origins.extend(Path(location) for location in spec.submodule_search_locations)

    return any(_is_within_project(origin, project_root) for origin in origins)


def _normalize_import_name(name: str) -> str:
    return name.replace("-", "_")


def _normalized_key(name: str) -> str:
    return _normalize_import_name(name).casefold()


def _looks_like_local_identifier(package: str, identifiers: Set[str]) -> bool:
    if not identifiers:
        return False
    package_key = _normalized_key(package)
    return package_key in identifiers or package_key.replace("_", "") in identifiers


def _collect_local_identifiers(project_root: Path) -> Set[str]:
    identifiers: Set[str] = set()
    for path in _iter_python_files(project_root):
        stem = path.stem
        if stem != "__init__":
            _add_identifier(identifiers, stem)
        parent = path.parent
        if _looks_like_package_dir(parent):
            _add_identifier(identifiers, parent.name)
        for class_name in _extract_class_names(path):
            _add_identifier(identifiers, class_name)
    return identifiers


@lru_cache(maxsize=None)
def _looks_like_package_dir(path: Path) -> bool:
    if (path / "__init__.py").exists():
        return True
    return any(child.is_file() for child in path.glob("*.py"))


def _iter_python_files(project_root: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [
            d
            for d in dirs
            if d not in _EXCLUDED_DIR_NAMES and not d.startswith(".")
        ]
        for file_name in files:
            if not file_name.endswith((".py", ".pyi")):
                continue
            yield Path(root) / file_name


def _extract_class_names(path: Path) -> Set[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(_camel_to_snake(node.name))
    return names


def _camel_to_snake(name: str) -> str:
    result = []
    for index, char in enumerate(name):
        if char.isupper() and index and (not name[index - 1].isupper()):
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _is_skipped_package(package: str) -> bool:
    return _normalized_key(package) in _SKIPPED_PACKAGES


def _add_identifier(store: Set[str], value: str) -> None:
    normalized = _normalized_key(value)
    store.add(normalized)
    store.add(normalized.replace("_", ""))


@lru_cache(maxsize=1)
def _fetch_online_mappings() -> Dict[str, str]:
    """Fetch a comprehensive module->package mapping from the web."""
    mappings = {}
    try:
        # Use a short timeout to not slow down the tool too much
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(MAPPING_URL, timeout=3, context=ctx) as response:
            content = response.read().decode("utf-8")
            for line in content.splitlines():
                if ":" in line:
                    parts = line.strip().split(":")
                    if len(parts) >= 2:
                        # pipreqs mapping format is module:package
                        mappings[_normalized_key(parts[0])] = parts[1]
    except Exception:
        # Silently fail or print debug info if needed, but don't crash
        pass
    return mappings


def _candidate_install_names(package: str) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()

    def add(candidate: str) -> None:
        if not candidate:
            return
        key = candidate.casefold()
        if key in seen:
            return
        seen.add(key)
        names.append(candidate)

    override = _PACKAGE_NAME_OVERRIDES.get(_normalized_key(package))
    if override:
        add(override)
    
    # Try online mappings
    online_mappings = _fetch_online_mappings()
    online_match = online_mappings.get(_normalized_key(package))
    if online_match:
        add(online_match)

    add(package)
    add(_normalize_import_name(package))
    add(package.replace("_", "-"))
    add(package.replace("-", "_"))
    add(package.lower())

    search_terms = {
        package,
        _normalize_import_name(package),
        package.replace("_", " "),
        _camel_to_snake(package),
    }
    for term in search_terms:
        for hit in _search_pypi_candidates(term):
            add(hit)

    return names


@lru_cache(maxsize=128)
def _search_pypi_candidates(term: str) -> Tuple[str, ...]:
    normalized = term.strip()
    if not normalized:
        return ()
    try:
        with xmlrpc.client.ServerProxy(PYPI_RPC_URL) as client:
            hits = client.search({"name": normalized}, "or")
    except OSError:
        return ()
    except xmlrpc.client.Error:
        return ()

    def score(item: dict) -> Tuple[float, int]:
        name = (item.get("name") or "").casefold()
        summary = (item.get("summary") or "").casefold()
        similarity = SequenceMatcher(None, normalized.casefold(), name).ratio()
        if normalized.casefold() in name:
            similarity += 0.5
        if normalized.casefold() in summary:
            similarity += 0.25
        ordering = int(item.get("_pypi_ordering", 0))
        return similarity, ordering

    sorted_hits = sorted(hits, key=score, reverse=True)
    candidates: List[str] = []
    for item in sorted_hits:
        name = item.get("name")
        if not name:
            continue
        candidates.append(name)
        if len(candidates) >= 5:
            break
    return tuple(candidates)


def _looks_like_missing_distribution(result: subprocess.CompletedProcess[str]) -> bool:
    message = (result.stdout or "") + "\n" + (result.stderr or "")
    lowered = message.casefold()
    keywords = [
        "not found in the package registry",
        "no matching distribution found",
        "could not find a version",
        "unsatisfiable",
    ]
    return any(keyword in lowered for keyword in keywords)


def _looks_like_platform_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Check if the error is due to platform/wheel compatibility issues."""
    message = (result.stdout or "") + "\n" + (result.stderr or "")
    lowered = message.casefold()
    keywords = [
        "doesn't have a source distribution or wheel for the current platform",
        "no wheels found",
        "no matching distribution found for your python version",
        "only has wheels for the following platforms",
    ]
    return any(keyword in lowered for keyword in keywords)


def _looks_like_build_failure(result: subprocess.CompletedProcess[str]) -> bool:
    """Check if the error is due to a build failure (compilation errors, etc.)."""
    message = (result.stdout or "") + "\n" + (result.stderr or "")
    lowered = message.casefold()
    keywords = [
        "error c",  # C++ compiler errors
        "fatal error",
        "compilation failed",
        "build stopped",
        "ninja: build stopped",
        "meson compilation",
        "error during compilation",
    ]
    return any(keyword in lowered for keyword in keywords)


def _looks_like_unresolvable_dependency(result: subprocess.CompletedProcess[str]) -> bool:
    """Check if the error is due to unresolvable transitive dependencies."""
    message = (result.stdout or "") + "\n" + (result.stderr or "")
    lowered = message.casefold()
    keywords = [
        "no solution found when resolving dependencies",
        "unsatisfiable requirements",
        "because",  # uv uses "because" in dependency resolution explanations
        "can't be installed because",
        "doesn't have a source distribution or wheel for the current platform",
    ]
    return any(keyword in lowered for keyword in keywords)


def _relay_process_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)


def _relay_timeout_output(error: subprocess.TimeoutExpired) -> None:
    if error.output:
        print(str(error.output).rstrip())
    if error.stderr:
        print(str(error.stderr).rstrip(), file=sys.stderr)


def _project_import_paths(project_root: Path) -> List[Path]:
    paths = [project_root]
    src_dir = project_root / "src"
    if src_dir.is_dir():
        paths.append(src_dir)
    return paths


@contextmanager
def _temporary_sys_path(paths: Iterable[Path]) -> Iterator[None]:
    inserted: List[str] = []
    try:
        for path in paths:
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
                inserted.append(path_str)
        yield
    finally:
        for path_str in inserted:
            if path_str in sys.path:
                sys.path.remove(path_str)


def _is_within_project(path: Path, project_root: Path) -> bool:
    try:
        path.resolve().relative_to(project_root.resolve())
        return True
    except ValueError:
        return False


def _run_command_streaming(
    cmd: List[str],
    cwd: Path,
    timeout: int | None = None,
    env: Dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command while streaming output to stdout, capturing it for the result."""
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
    )
    
    output_lines = []
    try:
        if process.stdout:
            for line in process.stdout:
                print(line, end="")
                output_lines.append(line)
        
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise
    
    stdout_content = "".join(output_lines)
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode,
        stdout=stdout_content,
        stderr="" # stderr is merged into stdout
    )


def _get_current_python_version(project_root: Path) -> str | None:
    try:
        with open(project_root / ".python-version", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _update_requires_python(project_root: Path, version: str) -> str | None:
    """Update requires-python in pyproject.toml to >=version. Returns original content if changed."""
    pyproject_path = project_root / "pyproject.toml"
    try:
        content = pyproject_path.read_text(encoding="utf-8")
        
        # Check current requirement
        match = _REQUIRES_PYTHON_REGEX.search(content)
        if not match:
            return None
            
        current_req = match.group(2)
        # Simple heuristic: if we are downgrading, we likely need to relax the constraint
        # e.g. if it was >=3.14 and we want 3.12, we should make it >=3.12
        
        new_req = f">={version}"
        if current_req == new_req:
            return None
            
        new_content = _REQUIRES_PYTHON_REGEX.sub(lambda m: f'{m.group(1)}{new_req}{m.group(3)}', content)
        pyproject_path.write_text(new_content, encoding="utf-8")
        return content
    except Exception as e:
        print(f"  [warn] Failed to update requires-python: {e}")
        return None


def _restore_pyproject(project_root: Path, content: str) -> None:
    (project_root / "pyproject.toml").write_text(content, encoding="utf-8")


def _try_downgrading_python(candidate: str, project_root: Path, timeout: int | None, no_build: bool = False) -> bool:
    print(f"  [python-version] Checking if '{candidate}' works with older Python versions...")
    
    current_python = _get_current_python_version(project_root)
    
    # Versions to try (descending order of preference)
    # Assuming we are on a newer version (e.g. 3.14), try older stable ones
    fallback_versions = ["3.12", "3.11", "3.10"]
    
    for version in fallback_versions:
        if current_python and current_python.startswith(version):
            continue
            
        print(f"  [python-version] Pinning Python {version} and retrying install...")
        
        # 1. Pin version
        subprocess.run(
            ["uv", "python", "pin", version],
            cwd=project_root,
            check=False,
            capture_output=True
        )
        
        # 2. Update pyproject.toml requires-python if needed
        original_pyproject = _update_requires_python(project_root, version)
        
        # 3. Try install
        if no_build:
            # Attempt 1: Strict no-build (forces wheels for everything, including deps)
            # This helps resolve to older versions that have wheels (e.g. scikit-image)
            print(f"  [python-version] Trying strict wheel-only install for '{candidate}'...")
            if _try_install_command(["uv", "add", "--no-build", candidate], candidate, candidate, project_root, timeout):
                print(f"  [python-version] Successfully installed '{candidate}' (wheels only) with Python {version}")
                return True

            # Attempt 2: Relaxed no-build (only candidate must be wheel)
            # If strict failed (maybe some other dep needs build), try allowing deps to build
            print("  [python-version] Strict wheel install failed. Retrying with --no-build-package...")
            if _try_install_command(["uv", "add", "--no-build-package", candidate, candidate], candidate, candidate, project_root, timeout):
                print(f"  [python-version] Successfully installed '{candidate}' with Python {version}")
                return True
        else:
            # Standard install (allowing build)
            if _try_install_command(["uv", "add", candidate], candidate, candidate, project_root, timeout):
                print(f"  [python-version] Successfully installed '{candidate}' with Python {version}")
                return True
        
        # Revert changes if failed
        if original_pyproject:
            _restore_pyproject(project_root, original_pyproject)
            
    # Restore original python version if we failed all attempts
    if current_python:
        print(f"  [python-version] Failed to find compatible Python version. Restoring {current_python}...")
        subprocess.run(
            ["uv", "python", "pin", current_python],
            cwd=project_root,
            check=False,
            capture_output=True
        )
        
    return False
