#!/usr/bin/env bash
# Experiment 3: post-deploy failure from a deploy-configuration defect. .js files are uploaded as
# text/plain. Every pre-deploy test passes; the live Content-Type check fails, the Verify stage
# rolls back to the newest verified release, re-verifies it, and fails the execution.
source "$(dirname "$0")/_common.sh"
start_branch exp/3-postdeploy-fail
sed -i 's#"\.js": "application/javascript; charset=utf-8"#".js": "text/plain; charset=utf-8"#' cicd/content-types.json
grep -q '".js": "text/plain' cicd/content-types.json
git commit -qam "Experiment 3: serve JavaScript as text/plain (deploy configuration defect)"
finish exp/3-postdeploy-fail "Experiment 3: post-deploy failure and rollback" \
  "Test and Deploy succeed; Verify fails content_types_correct (js/main.js text/plain), rolls back to the previous verified release, re-verification passes, the execution shows Failed; history.json marks this release rolled_back. Afterwards fix main with: git revert <merge commit> on a branch, PR, merge (a new release)."
