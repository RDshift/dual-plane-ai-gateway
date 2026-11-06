#!/usr/bin/env zsh
# scripts/start-venv.sh  (project: dual-plane-ai-gateway)
#
# Purpose:    Create/activate the project virtualenv and sync dependencies.
# Interface:  MUST be sourced from zsh:  source ./scripts/start-venv.sh
# Depends on: python3 (>= DUAL_MIN_PY), requirements.txt, requirements-dev.txt
#
# Behavior:
#   - Resolves project root relative to this file (cwd-independent).
#   - Creates .venv if absent; activates it in the calling shell.
#   - Upgrades pip and installs requirements only when the requirements
#     fingerprint (sha256) differs from the last successful sync.
#   - Never calls exit/set -e in the caller's shell; returns non-zero on failure.

# ---- Configuration block (no magic numbers below this section) -------------
typeset -g DUAL_MIN_PY="${DUAL_MIN_PY:-3.11}"
typeset -g DUAL_VENV_DIRNAME="${DUAL_VENV_DIRNAME:-.venv}"
typeset -g DUAL_REQ_FILES=("requirements.txt" "requirements-dev.txt")
typeset -g DUAL_STAMP_NAME=".requirements.sha256"
# -----------------------------------------------------------------------------

# Capture script path at top level (%x is only reliable here).
typeset -g _DUAL_SCRIPT_PATH="${(%):-%x}"

# Enforce "sourced" invocation.
if [[ "${ZSH_EVAL_CONTEXT}" != *:file* ]]; then
  print -u2 "[start-venv] ERROR: this script must be sourced, not executed."
  print -u2 "[start-venv] Usage: source ./scripts/start-venv.sh"
  exit 1
fi

_dual_log() { print -u2 -- "[start-venv] $*"; }

_dual_hash_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    return 127
  fi
}

_dual_start_venv() {
  emulate -L zsh
  setopt local_options pipe_fail

  local root venv vpy stamp f fingerprint previous created=0
  root="${_DUAL_SCRIPT_PATH:A:h:h}"
  venv="${root}/${DUAL_VENV_DIRNAME}"
  vpy="${venv}/bin/python"
  stamp="${venv}/${DUAL_STAMP_NAME}"

  # Load-or-die: find a compatible python interpreter (>= DUAL_MIN_PY)
  local py_bin=""
  for candidate in "${root}/.toolchain/python/python/install/bin/python3" python3.12 python3.11 python3; do
    if [[ -x "${candidate}" ]] || command -v "${candidate}" >/dev/null 2>&1; then
      if "${candidate}" -c "import sys; v=tuple(map(int,'${DUAL_MIN_PY}'.split('.'))); sys.exit(0 if sys.version_info[:2] >= v else 1)" 2>/dev/null; then
        py_bin="${candidate}"
        break
      fi
    fi
  done

  if [[ -z "${py_bin}" ]]; then
    _dual_log "ERROR: Python >= ${DUAL_MIN_PY} required but not found (checked local .toolchain and PATH)."
    return 1
  fi

  for f in "${DUAL_REQ_FILES[@]}"; do
    if [[ ! -f "${root}/${f}" ]]; then
      _dual_log "ERROR: missing ${root}/${f}"; return 1
    fi
  done

  # Create venv if absent (or incomplete) using the discovered python binary.
  if [[ ! -x "${vpy}" ]]; then
    _dual_log "Creating virtual environment at ${venv} using ${py_bin}"
    if ! "${py_bin}" -m venv "${venv}"; then
      _dual_log "ERROR: venv creation failed (is the python3-venv package installed?)."
      [[ -n "${venv}" && "${venv:t}" == "${DUAL_VENV_DIRNAME}" && -d "${venv}" ]] && rm -rf -- "${venv}"
      return 1
    fi
    created=1
  fi

  # Activate in the caller's shell.
  if ! source "${venv}/bin/activate"; then
    _dual_log "ERROR: activation failed."; return 1
  fi

  # Guard: existing venv may predate the version floor.
  if ! "${vpy}" -c "import sys; v=tuple(map(int,'${DUAL_MIN_PY}'.split('.'))); sys.exit(0 if sys.version_info[:2] >= v else 1)"; then
    _dual_log "ERROR: existing ${venv} runs Python < ${DUAL_MIN_PY}; remove it and re-source."; return 1
  fi

  # State fingerprint: skip install when requirements are unchanged.
  fingerprint="$(cd "${root}" && cat "${DUAL_REQ_FILES[@]}" | _dual_hash_stdin)" || {
    _dual_log "ERROR: no sha256 tool (sha256sum/shasum) available."; return 1
  }
  previous=""
  [[ -f "${stamp}" ]] && previous="$(<"${stamp}")"

  if [[ "${created}" -eq 1 || "${fingerprint}" != "${previous}" ]]; then
    _dual_log "Syncing dependencies"
    "${vpy}" -m pip install --quiet --upgrade pip || { _dual_log "ERROR: pip upgrade failed."; return 1 }
    for f in "${DUAL_REQ_FILES[@]}"; do
      "${vpy}" -m pip install --quiet -r "${root}/${f}" || { _dual_log "ERROR: install failed for ${f}."; return 1 }
    done
    print -r -- "${fingerprint}" >| "${stamp}"
  else
    _dual_log "Dependencies up to date"
  fi

  _dual_log "Ready: $(python -V 2>&1) @ ${VIRTUAL_ENV}"
  return 0
}

_dual_start_venv
_dual_rc=$?

unfunction _dual_start_venv _dual_hash_stdin _dual_log 2>/dev/null
unset _DUAL_SCRIPT_PATH
return ${_dual_rc}