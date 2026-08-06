#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${repo_root}/mvp/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Missing MVP virtual environment: ${python_bin}" >&2
  exit 2
fi

export HERMES_REPO="${HERMES_REPO:-${repo_root}/.vendor/hermes-agent}"
cd "${repo_root}"
exec "${python_bin}" -m workbench.validation.runner
