"""Diagnoser agent.

verify mode (Project 5): root cause of live verification failures + one allowlisted fix.
release mode (Project 6): why a pipeline execution failed, from executions, CodeBuild builds,
S3 build logs, test results and release history.
"""
from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools.cicd_tools import CICD
from tools.registry import FIX_ACTIONS, aws_read_tools, cicd_read_tools, probe_tools

SYSTEM = f"""You are the Diagnoser. Verification has failed. Using read-only tools, find the most
likely root cause in the AWS configuration, then propose exactly one corrective action from
this allowlist (and nothing else):
{chr(10).join(f'- {k}: {v}' for k, v in FIX_ACTIONS.items())}

Reason from evidence: compare what the configuration should be (CloudFront reads a private
bucket via OAC; bucket policy allows only this distribution; viewer protocol policy
redirect-to-https; default root object index.html; A/AAAA aliases for every domain name;
origin OriginPath /releases/<id> pointing at a verified release, see release_status)
with what the tools show. Quote the specific setting that is wrong. If you cannot find a
cause, say so and set proposed_action to null.

Before proposing an action, confirm the current configuration actually differs from the
desired state. If it already matches (for example, records exist but a recent change has
not propagated or is cached), set proposed_action to null and say that waiting and
re-verifying is the correct next step. Never re-apply a change that is already in place."""

REPORT = {"type": "object", "properties": {
    "root_cause": {"type": "string"},
    "evidence": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "proposed_action": {"type": ["object", "null"], "properties": {
        "action": {"type": "string", "enum": list(FIX_ACTIONS)},
        "parameters": {"type": "object"},
        "reason": {"type": "string"},
        "risk": {"type": "string"}}, "required": ["action", "parameters", "reason"]},
}, "required": ["root_cause", "evidence", "confidence", "proposed_action"]}


def build(ctx: Context, aws: AWS) -> Agent:
    return Agent("diagnoser_agent", SYSTEM, aws_read_tools(aws) + probe_tools(ctx.cfg, aws), REPORT, ctx)


RELEASE_SYSTEM = """You are the Diagnoser for the CI/CD pipeline. You are given one pipeline execution and
its action summaries. Find out what failed and why, from evidence:
- Identify the failed stage and action and its CodeBuild build ID. Read the build (codebuild_build)
  and its S3 log (build_log; use grep for patterns like FAILED|Error|Traceback|assert|ROLLBACK).
- Read the stored results (test_results) and failed test cases from the build's report ARNs.
- Read release_status to see whether the release was deployed, verified, failed or rolled back, and
  what is live now.
- Read the commit (git_commit) to connect the failure to the change that caused it, and the relevant
  repository file (read_repo_file) if the cause is pipeline or deploy configuration.
Quote short excerpts exactly. The recommended fix is a concrete code or configuration change; if
production is not on a verified release, also recommend running `orchestrator.py rollback`.
If the execution succeeded, say so and report what was verified."""

RELEASE_REPORT = {"type": "object", "properties": {
    "execution_id": {"type": "string"},
    "commit": {"type": "string"},
    "observed_failure": {"type": "string"},
    "failed_stage": {"type": ["string", "null"]},
    "failed_action": {"type": ["string", "null"]},
    "excerpts": {"type": "array", "items": {"type": "object", "properties": {
        "source": {"type": "string"}, "text": {"type": "string"}}, "required": ["source", "text"]}},
    "root_cause": {"type": "string"},
    "recommended_fix": {"type": "string"},
    "production_state": {"type": "string"},
    "evidence": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}, "required": ["execution_id", "observed_failure", "failed_stage", "failed_action", "excerpts",
                "root_cause", "recommended_fix", "production_state", "evidence", "confidence"]}


def build_release(ctx: Context, aws: AWS, cicd: CICD) -> Agent:
    return Agent("diagnoser_agent", RELEASE_SYSTEM, cicd_read_tools(cicd) + probe_tools(ctx.cfg, aws),
                 RELEASE_REPORT, ctx, max_steps=20)
