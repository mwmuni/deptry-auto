"""Pre-commit helper that bumps the project version when src/ changes are staged."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib  # type: ignore

PYPROJECT = Path("pyproject.toml")
VERSION_LINE = re.compile(r'^(\s*version\s*=\s*")(.*?)(")\s*$', re.MULTILINE)


def _read_version_from_path(path: Path) -> str:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data["project"]["version"]


def _read_version_from_git(spec: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show", spec],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    data = tomllib.loads(result.stdout)
    return data["project"]["version"]


def _list_staged_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return []
    entries = []
    for raw in result.stdout.splitlines():
        normalized = raw.strip().replace("\\", "/")
        if normalized:
            entries.append(normalized)
    return entries


def _has_prefix(paths: Iterable[str], prefix: str) -> bool:
    return any(path.startswith(prefix) for path in paths)


def _increment_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Version {version} does not follow major.minor.patch format")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def bump_version() -> None:
    staged_files = _list_staged_files()
    if not staged_files:
        print("No staged files detected, skipping version bump")
        return

    has_src_changes = _has_prefix(staged_files, "src/")
    pyproject_staged = "pyproject.toml" in staged_files

    if not (has_src_changes or pyproject_staged):
        print("No src/ or pyproject.toml changes staged, skipping version bump")
        return

    head_version = _read_version_from_git("HEAD:pyproject.toml")
    current_version = _read_version_from_path(PYPROJECT)

    if head_version and current_version != head_version:
        if pyproject_staged:
            print("pyproject.toml staged with version change; assuming manual bump")
        else:
            print(
                "Version already bumped in working tree; stage pyproject.toml to include it"
            )
        return

    new_version = _increment_patch(current_version)

    updated = _update_version_line(new_version)
    PYPROJECT.write_text(updated, encoding="utf-8")

    try:
        subprocess.run(["git", "add", str(PYPROJECT)], check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Failed to stage pyproject.toml after bumping version") from exc

    print(f"Version bumped to {new_version}")


def _update_version_line(new_version: str) -> str:
    original = PYPROJECT.read_text(encoding="utf-8")
    replaced = VERSION_LINE.sub(lambda match: f"{match.group(1)}{new_version}{match.group(3)}", original, count=1)
    if replaced == original:
        raise RuntimeError("Failed to locate version line in pyproject.toml")
    return replaced


if __name__ == "__main__":
    bump_version()