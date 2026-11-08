"""Single-command verification gate for the Dual-Plane AI Gateway.

Interface: python3 tests/verify_gateway_suite.py [extra pytest args]. Runs the
pytest suite, writes artifacts/test_reports/latest_junit.xml and
latest_summary.json, then prints a human-readable summary banner as the last
lines on stdout. Exit code is the pytest exit code.
Dependencies: pytest, pyproject.toml [tool.pytest.ini_options].
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import time
import xml.etree.ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts" / "test_reports"
JUNIT_NAME = "latest_junit.xml"
SUMMARY_NAME = "latest_summary.json"
BANNER_WIDTH = 72
BANNER_CHAR = "="
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402


class CountingPlugin:
    """In-process pytest plugin mirroring the terminal reporter tallies.

    Subtest reports (unittest subTest or pytest-subtests) carry a context
    attribute and are tallied separately so Total matches pytest's own count.
    """

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors = 0
        self.skipped = 0
        self.subtests_passed = 0
        self.subtests_failed = 0
        self.duration_s = 0.0

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        is_subtest = getattr(report, "context", None) is not None
        self.duration_s += float(getattr(report, "duration", 0.0) or 0.0)
        if is_subtest:
            if report.failed:
                self.subtests_failed += 1
            elif report.passed and report.when == "call":
                self.subtests_passed += 1
            return
        if report.when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
            elif report.skipped:
                self.skipped += 1
        elif report.failed:
            self.errors += 1
        elif report.skipped and report.when == "setup":
            self.skipped += 1

    def counts(self) -> dict[str, float | int]:
        total = self.passed + self.failed + self.errors + self.skipped
        return {
            "total": total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "subtests_passed": self.subtests_passed,
            "subtests_failed": self.subtests_failed,
            "duration_s": round(self.duration_s, 3),
        }


def parse_junit_counts(junit_xml_path: Path) -> dict[str, float | int]:
    """Fallback tally from the JUnit XML (JUnit counts subtests as cases)."""
    counts: dict[str, float | int] = {
        "total": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "passed": 0,
        "duration_s": 0.0,
    }
    if not junit_xml_path.is_file():
        return counts
    try:
        root = ET.parse(junit_xml_path).getroot()
    except ET.ParseError:
        return counts
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        counts["total"] += int(suite.get("tests", 0))
        counts["failed"] += int(suite.get("failures", 0))
        counts["errors"] += int(suite.get("errors", 0))
        counts["skipped"] += int(suite.get("skipped", 0))
        counts["duration_s"] += float(suite.get("time", 0.0))
    counts["passed"] = max(
        0, int(counts["total"]) - int(counts["failed"]) - int(counts["errors"]) - int(counts["skipped"])
    )
    counts["duration_s"] = round(float(counts["duration_s"]), 3)
    return counts


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def print_summary_banner(summary: dict[str, object]) -> None:
    """Print the executive summary as the final block on stdout."""
    counts = summary["counts"]
    assert isinstance(counts, dict)
    rule = BANNER_CHAR * BANNER_WIDTH
    lines = [
        "",
        rule,
        f" DUAL-PLANE AI GATEWAY VERIFICATION SUMMARY  [{summary['status']}]",
        rule,
        f" Status        : {summary['status']} (pytest exit code {summary['exit_code']})",
        f" Total tests   : {counts['total']}",
        f" Passed        : {counts['passed']}",
        f" Failed        : {counts['failed']}    Errors: {counts['errors']}    Skipped: {counts['skipped']}",
        f" Subtests      : {counts.get('subtests_passed', 0)} passed, {counts.get('subtests_failed', 0)} failed",
        f" Duration      : {counts['duration_s']:.3f}s (suite)  {summary['wall_clock_s']:.3f}s (wall clock)",
        f" Python        : {summary['python_version']} ({summary['python_implementation']})",
        f" Platform      : {summary['platform']}",
        f" Timestamp UTC : {summary['timestamp_utc']}",
        f" JUnit report  : {summary['junit_report']}",
        f" JSON summary  : {summary['summary_report']}",
        rule,
        "",
    ]
    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


def run_suite(extra_args: list[str] | None = None) -> int:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    junit_xml_path = ARTIFACTS_DIR / JUNIT_NAME
    summary_json_path = ARTIFACTS_DIR / SUMMARY_NAME

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

    counter = CountingPlugin()
    started = time.monotonic()
    exit_code = int(pytest.main(args, plugins=[counter]))
    wall_clock_s = round(time.monotonic() - started, 3)
    counts = counter.counts()
    if counts["total"] == 0:
        counts = parse_junit_counts(junit_xml_path)

    summary: dict[str, object] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "exit_code": exit_code,
        "status": STATUS_PASSED if exit_code == 0 else STATUS_FAILED,
        "wall_clock_s": wall_clock_s,
        "counts": counts,
        "junit_report": _relative(junit_xml_path),
        "summary_report": _relative(summary_json_path),
    }
    summary_json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print_summary_banner(summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_suite(sys.argv[1:]))
