"""Zero-trust deployment doctor for the Dual-Plane AI Gateway.

Interface: python3 scripts/gateway_doctor.py [--json]
  Runs gateway.core.diagnostics.run_zero_trust_diagnostics(), prints either
  a human-readable table or indented JSON to stdout, shuts the lifecycle
  manager down so no child MCP process survives, and exits 0 when the report
  status is "ok", else 1.
Dependencies: gateway.core.config, gateway.core.diagnostics, gateway.core.lifecycle,
gateway.core.logging.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gateway.core.config import settings  # noqa: E402
from gateway.core.diagnostics import STATUS_OK, run_zero_trust_diagnostics  # noqa: E402
from gateway.core.lifecycle import lifecycle_manager  # noqa: E402
from gateway.core.logging import configure_stderr_logging  # noqa: E402

EXIT_OK = 0
EXIT_DEGRADED = 1
LABEL_WIDTH = 30
RULE_WIDTH = 64
RULE_CHAR = "-"


async def _collect() -> dict[str, Any]:
    try:
        return await run_zero_trust_diagnostics()
    finally:
        await lifecycle_manager.execute_shutdown()


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_table(report: dict[str, Any]) -> str:
    rule = RULE_CHAR * RULE_WIDTH
    lines = [rule, f" GATEWAY DOCTOR  overall status: {report['status'].upper()}", rule]
    for section, body in report.items():
        if not isinstance(body, dict):
            continue
        lines.append(f" [{section}]  status={body.get('status', '')}")
        for key, value in body.items():
            if key == "status":
                continue
            lines.append(f"   {key:<{LABEL_WIDTH}} {_format_value(value)}")
    lines.append(rule)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run zero-trust deployment diagnostics.")
    parser.add_argument("--json", action="store_true", help="emit the report as indented JSON")
    args = parser.parse_args(argv)

    configure_stderr_logging(level=settings.log_level, logger_name="gateway")
    report = asyncio.run(_collect())

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render_table(report) + "\n")
    sys.stdout.flush()
    return EXIT_OK if report["status"] == STATUS_OK else EXIT_DEGRADED


if __name__ == "__main__":
    raise SystemExit(main())
