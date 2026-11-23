# Project 6 experiments

Three controlled experiments, run in order. Each script prepares **one commit on its own branch**
from `origin/main`, runs the same pre-deploy gate the pipeline runs, and prints the commands to
publish it. Nothing is pushed to `main` until you merge, and merging is what triggers the
pipeline. Run them from the repo root with the virtualenv active and `npm ci` done.

Wait for each pipeline execution to finish before starting the next one. The pipeline runs in
QUEUED mode, so overlapping merges would still run one at a time, but the evidence is easier to
read one experiment at a time.

| # | Script | Change | Expected pipeline result | Production afterwards |
|---|---|---|---|---|
| 1 | `1_success.sh` | Adds a sentence to the home page | Source, Test, Deploy, Verify all succeed | New release live; the footer shows its short SHA; history: `deployed` then `verified` |
| 2 | `2_predeploy_fail.sh` | Home-page button links to `/projets.html` | **Test fails** (`test_every_internal_reference_resolves`); Deploy and Verify never run | Unchanged; nothing uploaded; no history entry |
| 3 | `3_postdeploy_fail.sh` | `cicd/content-types.json` maps `.js` to `text/plain` | Test and Deploy succeed; **Verify fails** `content_types_correct`, rolls back to the previous verified release, re-verifies it, then fails the execution | Previous verified release live again; history: `deployed`, `failed`, `rolled_back` |

## Running one

```bash
bash scripts/experiments/1_success.sh        # prepares exp/1-success and runs the local gate
git push -u origin exp/1-success
gh pr create --base main --head exp/1-success --title "Experiment 1: successful release" --body "Project 6 experiment"
gh pr merge --merge --delete-branch           # triggers the pipeline
```

Watch it at
`https://us-east-2.console.aws.amazon.com/codesuite/codepipeline/pipelines/csce412-site-pipeline/view`,
or with `AWS_PROFILE=csce412-agent python orchestrator.py release-status`.

## After each experiment

```bash
AWS_PROFILE=csce412-agent python orchestrator.py release-status
AWS_PROFILE=csce412-agent python orchestrator.py analyze-release          # Diagnoser on the latest execution
curl -s https://csce412isaacgeng.me/release.json                         # which release is live
```

Note the execution ID (`release-status` lists it) for the log export.

## Cleaning up after 2 and 3

Experiments 2 and 3 leave a bad commit on `main`. Revert it with a normal PR, which starts a new,
passing release:

```bash
git checkout main && git pull
git checkout -b exp/revert-2
git revert --no-edit <commit of experiment 2>   # squash merge: one commit; add -m 1 for a merge commit
git push -u origin exp/revert-2 && gh pr create --fill && gh pr merge --merge --delete-branch
```

After experiment 3, the revert's release contains the correct content-type map. Do not upload
anything by hand. Recovery always goes through a release (the pipeline's rollback, or
`orchestrator.py rollback`).

## Optional: agent-driven rollback

To show the Recovery Coordinator (instead of the pipeline) restoring a release, run
`AWS_PROFILE=csce412-agent python orchestrator.py rollback` while a release is live that you want
to back out. It reads the history, asks for approval, switches to the newest *other* verified
release, hands off to the Verifier, and records `rolled_back` only if re-verification passes.

## Packaging the evidence

```bash
AWS_PROFILE=csce412-agent python scripts/export_logs.py \
  --runs runs/<id>-review-pipeline runs/<id>-generate-tests runs/<id>-analyze-release ... \
  --executions <exp1 id> <exp2 id> <exp3 id>
```

This writes `project6-logs.zip`: agent runs, every CodeBuild log, the JUnit/JSON results and
`history.json`, all redacted.
