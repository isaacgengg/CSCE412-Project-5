"""Recovery Coordinator (extends the Project 5 Infra Agent).

rollback mode: decide whether production needs the known-good release restored and do it with
the same bounded rollback the pipeline uses (cicd/release.py), behind human approval.
fix mode: apply exactly one Diagnoser-proposed fix (Project 5 behaviour, now including rollback).
"""
from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools.cicd_tools import CICD
from tools.registry import aws_read_tools, aws_write_tools, cache_tools, cicd_read_tools

NAME = "recovery_coordinator"

ROLLBACK_SYSTEM = """You are the Recovery Coordinator. Production may be serving a bad release.
1. Read release_status: the live release, the known-good release (newest verified, excluding nothing),
   and the history. Use recent_executions for context on what the pipeline did.
2. If the live release is verified and is the newest verified release, and the task gives no evidence
   that production is broken, report status not_needed.
3. Otherwise call rollback_to_known_good (a human must approve; give a clear rationale). It restores a
   whole verified release by switching CloudFront's OriginPath and invalidating the cache. Never
   upload, edit or delete individual files; there is no tool for that.
4. Report the release that was live before, the release live now, and what the Verifier should check."""

FIX_SYSTEM = """You are the Recovery Coordinator applying a fix proposed by the Diagnoser.
Apply ONLY the proposed action, with the proposed parameters, using the matching tool.
The tool will ask a human for approval; pass the Diagnoser's reasoning as the rationale.
If the change is a CloudFront config change (other than rollback_to_known_good, which already waits
and invalidates), wait for deployment and then invalidate ["/*"]. If the human denies the change,
stop and report that."""

REPORT = {"type": "object", "properties": {
    "status": {"type": "string", "enum": ["rolled_back", "not_needed", "fixed", "skipped", "denied", "failed"]},
    "from_release": {"type": "string"},
    "to_release": {"type": "string"},
    "actions_taken": {"type": "array", "items": {"type": "string"}},
    "notes": {"type": "string"},
}, "required": ["status", "actions_taken", "notes"]}


def build_rollback(ctx: Context, aws: AWS, cicd: CICD) -> Agent:
    reads = [t for t in cicd_read_tools(cicd) if t.name in ("release_status", "recent_executions")]
    return Agent(NAME, ROLLBACK_SYSTEM, reads + [aws_write_tools(aws)["rollback_to_known_good"]], REPORT, ctx)


def build_fix(ctx: Context, aws: AWS, action: str) -> Agent:
    """Only the single proposed action is exposed, so the agent cannot do anything else."""
    w = aws_write_tools(aws)
    if action not in w:
        raise ValueError(f"action {action} is not on the allowlist")
    reads = [t for t in aws_read_tools(aws) if t.name in ("cloudfront_summary", "release_status")]
    return Agent(NAME, FIX_SYSTEM, reads + [w[action]] + cache_tools(aws), REPORT, ctx)
