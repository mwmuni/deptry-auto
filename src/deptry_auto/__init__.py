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
    failures: List[str] = []
    for package in sorted(missing_packages):
        if args.dry_run:
            install_name = _candidate_install_names(package)[0]
            if install_name != package:
                print(
                    f"[dry-run] uv add {install_name}  # resolved from '{package}'"
                )
            else:
                print(f"[dry-run] uv add {package}")
            continue
        if not _add_dependency(package, project_root):
            failures.append(package)

    if args.dry_run:
        print("Dry run complete. No dependencies were modified.")
    elif failures:
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


def _add_dependency(package: str, project_root: Path) -> bool:
    print(f"Adding dependency '{package}' via uv...")
    candidates = _candidate_install_names(package)
    attempted: List[str] = []
    for candidate in candidates:
        attempted.append(candidate)
        result = subprocess.run(
            ["uv", "add", candidate],
            cwd=project_root,
            text=True,
            capture_output=True,
        )
        _relay_process_output(result)
        if result.returncode == 0:
            if candidate != package:
                print(f"Installed '{package}' using PyPI project '{candidate}'.")
            return True
        if not _looks_like_missing_distribution(result):
            print(f"uv add exited with {result.returncode} for '{candidate}'.")
            return False
        print(
            f"Package '{candidate}' was not found on PyPI."
            " Trying another candidate..."
        )

    print(
        "Exhausted install candidates for '"
        + package
        + "': "
        + ", ".join(attempted)
    )
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

    add(package)
    override = _PACKAGE_NAME_OVERRIDES.get(_normalized_key(package))
    if override:
        add(override)
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

    def ordering(item: dict) -> int:
        return int(item.get("_pypi_ordering", 0))

    sorted_hits = sorted(hits, key=ordering, reverse=True)
    candidates: List[str] = []
    for item in sorted_hits:
        name = item.get("name")
        if not name:
            continue
        if normalized.casefold() not in name.casefold():
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


def _relay_process_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)


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
