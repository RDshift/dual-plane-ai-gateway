from pathlib import Path
import os
import subprocess
import sys
import venv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = PROJECT_ROOT / ".venv"


def get_venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def main() -> int:
    if not VENV_DIR.exists():
        print(f"[bootstrap] Creating virtual environment at {VENV_DIR}", file=sys.stderr)
        venv.create(VENV_DIR, with_pip=True)

    vpy = get_venv_python()
    subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run(
        [str(vpy), "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements-dev.txt")],
        check=True,
    )
    subprocess.run([str(vpy), "-m", "pip", "install", "-e", str(PROJECT_ROOT)], check=True)

    result = subprocess.run([str(vpy), str(PROJECT_ROOT / "tests" / "verify_gateway_suite.py")])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())