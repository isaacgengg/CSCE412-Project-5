# CI/CD pipeline (Project 6)

A push to `main` releases the site automatically. The pipeline is deterministic and works with no
AI model available. The agents (see the README) review it, generate tests for it, diagnose its
failures and coordinate recovery.

## Flow

```
GitHub push to main
   │  (CodeConnections, CodePipeline V2 trigger, QUEUED)
   ▼
Source ──► Test (CodeBuild) ──► Deploy (CodeBuild) ──► Verify & Recover (CodeBuild)
            build release         upload releases/<sha>/      live checks on https://domain
            run pre-deploy gate   switch OriginPath           pass → history: verified
            JUnit + JSON results  wait Deployed, invalidate   fail → history: failed
            ✗ any failure stops   history: deployed                  rollback to newest verified
              the execution here                                     re-verify → rolled_back
                                                                     exit 1 (execution Failed)
```

| Stage | Buildspec | Input | What it does |
|---|---|---|---|
| Source | (none) | GitHub `isaacgengg/CSCE412-Project-5`, branch `main` | Emits `SourceArtifact`; namespace `SourceVariables` provides `CommitId` |
| Test | `cicd/buildspec-test.yml` | SourceArtifact | `python -m cicd.release build`, then `test predeploy` (unit + pre-deploy + generated tests). Output `BuildArtifact` = the built site |
| Deploy | `cicd/buildspec-deploy.yml` | SourceArtifact (code), BuildArtifact (site) | `python -m cicd.release deploy` |
| Verify | `cicd/buildspec-verify.yml` | SourceArtifact, BuildArtifact | `python -m cicd.release verify-or-rollback` |

Every CodeBuild action gets `EXPECTED_SHA=#{SourceVariables.CommitId}` and
`PIPELINE_EXECUTION_ID=#{codepipeline.PipelineExecutionId}`. Deploy and Verify refuse to continue
if the artifact's `release.json` does not match `EXPECTED_SHA`.

The Deploy stage never runs if the Test stage fails, because it consumes the Test stage's output
artifact. That is the deployment gate.

## Releases and the live release

- **Release ID** = the full commit SHA (`p5-baseline` for the seeded Project 5 site). The build
  writes `release.json` (`release_id`, `commit`, `short`, `built_at`, `pipeline_execution_id`) and
  stamps `Release <short sha>` into every page footer.
- **Layout** in `s3://csce412isaacgeng-site`:
  - `releases/<id>/…`: immutable. `release.json` is uploaded last and marks the release complete. A
    complete release is never overwritten; re-running the same commit reuses it.
  - `deployments/history.json`: private, outside every served path.
- **Live release** = CloudFront origin `OriginPath` (`/releases/<id>`). `cicd.release.current_release()`
  parses it. Production also serves `/release.json`, which the Verify stage checks against the
  expected ID.
- **History** (`deployments/history.json`): one entry per release with `status` and an `events`
  list (`deployed`, `verified`, `failed`, `rolled_back`, `rollback_failed`) carrying the execution
  ID, timestamps and details.

### Concurrency

QUEUED mode already runs pipeline executions one at a time. History writes still use S3
conditional writes (`IfMatch` on the ETag, `IfNoneMatch: *` on first create) with retries,
because the Recovery Coordinator can write history from outside the pipeline at the same moment.

## Cache handling

- HTML and JSON are uploaded with `Cache-Control: no-cache`; other assets with
  `public, max-age=86400`. Content types come from `cicd/content-types.json`, and any unmapped
  extension fails the deploy.
- Switching `OriginPath` does not change the cache key, so every switch (deploy or rollback)
  waits for the distribution to reach `Deployed`, then invalidates `/*` and waits for the
  invalidation to complete before verification starts.

## Verification

`tests/postdeploy` runs the checks in `tools/live_checks.py`, the same checks the Verifier agent
runs:

- the domain is reachable over valid TLS, the certificate covers the domain, and HTTP→HTTPS returns 301
- every required page returns 200 with CloudFront headers; www works; missing pages get the custom 404
- `/release.json` `release_id` equals the expected release
- the Content-Type of html, css, js, svg and json matches an independent expectation (deliberately
  not read from `content-types.json`)
- critical function: the deployed `projects.html` and `js/main.js` run in jsdom, a filter is clicked,
  and the cards and the "Showing N of 4 projects" count must change
- S3 security: an anonymous GET of `releases/<id>/index.html` on the bucket's REST endpoint returns
  403, and all four Block Public Access settings are on

The jsdom check also runs before deploy, against the built output.

## Rollback

`cicd.release.rollback_to_known_good()` is the only rollback code, used both by the pipeline and
by the Recovery Coordinator. It is bounded:

- the target is the newest release whose status is `verified`, excluding the failed one, whose
  `releases/<id>/release.json` exists
- it switches `OriginPath` to that whole release, waits for Deployed and invalidates `/*`; it never
  uploads or edits individual files
- callers re-verify against the target's ID before recording `rolled_back` (or `rollback_failed`)
- if no verified release exists, it refuses and the execution fails loudly

`cicd/seed_baseline.py` made `p5-baseline` the first verified release, so even the first pipeline
release had something to roll back to.

## Logs and results

- CodeBuild: CloudWatch Logs **disabled**; S3 logs at
  `s3://csce412isaacgeng-pipeline-artifacts/codebuild-logs/<build-uuid>.gz`
- Test results: the CodeBuild report group `csce412-site-tests` (JUnit) and
  `s3://csce412isaacgeng-pipeline-artifacts/results/<execution-id>/{predeploy,postdeploy,postdeploy-after-rollback}.json`
  plus the JUnit XML
- The artifacts bucket has Block Public Access on, ACLs disabled and SSE-S3, and denies non-TLS access

## AWS services and permissions

Only S3, CloudFront, Route 53, ACM, CodePipeline and CodeBuild, plus the IAM roles they need.
The GitHub source uses a **CodeConnections** connection, which is how CodePipeline V2 integrates
with GitHub (no separate service is run). No CloudFormation, CloudWatch Logs, Secrets Manager,
EventBridge or SNS.

| Identity | Can do |
|---|---|
| `csce412-codepipeline` | read/write pipeline artifacts; start and read the 3 builds; use this one connection |
| `csce412-codebuild-test` | read/write artifacts and results; write test reports. **No site bucket, no CloudFront** |
| `csce412-codebuild-deploy` | the above minus reports; list site bucket; read/write `releases/*`; read/write `deployments/history.json`; get/update distribution and invalidate (this distribution only) |
| `csce412-codebuild-verify` | like deploy but **read-only** on `releases/*`; plus test reports and reading Block Public Access |
| `csce412-agent` (agents) | read `releases/*` and `deployments/*`, write only `history.json`; CloudFront get/update/invalidate (rollback and approved fixes); read-only CodePipeline, CodeBuild, test reports, artifacts `results/` and `codebuild-logs/`, and the 4 pipeline roles' policies; Project 5's approval-gated bucket-policy and Block Public Access fixes |

No policy uses a `*` resource; `tests/unit/test_setup_spec.py` enforces this for the pipeline roles.
The agent policy is a customer-managed policy (`csce412-agent-policy`), because it is larger than the
2,048-character limit for inline user policies.

## Setup (one time, admin profile)

1. Create and authorize a GitHub connection in the console (Developer Tools → Settings →
   Connections, us-east-2), limited to this repository. Copy its ARN.
2. `AWS_PROFILE=csce412-admin python cicd/seed_baseline.py`: copies the Project 5 root objects
   to `releases/p5-baseline/`, switches, verifies, and records it `verified` (reverts on failure).
3. `AWS_PROFILE=csce412-admin python cicd/setup_pipeline.py --connection-arn <ARN> --dry-run`, then
   without `--dry-run`. Idempotent; creates the artifacts bucket, report group, 4 roles, 3 projects
   and the pipeline. It refuses to create the pipeline before a verified release exists, because
   creating a pipeline immediately releases `main`.
4. Agent identity:
   ```bash
   aws iam create-policy --policy-name csce412-agent-policy --policy-document file://iam/agent-policy.json
   aws iam attach-user-policy --user-name csce412-agent --policy-arn arn:aws:iam::<account>:policy/csce412-agent-policy
   # later changes:
   aws iam create-policy-version --policy-arn arn:aws:iam::<account>:policy/csce412-agent-policy \
       --policy-document file://iam/agent-policy.json --set-as-default    # at most 5 versions are kept
   ```
5. Optional, after a pipeline release is verified: `AWS_PROFILE=csce412-admin python cicd/cleanup_root.py`
   deletes the legacy root objects (copies remain in `releases/p5-baseline/`).

## Running the pieces locally

```bash
npm ci
python -m cicd.release build                 # build/site with release.json + footer stamp
python -m cicd.release test predeploy        # the Test stage's gate
node tests/js/filter_check.mjs build/site    # jsdom check on the build
AWS_PROFILE=csce412-agent python -m cicd.release test postdeploy   # live checks (read-only)
AWS_PROFILE=csce412-agent python -m cicd.release status            # live release and history
python -m pytest tests/agents                # agent and harness unit tests (needs the openai package)
```

## Known limitations

- An anonymous S3 GET returns 403 whether or not the key exists, so the S3 check proves only that
  access is denied. The `/release.json` check proves which release is live.
- The Test Engineer harness keeps or drops whole files: a weak test function can ride along in a
  file whose other tests kill mutants.
- The harness's static checks (allowlisted imports, no writes/exec) plus a credential-free
  subprocess are a guard rail for coursework, not a security sandbox.
