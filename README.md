# CSCE 412 Projects 5–6: Secure Cloud Website, Multi-Agent AI System, and CI/CD with Rollback

Live site: **https://csce412isaacgeng.me**

A static portfolio website on AWS: a private S3 bucket behind CloudFront with Origin Access
Control, on a custom domain with Route 53 and an ACM certificate. **Project 6** adds a CI/CD
pipeline: every push to `main` is built, tested, deployed as an immutable release, verified in
production, and automatically rolled back to the last known-good release if verification fails.
Eight cooperating AI agents (OpenAI Chat Completions with function calling) review the pipeline,
generate tests for it, diagnose failures and coordinate recovery. The pipeline itself never needs
an AI model.

Project 5 as submitted is tagged `p5-final`.

## Architecture

```
Visitor ─DNS─> Route 53 (A/AAAA alias) ─> CloudFront ─OAC (SigV4)─> S3 bucket (private)
                                           │  OriginPath = /releases/<live release id>
                                           │  ACM certificate (us-east-1), HTTP→HTTPS, custom 403/404
                                           └─ releases/<sha>/ (immutable)   deployments/history.json (private)

git push main ─> CodePipeline V2 (us-east-2, QUEUED)
                 Source ─> Test ─────────> Deploy ────────────> Verify & Recover
                           build + gate    releases/<sha>/       live checks; on failure roll back
                           (blocks deploy) OriginPath, invalidate to the newest verified release
                 CodeBuild logs + JUnit/JSON results ─> private artifacts bucket (no CloudWatch)
```

Full pipeline details are in [docs/cicd.md](docs/cicd.md).

```
                ┌─ review-pipeline ─> Pipeline Engineer (read-only review, drift, least privilege)
                ├─ generate-tests ──> Site Agent ─> Test Engineer ─> mutation harness ─> tests/generated/
orchestrator ───┼─ analyze-release ─> Diagnoser (executions, CodeBuild logs, results, history, commit)
                ├─ rollback ────────> Recovery Coordinator ─approval─> rollback ─> Verifier ─> history
                ├─ verify ──────────> Verifier ─fail─> Diagnoser ─> Recovery Coordinator (one fix) ─> Verifier
                └─ audit / plan ────> Security Agent / Planner
      every hand-off is a JSON message on the bus, logged under runs/<id>/
```

Each agent is an LLM given a system prompt, a JSON task and an **allowlist of tools**. Python runs
the tools and returns raw results; the model ends by calling `submit_report` with a
schema-validated report. Tools that change infrastructure are *consequential* and pause for a
human `y/N`.

| Decided by an LLM | Done by deterministic code |
|---|---|
| Task decomposition (Planner) | The entire pipeline: build, tests, deploy, verification, rollback (`cicd/release.py`) |
| Site quality notes and readiness (Site) | Release IDs, history, current release, the bounded rollback |
| Pipeline findings and recommendations (Pipeline Engineer) | Which generated tests are kept (mutation harness) |
| Candidate tests from the requirements (Test Engineer) | Live verification pass/fail (`tools/live_checks.py`) |
| Root cause and fix for a failed execution or verification (Diagnoser) | Approval gate, logging, redaction |
| Whether to roll back, and the rationale (Recovery Coordinator) | What was actually rolled back (read from CloudFront, not from the model) |
| Security findings and severity (Security) | |
| Follow-up probes and symptom summary (Verifier) | |

## Agents

| Agent | Responsibility | Tools | Command |
|---|---|---|---|
| **Planner** (`agents/planner.py`) | Turns `requirements.yaml` (incl. delivery requirements) into tasks for the other agents | none (JSON mode) | `plan` |
| **Site Agent** (`agents/site_agent.py`) | Checks the site against the website requirements; quality notes feed the Test Engineer | `inventory_site`, `check_links`, `check_site_requirements` | `generate-tests` |
| **Pipeline Engineer** (`agents/pipeline_engineer.py`) | Reviews stage order, test gate, immutable releases, cache refresh, rollback, S3-only logging, least-privilege roles, drift from the repo, recent health. **Read-only; no update tool** | `read_repo_file`, `live_pipeline`, `pipeline_drift`, `codebuild_projects`, `pipeline_role_policies`, `recent_executions` | `review-pipeline` |
| **Test Engineer** (`agents/test_engineer.py`) | Writes candidate pytest files from the requirements; the harness keeps only those that pass on the real build **and** fail on at least one mutant (deleted page, broken link, removed script tag, dropped nav link, corrupt `release.json`); rejects exact-copy assertions | `inventory_site`, `read_site_file`, `stage_candidate`, `drop_candidate`, `evaluate_candidates` | `generate-tests` |
| **Diagnoser** (`agents/diagnoser_agent.py`) | *Release mode:* reports the observed failure, failed stage/action, log and test excerpts, root cause, recommended fix and evidence. *Verify mode:* root cause in AWS config plus one allowlisted fix | CI/CD reads (`execution_actions`, `codebuild_build`, `build_log`, `test_results`, `failed_test_cases`, `release_status`, `git_commit`, …) and probes | `analyze-release`, `verify` |
| **Recovery Coordinator** (`agents/recovery_agent.py`) | Reads history, identifies the known-good release, runs the same bounded rollback as the pipeline\*, hands off to the Verifier; in fix mode applies exactly one Diagnoser proposal\* | `release_status`, `recent_executions`, `rollback_to_known_good`\* (fix mode: the single proposed tool) | `rollback`, `verify` |
| **Security Agent** (`agents/security_agent.py`) | Read-only audit: Block Public Access, bucket policy scope, OAC, HTTPS redirect, certificate, TLS policy, DNS | AWS read tools | `audit` |
| **Verifier** (`agents/verifier_agent.py`) | Runs the pipeline's live check suite (incl. the **live release ID**, Content-Types, the jsdom filter check) and investigates failures | `http_get`, `http_get_json`, `tls_certificate`, `dns_resolve`, `s3_direct_access`, `jsdom_filter_check`, `s3_public_access_block` | `verify`, after `rollback` |

\* consequential: requires human approval.

## Requirements

- Python 3.10+ and Node.js 18+ (for the jsdom check)
- An OpenAI API key for the agent commands (the pipeline and `release-status` need none)
- AWS: an **admin** profile for one-time setup scripts, and the least-privilege **agent** user
  (`iam/agent-policy.json`) for everything the agents do

## Installation

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # agents + local use (requirements-ci.txt is what CodeBuild installs)
npm ci                               # jsdom, pinned in package-lock.json
cp .env.example .env                 # then edit .env
```

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI key for the agents (`.env`, gitignored; never committed) |
| `OPENAI_MODEL` | Model, default `gpt-4.1-mini` |
| `AWS_PROFILE` | `csce412-agent` for the agents; `csce412-admin` only for setup scripts |
| `AWS_REGION` | Optional; the site and pipeline are in `us-east-2` (`config.yaml`) |
| `P5_CONFIG` | Optional alternate `config.yaml` |
| `EXPECTED_SHA`, `PIPELINE_EXECUTION_ID` | Set by CodePipeline for each build; locally `EXPECTED_SHA` falls back to `git rev-parse HEAD` |
| `EXPECTED_RELEASE`, `SITE_DIR` | Test inputs (release that must be live; built site to test), set by `cicd/release.py` |

Non-secret settings (domain, bucket, distribution, artifacts bucket, required pages) are in `config.yaml`.

## How a release happens

1. Merge or push to `main`. The CodeConnections trigger starts the pipeline (QUEUED: one execution at a time).
2. **Test**: builds `build/site`, writes `release.json`, stamps the short SHA into every footer, and runs
   `tests/unit`, `tests/predeploy` and `tests/generated`. Any failure stops the execution, so nothing is deployed.
3. **Deploy**: uploads to `releases/<full sha>/`, points CloudFront's `OriginPath` at it, waits for
   `Deployed`, invalidates `/*`, and records `deployed`.
4. **Verify & Recover**: runs `tests/postdeploy` against production. If it passes, the release is
   recorded `verified`. If it fails, the release is recorded `failed`, the pipeline rolls back to the
   newest verified release, re-verifies it, records `rolled_back`, and the execution ends Failed.

**Release IDs**: the commit SHA (`release.json`, page footers, the `releases/` prefix).
**Which release is live**: CloudFront's `OriginPath` (`python orchestrator.py release-status`, or
`https://csce412isaacgeng.me/release.json`).
**Cache handling**: HTML/JSON are `no-cache`, assets `max-age=86400`; every switch waits for
Deployed and then invalidates `/*`.
**Rollback**: whole releases only, only to an existing `verified` release, never individual files
or manual uploads. Details in [docs/cicd.md](docs/cicd.md#rollback).

## Commands

```bash
python orchestrator.py release-status                  # live + known-good release, history, recent executions (no LLM)
python orchestrator.py review-pipeline                 # Pipeline Engineer
python orchestrator.py generate-tests                  # Site Agent -> Test Engineer -> harness -> tests/generated/
python orchestrator.py analyze-release [--execution-id ID]   # Diagnoser (default: latest execution)
python orchestrator.py rollback                        # Recovery Coordinator -> approval -> Verifier
python orchestrator.py verify                          # live checks; on failure diagnose -> approve -> fix -> re-verify
python orchestrator.py audit                           # Security Agent
python orchestrator.py plan                            # Planner
```

Flags: `--auto-deny` (deny every approval; safe dry run), `--no-recover` (verify only).
Run with `AWS_PROFILE=csce412-agent`.

Each run writes `runs/<timestamp>-<command>/`:

| File | Contents |
|---|---|
| `timeline.log` | Chronological log of every agent step |
| `agents/<agent>.log` | Per-agent log |
| `messages.jsonl` | Agent-to-agent messages (sender, recipient, type, payload) |
| `tools.jsonl` | Every tool call: arguments, approval status, raw result |
| `llm_calls.jsonl` | Every model call: agent, model, tokens, tool chosen |
| `approvals.jsonl` | Human approval decisions |
| `evidence/` | `pipeline_review.json`, `test_generation.json`, `release_analysis_<id>.json`, `rollback.json`, `verification_*.json`, … |

All log output passes through `core/redact.py`.

## Setup (one time)

Infrastructure from Project 5 (`deploy/bootstrap.py`, admin profile) already exists: domain, zone,
certificate, bucket, OAC, distribution, bucket policy, DNS. Content is now published only by the pipeline.

1. **GitHub connection** (console, us-east-2): Developer Tools → Settings → Connections → GitHub;
   install the AWS Connector app for this repository only. Copy the ARN.
2. **Baseline**: `AWS_PROFILE=csce412-admin python cicd/seed_baseline.py`
3. **Pipeline**: `AWS_PROFILE=csce412-admin python cicd/setup_pipeline.py --connection-arn <ARN> [--dry-run]`
4. **Agent user** (`csce412-agent`, no console access): attach `iam/agent-policy.json` as the
   customer-managed policy `csce412-agent-policy` (it exceeds the 2,048-character inline limit);
   create an access key and run `aws configure --profile csce412-agent`.
5. **Optional**: `AWS_PROFILE=csce412-admin python cicd/cleanup_root.py` removes the legacy
   Project 5 root objects once a pipeline release is verified.

Exact commands are in [docs/cicd.md](docs/cicd.md#setup-one-time-admin-profile).

## Permissions

| Identity | Scope |
|---|---|
| Pipeline and CodeBuild roles | Per stage, exact resource ARNs only; the test role cannot touch the site or CloudFront; only deploy writes `releases/`; verify writes only `history.json` |
| Agent user | Read releases and history, write only `history.json`; CloudFront update/invalidate for rollback and approved fixes; read-only CodePipeline/CodeBuild/results/logs/pipeline-role policies; Project 5's approval-gated bucket fixes. Cannot create or delete buckets, distributions, certificates or zones, cannot change the pipeline, cannot upload site files |

AWS services used: S3, CloudFront, Route 53, ACM, CodePipeline (with its CodeConnections GitHub
source), CodeBuild, and IAM roles. Full table in [docs/cicd.md](docs/cicd.md#aws-services-and-permissions).

## Experiments

[scripts/experiments/](scripts/experiments/README.md): (1) a successful release with a visible
change, (2) a broken internal link blocked by the Test stage, (3) a deploy-configuration defect
(`.js` served as `text/plain`) caught in production and rolled back automatically. Package the
evidence with `scripts/export_logs.py`.

## Tests

| Suite | Runs in | What |
|---|---|---|
| `tests/unit` | Test stage | Release logic with a fake S3; pipeline definition safety properties |
| `tests/predeploy` | Test stage | Files, pages, internal references, assets, structure, nav, `release.json`, footer stamp, jsdom filter |
| `tests/generated` | Test stage | Tests kept by the Test Engineer's harness |
| `tests/postdeploy` | Verify stage | Live checks (`tools/live_checks.py`) |
| `tests/agents` | locally | Harness, CI/CD tools with fake AWS, rollback approval and recording |

## Repository layout

```
agents/      planner, site, pipeline engineer, test engineer, diagnoser, recovery coordinator, security, verifier
cicd/        release.py (shared release logic + pipeline CLI), buildspecs, content-types.json,
             setup_pipeline.py, seed_baseline.py, cleanup_root.py
core/        agent loop + approval gate, OpenAI wrapper, message bus/logging, config, redaction
tools/       site inspection, probes, live checks, AWS and CI/CD reads, test harness, tool allowlists
tests/       unit, predeploy, postdeploy, generated, agents, js/filter_check.mjs
deploy/      bootstrap.py (Project 5 infrastructure)
scripts/     experiments/, export_logs.py, inject_failure.py (Project 5 failure scenarios)
iam/         agent-policy.json
docs/        cicd.md
site/        the website
```

## Human-only steps

Registering the domain and delegating DNS; creating and authorizing the GitHub connection; running
the admin setup scripts; creating the agent user and its access key; approving consequential agent
actions; merging the experiment PRs.

## AI usage

The agent system, pipeline and website were written with AI assistance (Claude), then reviewed,
tested and adapted by me. The agents call the OpenAI API at runtime.
