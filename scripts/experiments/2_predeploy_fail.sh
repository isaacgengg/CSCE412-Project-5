#!/usr/bin/env bash
# Experiment 2: pre-deploy failure. A broken internal link fails the Test stage, so Deploy never runs.
source "$(dirname "$0")/_common.sh"
start_branch exp/2-predeploy-fail
sed -i 's#class="btn btn-primary" href="/projects.html"#class="btn btn-primary" href="/projets.html"#' site/index.html
grep -q 'href="/projets.html"' site/index.html
git commit -qam "Experiment 2: broken internal link on the home page"
finish exp/2-predeploy-fail "Experiment 2: pre-deploy failure" \
  "the local gate above already FAILS (test_every_internal_reference_resolves). In the pipeline the Test stage fails, Deploy and Verify never run, production and history.json are unchanged. Afterwards restore main with: git revert <commit> on a branch, PR, merge."
