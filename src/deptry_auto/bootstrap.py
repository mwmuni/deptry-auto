"""Bootstrap build environment setup for Windows."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_visual_studio_installation() -> Path | None:
    """
    Find an available Visual Studio installation.
    
    Returns the path to the VS installation, or None if not found.
    """
    # Common installation paths for Visual Studio (in preference order)
    vs_paths = [
        Path("C:/Program Files/Microsoft Visual Studio/2022/Community"),
        Path("C:/Program Files/Microsoft Visual Studio/2022/BuildTools"),
        Path("C:/Program Files/Microsoft Visual Studio/2022/Enterprise"),
        Path("C:/Program Files (x86)/Microsoft Visual Studio/2019/Community"),
        Path("C:/Program Files (x86)/Microsoft Visual Studio/2019/BuildTools"),
        Path("C:/Program Files (x86)/Microsoft Visual Studio/2019/Enterprise"),
    ]
    
    for vs_path in vs_paths:
        if vs_path.exists():
            return vs_path
    
    return None


def activate_msvc_environment() -> bool:
    """
    Detect and activate MSVC environment from Visual Studio installation.
    
    Returns True if MSVC was activated, False otherwise.
    """
    vs_path = _find_visual_studio_installation()
    
    if not vs_path:
        print("[bootstrap] [FAIL] No Visual Studio installation found")
        print("[bootstrap] To install: https://visualstudio.microsoft.com/downloads/")
        print("[bootstrap] Select 'Desktop development with C++'")
        return False
    
    print(f"[bootstrap] Found Visual Studio at: {vs_path}")
    
    vcvars_path = vs_path / "VC/Auxiliary/Build/vcvars64.bat"
    if not vcvars_path.exists():
        print(f"[bootstrap] [FAIL] vcvars64.bat not found at {vcvars_path}")
        return False
    
    print("[bootstrap] Attempting to activate MSVC environment...")
    
    try:
        # Run vcvars64.bat and capture the environment variables
        result = subprocess.run(
            f'"{vcvars_path}" && set',
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        if result.returncode != 0:
            print(f"[bootstrap] [FAIL] Failed to activate MSVC: {result.stderr}")
            return False
        
        # Parse the environment variables from vcvars output
        for line in result.stdout.split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value
        
        print("[bootstrap] [OK] MSVC environment activated")
        return True
        
    except subprocess.TimeoutExpired:
        print("[bootstrap] [FAIL] MSVC activation timed out")
        return False
    except Exception as e:
        print(f"[bootstrap] [FAIL] Error activating MSVC: {e}")
        return False


def setup_msvc_via_visual_studio_build_tools() -> bool:
    """
    Detect and setup MSVC from Visual Studio Build Tools or Visual Studio Community.
    
    Returns True if MSVC is available/set up, False otherwise.
    """
    print("\n[bootstrap] Checking for MSVC via Visual Studio installations...")
    
    vs_path = _find_visual_studio_installation()
    if vs_path:
        print(f"[bootstrap] Found Visual Studio at: {vs_path}")
        
        # Try to activate MSVC environment
        vcvars_path = vs_path / "VC/Auxiliary/Build/vcvars64.bat"
        if vcvars_path.exists():
            print(f"[bootstrap] Found vcvars64.bat at: {vcvars_path}")
            print("[bootstrap] Run this to set up MSVC environment:")
            print(f"[bootstrap]   {vcvars_path}")
            return True
    
    return False


def setup_cmake_via_pip() -> bool:
    """
    Install CMake via uv pip (portable, doesn't require system installation).
    
    Returns True if CMake is installed/available, False otherwise.
    """
    if shutil.which("cmake"):
        print("[bootstrap] [OK] CMake is already installed (found in PATH)")
        return True

    print("\n[bootstrap] Setting up CMake via uv pip...")
    
    try:
        result = subprocess.run(
            ["uv", "pip", "install", "--upgrade", "cmake"],
            timeout=300,
        )
        if result.returncode == 0:
            print("[bootstrap] [OK] CMake installed successfully")
            return True
        else:
            print("[bootstrap] [FAIL] Failed to install CMake")
            return False
    except Exception as e:
        print(f"[bootstrap] [FAIL] Error installing CMake: {e}")
        return False


def setup_ninja_via_pip() -> bool:
    """
    Install Ninja via uv pip (portable, doesn't require system installation).
    
    Returns True if Ninja is installed/available, False otherwise.
    """
    if shutil.which("ninja"):
        print("[bootstrap] [OK] Ninja is already installed (found in PATH)")
        return True

    print("\n[bootstrap] Setting up Ninja via uv pip...")
    
    try:
        result = subprocess.run(
            ["uv", "pip", "install", "--upgrade", "ninja"],
            timeout=300,
        )
        if result.returncode == 0:
            print("[bootstrap] [OK] Ninja installed successfully")
            return True
        else:
            print("[bootstrap] [FAIL] Failed to install Ninja")
            return False
    except Exception as e:
        print(f"[bootstrap] [FAIL] Error installing Ninja: {e}")
        return False


def setup_meson_via_pip() -> bool:
    """
    Install Meson via uv pip (portable, doesn't require system installation).
    
    Returns True if Meson is installed/available, False otherwise.
    """
    if shutil.which("meson"):
        print("[bootstrap] [OK] Meson is already installed (found in PATH)")
        return True

    print("\n[bootstrap] Setting up Meson via uv pip...")
    
    try:
        result = subprocess.run(
            ["uv", "pip", "install", "--upgrade", "meson"],
            timeout=300,
        )
        if result.returncode == 0:
            print("[bootstrap] [OK] Meson installed successfully")
            return True
        else:
            print("[bootstrap] [FAIL] Failed to install Meson")
            return False
    except Exception as e:
        print(f"[bootstrap] [FAIL] Error installing Meson: {e}")
        return False


def bootstrap_build_environment(auto_install: bool = False, preferred: str | None = None) -> dict[str, bool]:
    """
    Bootstrap the build environment for compiling packages from source.
    
    Args:
        auto_install: If True, automatically install tools via pip.
                     If False, only report what's needed.
        preferred: Preferred build system ('cmake', 'ninja', 'meson'). 
                  If specified, only tries to install that tool.
                  If installation fails, tries alternatives.
    
    Returns:
        Dictionary with status of each tool.
    """
    print("\n" + "=" * 70)
    print("Build Environment Bootstrap")
    print("=" * 70)
    
    status = {
        "msvc": False,
        "cmake": False,
        "ninja": False,
        "meson": False,
    }
    
    # Check MSVC
    msvc_found = setup_msvc_via_visual_studio_build_tools()
    if not msvc_found:
        print("\n[bootstrap] MSVC not found. To install:")
        print("[bootstrap] 1. Download Visual Studio Community or Build Tools:")
        print("[bootstrap]    https://visualstudio.microsoft.com/downloads/")
        print("[bootstrap] 2. Run installer and select 'Desktop development with C++'")
        print("[bootstrap] 3. After install, run vcvars64.bat to activate environment")
    else:
        status["msvc"] = True
    
    # Install Python-based tools (smart preference-based approach)
    if auto_install:
        print("\n[bootstrap] Auto-installing Python-based build tools...")
        
        # Define installation order: try preferred first, then fallback to alternatives
        tools_to_try = []
        if preferred == "cmake":
            tools_to_try = ["cmake", "ninja", "meson"]
        elif preferred == "ninja":
            tools_to_try = ["ninja", "cmake", "meson"]
        elif preferred == "meson":
            tools_to_try = ["meson", "cmake", "ninja"]
        else:
            # No preference: try cmake first (most common), then others
            tools_to_try = ["cmake", "ninja", "meson"]
        
        # Try to install tools in order until one succeeds
        installed_any = False
        for tool in tools_to_try:
            if tool == "cmake":
                if setup_cmake_via_pip():
                    status["cmake"] = True
                    installed_any = True
            elif tool == "ninja":
                if setup_ninja_via_pip():
                    status["ninja"] = True
                    installed_any = True
            elif tool == "meson":
                if setup_meson_via_pip():
                    status["meson"] = True
                    installed_any = True
        
        if not installed_any:
            print("\n[bootstrap] [WARN] Failed to install any Python-based build tools")
            print("[bootstrap] To install manually, run:")
            print("[bootstrap]   uv pip install cmake")
    else:
        print("\n[bootstrap] To install Python-based build tools, run:")
        if preferred:
            print(f"[bootstrap]   uv pip install {preferred}")
        else:
            print("[bootstrap]   uv pip install cmake")
    
    return status


def setup_numpy_for_compilation() -> bool:
    """
    Ensure NumPy is installed (required for building scientific packages).
    
    Returns True if NumPy is available/installed, False otherwise.
    """
    print("\n[bootstrap] Checking NumPy installation...")
    
    try:
        __import__("numpy")
        print("[bootstrap] [OK] NumPy is already installed")
        return True
    except ImportError:
        print("[bootstrap] NumPy not found, installing...")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "numpy"],
                capture_output=True,
                timeout=300,
            )
            if result.returncode == 0:
                print("[bootstrap] [OK] NumPy installed successfully")
                return True
            else:
                print(f"[bootstrap] [FAIL] Failed to install NumPy: {result.stderr.decode()}")
                return False
        except Exception as e:
            print(f"[bootstrap] [FAIL] Error installing NumPy: {e}")
            return False


def quick_bootstrap() -> bool:
    """
    Quick bootstrap: Install all pip-available tools automatically.
    
    This is the easiest path for users who don't have Visual Studio installed.
    
    Returns True if at least the pip tools are installed.
    """
    print("\n[bootstrap] Starting quick bootstrap (pip-based tools only)...")
    print("[bootstrap] Note: MSVC compiler still requires manual Visual Studio install")
    print("[bootstrap] But pip tools will allow building pure-Python packages")
    
    # Ensure NumPy first (dependency for many packages)
    setup_numpy_for_compilation()
    
    # Install pip-based tools
    setup_cmake_via_pip()
    setup_ninja_via_pip()
    setup_meson_via_pip()
    
    print("\n[bootstrap] Bootstrap complete!")
    print("[bootstrap] Try running: deptry-auto . --install-timeout 600")
    print("[bootstrap] (with longer timeout for compilation)")
    
    return True


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Bootstrap build environment for deptry-auto"
    )
    parser.add_argument(
        "--auto-install",
        action="store_true",
        help="Automatically install pip-based tools (cmake, ninja, meson)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick bootstrap: install all pip tools automatically",
    )
    
    args = parser.parse_args()
    
    if args.quick:
        quick_bootstrap()
    else:
        bootstrap_build_environment(auto_install=args.auto_install)
