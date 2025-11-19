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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, List, Set, Tuple

MISSING_DEP_CODE = "DEP001"
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

    if local_packages:
        print(
            "Ignoring locally provided packages: "
            + ", ".join(sorted(local_packages))
        )

    if not missing_packages:
        print("No missing dependencies found.")
        return

    print(f"Found missing dependencies: {', '.join(sorted(missing_packages))}")
    for package in sorted(missing_packages):
        if args.dry_run:
            print(f"[dry-run] uv add {package}")
            continue
        _add_dependency(package, project_root)

    if args.dry_run:
        print("Dry run complete. No dependencies were modified.")
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


def _add_dependency(package: str, project_root: Path) -> None:
    print(f"Adding dependency '{package}' via uv...")
    try:
        subprocess.run(["uv", "add", package], cwd=project_root, check=True)
    except subprocess.CalledProcessError as error:
        print(f"Failed to add '{package}': {error}")
        sys.exit(error.returncode)


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
    return _normalized_key(package) in identifiers


def _collect_local_identifiers(project_root: Path) -> Set[str]:
    identifiers: Set[str] = set()
    for path in _iter_python_files(project_root):
        stem = path.stem
        if stem != "__init__":
            identifiers.add(_normalized_key(stem))
        parent = path.parent
        if (parent / "__init__.py").exists():
            identifiers.add(_normalized_key(parent.name))
        identifiers.update(_extract_class_names(path))
    return identifiers


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
            names.add(_normalized_key(node.name))
    return names


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
