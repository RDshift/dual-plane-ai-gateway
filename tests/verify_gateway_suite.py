"""Single-command verification gate: runs the pytest suite and writes JUnit + JSON summaries.

Interface: python3 tests/verify_gateway_suite.py [extra pytest args]. Exit code is the pytest exit code.
Dependencies: pytest, pyproject.toml [tool.pytest.ini_options].
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts" / "test_reports"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run_suite(extra_args: list[str] | None = None) -> int:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    junit_xml_path = ARTIFACTS_DIR / "latest_junit.xml"
    summary_json_path = ARTIFACTS_DIR / "latest_summary.json"

    args = [
        f"--rootdir={PROJECT_ROOT}",
        "-c",
        str(PROJECT_ROOT / "pyproject.toml"),
        "-v",
        "--tb=short",
        f"--junitxml={junit_xml_path}",
        str(PROJECT_ROOT / "tests"),
    ]
    if extra_args:
        args.extend(extra_args)

    exit_code = int(pytest.main(args))
    summary_payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "exit_code": exit_code,
        "status": "PASSED" if exit_code == 0 else "FAILED",
        "junit_report": str(junit_xml_path.relative_to(PROJECT_ROOT)),
    }
    summary_json_path.write_text(
        json.dumps(summary_payload, indent=2) + "\n", encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_suite(sys.argv[1:]))
