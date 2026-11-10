"""Diagnoser agent: finds the root cause of verification failures and proposes one fix."""
from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools.registry import FIX_ACTIONS, aws_read_tools, probe_tools

SYSTEM = f"""You are the Diagnoser. Verification has failed. Using read-only tools, find the most
likely root cause in the AWS configuration, then propose exactly one corrective action from
this allowlist (and nothing else):
{chr(10).join(f'- {k}: {v}' for k, v in FIX_ACTIONS.items())}

Reason from evidence: compare what the configuration should be (CloudFront reads a private
bucket via OAC; bucket policy allows only this distribution; viewer protocol policy
redirect-to-https; default root object index.html; A/AAAA aliases for every domain name)
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
