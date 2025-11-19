# Main module for deptry-auto
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import xmlrpc.client
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Set, Tuple
from difflib import SequenceMatcher

MISSING_DEP_CODE = "DEP001"
PYPI_RPC_URL = "https://pypi.org/pypi"
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
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
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
            sys.exit(1)
        else:
            print("Finished adding missing dependencies.")


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
    print(f"Adding dependencies via uv: {', '.join(install_names)}")
    
    try:
        result = subprocess.run(
            ["uv", "add"] + install_names,
            cwd=project_root,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Timed out after {timeout}s while installing dependencies."
        )
        print("Attempting individual package installations...")
        return _add_dependencies_individually(package_to_install, project_root, timeout)
    
    if result.returncode == 0:
        return []
    
    # Batch failed; try to identify which packages are problematic
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
                f"Skipping packages with unresolvable dependencies: "
                f"{', '.join(problematic_packages)}"
            )
            print("Retrying batch installation with remaining packages...")
            failures = _add_dependencies_batch(viable_packages, project_root, timeout)
            # Add the skipped packages to the failures list
            failures.extend(problematic_packages)
            return failures
    
    # Couldn't identify specific problematic packages, try individually
    print("Batch installation failed. Attempting individual package installations...")
    return _add_dependencies_individually(package_to_install, project_root, timeout)


def _add_dependencies_individually(
    package_to_install: Dict[str, str], project_root: Path, timeout: int | None
) -> List[str]:
    """Install packages individually with candidate resolution."""
    failures: List[str] = []
    for package, install_name in package_to_install.items():
        if not _try_install_with_candidates(package, install_name, project_root, timeout):
            failures.append(package)
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
    for candidate in candidates:
        attempted.append(candidate)
        if _try_install_candidate(candidate, package, project_root, timeout):
            return True

    print(
        "Exhausted install candidates for '"
        + package
        + "': "
        + ", ".join(attempted)
    )
    return False


def _try_install_candidate(
    candidate: str, original_package: str, project_root: Path, timeout: int | None
) -> bool:
    """Try to install a single candidate with fallback strategies."""
    # Try basic install first
    if _try_install_command(["uv", "add", candidate], candidate, original_package, project_root, timeout):
        return True
    
    # If it failed, check what kind of failure
    try:
        result = subprocess.run(
            ["uv", "add", candidate],
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=timeout,
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
        print("This package may not have pre-built wheels for your platform.")
        print("Trying another candidate...")
        return False
    
    # Check if it's a platform/wheel compatibility issue
    if _looks_like_platform_error(result):
        print(f"Platform compatibility issue with '{candidate}'. Trying without pre-built wheels...")
        # Try with --no-build to allow building from source
        if _try_install_command(["uv", "add", "--no-build", candidate], candidate, original_package, project_root, timeout):
            return True
        print(f"Failed to build '{candidate}' from source.")
    
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
    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Timed out after {timeout}s while installing '{candidate}'."
        )
        return False
    
    _relay_process_output(result)
    if result.returncode == 0:
        if candidate != original_package:
            print(f"Installed '{original_package}' using PyPI project '{candidate}'.")
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
