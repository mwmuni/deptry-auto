import tomllib
import tomli_w
from pathlib import Path
import subprocess
import sys


def bump_version():
    # Check if there are staged changes to src/ directory
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True
        )
        staged_files = result.stdout.strip().split('\n')
        if not any(file.startswith('src/') for file in staged_files if file):
            print("No changes to src/ directory, skipping version bump")
            return
    except subprocess.CalledProcessError:
        print("Failed to check staged files, skipping version bump")
        return

    path = Path("pyproject.toml")
    with path.open("rb") as f:
        data = tomllib.load(f)

    version = data["project"]["version"]
    # Assume semver: major.minor.patch
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Version {version} does not follow major.minor.patch format")
    parts[-1] = str(int(parts[-1]) + 1)
    new_version = ".".join(parts)

    data["project"]["version"] = new_version

    with path.open("wb") as f:
        tomli_w.dump(data, f)

    print(f"Version bumped to {new_version}")


if __name__ == "__main__":
    bump_version()