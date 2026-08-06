#!/usr/bin/env bash
set -euo pipefail

repo_url="https://github.com/NousResearch/hermes-agent.git"
workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
target="${HERMES_REPO:-${workspace_root}/.vendor/hermes-agent}"
revision="$(tr -d '[:space:]' < "${workspace_root}/mvp/hermes-revision.txt")"

if [[ -d "${target}/.git" ]]; then
  if [[ -n "$(git -C "${target}" status --porcelain)" ]]; then
    echo "Refusing to update dirty Hermes checkout: ${target}" >&2
    exit 1
  fi
else
  if [[ -e "${target}" ]]; then
    echo "Target exists but is not a Git checkout: ${target}" >&2
    exit 1
  fi
  mkdir -p "$(dirname "${target}")"
  git clone --filter=blob:none --no-checkout "${repo_url}" "${target}"
fi

git -C "${target}" fetch origin "${revision}"
git -C "${target}" checkout --detach "${revision}"
echo "${target}"
