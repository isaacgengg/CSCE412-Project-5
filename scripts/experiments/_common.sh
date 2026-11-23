# Shared helpers for the experiment scripts. Each script prepares ONE commit on its own branch
# and prints the commands to publish it. Nothing here pushes to main.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

start_branch() {
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then echo "uncommitted changes to tracked files; commit or stash first" >&2; exit 1; fi
  git fetch -q origin
  git checkout -q -B "$1" origin/main
}

finish() {  # $1 branch, $2 title, $3 what to expect
  local branch=$1 title=$2 expect=$3
  python -m cicd.release build >/dev/null
  echo "== local pre-deploy gate (what CodeBuild's Test stage will run) =="
  python -m cicd.release test predeploy | tail -n 3 || true
  cat <<MSG

Prepared commit on branch $branch:
  $(git log -1 --format='%h %s')

Publish it (merging to main triggers the pipeline):
  git push -u origin $branch
  gh pr create --base main --head $branch --title "$title" --body "Project 6 experiment"
  gh pr merge --merge --delete-branch

Expected: $expect

Then record evidence:
  AWS_PROFILE=csce412-agent python orchestrator.py release-status
  AWS_PROFILE=csce412-agent python orchestrator.py analyze-release
MSG
}
