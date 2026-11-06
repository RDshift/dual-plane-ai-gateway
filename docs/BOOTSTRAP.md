## Bootstrap & Verification

**Project:** `dual-plane-ai-gateway` — requires Python >= 3.11.

### 1. Activate the environment (Zsh)

From the repository root:

```zsh
source ./scripts/start-venv.sh
```

The script must be sourced (not executed). It creates `.venv` if absent, activates it in the current shell, and installs `requirements.txt` and `requirements-dev.txt`. Re-sourcing is idempotent: installs are skipped unless the requirements files changed.

Override the minimum Python version if needed (default `3.11`):

```zsh
DUAL_MIN_PY=3.12 source ./scripts/start-venv.sh
```

### 2. Verify the environment

```zsh
python scripts/bootstrap_and_verify.py
```

This ensures `.venv` exists, installs dev requirements and the project in editable mode, then runs `tests/verify_gateway_suite.py`. Exit code `0` indicates a healthy environment.

> If the project nests the package under `gateway/`, use `python gateway/scripts/bootstrap_and_verify.py` instead.