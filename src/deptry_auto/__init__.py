from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Set

MISSING_DEP_CODE = "DEP001"


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
