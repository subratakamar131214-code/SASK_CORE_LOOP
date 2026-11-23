#!/usr/bin/env bash
# CI-only helper: commit the sanitized, allowlisted runtime state.
# Raw inboxes, caches, locks, build and dist outputs are gitignored.
set -euo pipefail

label="${1:-state}"
repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

python_bin="${PYTHON:-python}"

git config user.name >/dev/null 2>&1 || git config user.name "sask-core-loop[bot]"
git config user.email >/dev/null 2>&1 || git config user.email "sask-core-loop@users.noreply.github.com"

paths=()
for candidate in state swarm/children; do
  if [ -d "$candidate" ]; then
    paths+=("$candidate")
  fi
done

if [ "${#paths[@]}" -eq 0 ]; then
  echo "no state paths to commit"
  exit 0
fi

git add -A -- "${paths[@]}"

unexpected="$(git diff --cached --name-only | grep -vE '^(state/|swarm/children/)' || true)"
if [ -n "$unexpected" ]; then
  echo "refusing to commit non-state paths:" >&2
  echo "$unexpected" >&2
  exit 1
fi

if git diff --cached --quiet; then
  echo "state unchanged"
  exit 0
fi

"$python_bin" -m src.compliance.secretscan --tree state
if [ -d swarm/children ]; then
  "$python_bin" -m src.compliance.secretscan --tree swarm/children
fi

git commit -m "chore(state): ${label} run ${GITHUB_RUN_ID:-local}"

branch="${GITHUB_REF_NAME:-main}"
git pull --rebase --autostash origin "$branch"
git push origin "HEAD:$branch"
