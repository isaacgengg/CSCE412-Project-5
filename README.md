# CSCE 412 Project 5: Secure Cloud Website with a Multi-Agent AI System

Live site: **https://csce412isaacgeng.me**

A static portfolio website hosted on AWS (private S3 bucket → CloudFront with Origin Access
Control → custom domain on Route 53 with an ACM certificate), deployed, audited, verified and
repaired by six cooperating AI agents built on the OpenAI API.

## Architecture

```
Visitor ──DNS──> Route 53 (A/AAAA alias) ──> CloudFront ──OAC (SigV4)──> S3 bucket (private)
                                              │  ACM certificate (us-east-1), HTTP→HTTPS
                                              └─ custom 403/404 → /404.html
```

```
requirements.yaml ─> Planner ─> Site Agent ─> Infra Agent ─> Security Agent ─> Verifier ─┐
                                                   ▲                                     │ fail
                                                   └── approved fix ── human ── Diagnoser ┘
                     (every hand-off is a JSON message on the bus, logged to runs/<id>/)
```

Each agent is an LLM (OpenAI Chat Completions with function calling) given a system prompt,
a JSON task, and an **allowlist of tools**. Python executes the tools and returns raw results;
the model reasons over them and ends by calling `submit_report` with a schema-validated JSON
report, which becomes the next agent's input. Tools that change infrastructure are marked
*consequential* and pause for a human `y/N` before running.

AI decisions vs. deterministic code:

| Decided by the LLM | Done by deterministic Python |
|---|---|
| Task decomposition and assignment (Planner) | Tool execution (boto3, HTTP, TLS, DNS, file parsing) |
| Whether the site is ready to deploy; quality issues (Site) | The Part D check suite and its pass/fail values |
| Which deploy steps to run and when (Infra) | Approval gate, logging, redaction |
| Security findings and severity (Security) | Bootstrap and failure-injection scripts |
| Follow-up probes and symptom summary (Verifier) | |
| Root cause and which allowlisted fix to propose (Diagnoser) | |

The verification verdict always comes from evidence; the orchestrator overwrites the model's
`all_passed` with the computed value.

## Agents

| Agent | Responsibility | Tools | Input → Output |
|---|---|---|---|
| **Planner** (`agents/planner.py`) | Turns `requirements.yaml` into ordered tasks assigned to agents; lists human-only steps | none (JSON mode) | requirements → `plan.json` |
| **Site Agent** (`agents/site_agent.py`) | Checks the five-page site against Part A; blocks deploys on broken references | `inventory_site`, `check_links`, `check_site_requirements` | site tasks → readiness report |
| **Infra Agent** (`agents/infra_agent.py`) | Deploys content and applies approved fixes | CloudFront/S3/Route 53/ACM reads, `s3_sync_site`*, `cloudfront_invalidate`, `cloudfront_wait_deployed`; in fix mode only the single proposed action* | site report or fix proposal → deployment/fix report |
| **Security Agent** (`agents/security_agent.py`) | Read-only audit: Block Public Access, bucket policy scope, OAC, HTTPS redirect, cert, TLS policy, DNS | all AWS read tools | deploy report → findings with severity and evidence |
| **Verifier** (`agents/verifier_agent.py`) | Runs the Part D suite, then investigates failures with extra probes | `http_get`, `tls_certificate`, `dns_resolve`, `s3_direct_access` | security report → verification report + evidence file |
| **Diagnoser** (`agents/diagnoser_agent.py`) | Finds the root cause in AWS config and proposes one fix from an allowlist | AWS reads + probes | verification report → proposal (action, params, reason, risk) |

\* consequential: requires human approval.

Verification checks (Part D): domain returns 200; TLS chain valid; certificate covers the
domain and is unexpired; `http://` redirects (301/308) to `https://`; all five pages return 200;
responses carry CloudFront headers; anonymous direct S3 request returns 403; missing pages
return the custom 404; `www` returns 200.

## Requirements

- Python 3.10+
- An OpenAI API key (you pay per call; a full `deploy` run costs a few cents with a mini model)
- AWS credentials for two identities:
  - **admin** (your own) for one-time `deploy/bootstrap.py` and `scripts/inject_failure.py`
  - **agent** user with only `iam/agent-policy.json` for everything the agents do

## Installation

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI key used by `core/llm.py` (never committed; `.env` is gitignored) |
| `OPENAI_MODEL` | Model name, default `gpt-4.1-mini`; any current Chat Completions model with tool calling works |
| `AWS_PROFILE` | Profile for the least-privilege agent user, e.g. `csce412-agent` |
| `AWS_REGION` | Optional; bucket region is auto-detected |
| `P5_CONFIG` | Optional path to an alternate `config.yaml` |

Non-secret settings (domain, bucket, required pages) live in `config.yaml`.

### Agent IAM user

1. In IAM, create a user `csce412-agent` with no console access.
2. Replace `ACCOUNT_ID`, `DISTRIBUTION_ID`, `HOSTED_ZONE_ID` in `iam/agent-policy.json`, and
   attach it as an inline policy.
3. Create an access key and run `aws configure --profile csce412-agent`.

The agents cannot create or delete buckets, distributions, certificates or zones, can only
change A/AAAA records in this one zone, and cannot delete the bucket policy.

## Running

```bash
python orchestrator.py plan       # Planner only
python orchestrator.py deploy     # full pipeline: plan → site → infra → security → verify (→ recover)
python orchestrator.py audit      # Security Agent only
python orchestrator.py verify     # verify; on failure diagnose → approve → fix → re-verify
```

Flags: `--no-recover` (verify without fixing), `--auto-deny` (deny every approval; safe dry run).

Each run writes `runs/<timestamp>-<command>/`:

| File | Contents |
|---|---|
| `timeline.log` | Chronological, human-readable log of every agent step |
| `agents/<agent>.log` | Per-agent log |
| `messages.jsonl` | Agent-to-agent messages (sender, recipient, type, payload) |
| `tools.jsonl` | Every tool call with arguments, approval status and raw result |
| `llm_calls.jsonl` | Every model call: agent, model, tokens, tool chosen |
| `approvals.jsonl` | Human approval decisions |
| `evidence/` | `plan.json`, `security_audit.json`, `verification_*.json`, `diagnosis_*.json` |

All log output passes through `core/redact.py`.

## Reproducing the workflow

**1. Infrastructure (one time, admin profile)**

```bash
AWS_PROFILE=default python deploy/bootstrap.py
```

Idempotent: creates any missing piece (certificate, private bucket with Block Public Access,
OAC, distribution with HTTPS redirect and custom error pages, OAC-scoped bucket policy, alias
records) and uploads `site/`. Re-running changes nothing that already exists.

**2. Deploy and verify with the agents**

```bash
python orchestrator.py deploy
```

Approve the `s3_sync_site` prompt. Expect every check to report `PASS`.

**3. Failure diagnosis and recovery (Part E)**

```bash
AWS_PROFILE=default python scripts/inject_failure.py list
AWS_PROFILE=default python scripts/inject_failure.py inject bucket-policy
python orchestrator.py verify          # detects, diagnoses, asks approval, fixes, re-verifies
python scripts/inject_failure.py clear # remove the backup file once recovered
```

Scenarios: `bucket-policy` (OAC can't read S3, every page 403), `http-allowed` (no HTTPS
redirect), `root-object` (`/` fails, `/index.html` works). Each saves prior state and can be
undone manually with `inject_failure.py revert` if needed.

**4. Package the logs**

```bash
python scripts/export_logs.py --deploy runs/<deploy-run> --failure runs/<verify-run>
```

## Human-only steps

- Registering `csce412isaacgeng.me` at Namecheap.
- Setting Namecheap's Custom DNS to the Route 53 nameservers (printed by bootstrap).
- Creating the agent IAM user and access keys.
- Approving consequential changes at the prompt.
- Injecting the controlled failure.

## Repository layout

```
agents/      planner, site, infra, security, verifier, diagnoser
core/        agent loop + approval gate, OpenAI wrapper, message bus/logging, config, redaction
tools/       site inspection, HTTP/TLS/DNS probes, AWS operations, per-agent tool allowlists
deploy/      bootstrap.py (idempotent infrastructure setup)
scripts/     inject_failure.py, export_logs.py
iam/         agent-policy.json (least privilege)
site/        the five-page website + 404 page
config.yaml  requirements.yaml  requirements.txt  .env.example
```

## AI usage

The agent system and website were written with AI assistance (Claude), then reviewed, tested
and adapted by me. The agents themselves call the OpenAI API at runtime.
