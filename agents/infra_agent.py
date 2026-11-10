"""Infrastructure agent: performs deployments and approved fixes."""
from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools.registry import aws_read_tools, aws_write_tools, cache_tools

DEPLOY_SYSTEM = """You are the Infrastructure Agent. Deploy the website safely:
1. Read the current CloudFront and bucket state first.
2. Only deploy if the Site Agent's report says ready_to_deploy is true; otherwise report why not.
3. Upload the site with s3_sync_site (requires human approval; give a clear rationale).
4. Invalidate CloudFront so visitors get the new content.
5. Confirm the objects now in the bucket match the site pages.
Never change security settings during a deployment; that is the Security Agent's domain."""

FIX_SYSTEM = """You are the Infrastructure Agent applying a fix proposed by the Diagnoser.
Apply ONLY the proposed action, with the proposed parameters, using the matching tool.
The tool will ask a human for approval; pass the Diagnoser's reasoning as the rationale.
If the change is a CloudFront config change, wait for deployment. Then invalidate the cache
with paths ["/*"] so the fix is visible. If the human denies the change, stop and report that."""

REPORT = {"type": "object", "properties": {
    "status": {"type": "string", "enum": ["deployed", "fixed", "skipped", "denied", "failed"]},
    "actions_taken": {"type": "array", "items": {"type": "string"}},
    "resources": {"type": "object", "properties": {
        "bucket": {"type": "string"}, "distribution_id": {"type": "string"},
        "distribution_domain": {"type": "string"}}},
    "notes": {"type": "string"},
}, "required": ["status", "actions_taken", "notes"]}


def build_deploy(ctx: Context, aws: AWS) -> Agent:
    w = aws_write_tools(aws)
    return Agent("infra_agent", DEPLOY_SYSTEM,
                 aws_read_tools(aws) + [w["s3_sync_site"]] + cache_tools(aws), REPORT, ctx)


def build_fix(ctx: Context, aws: AWS, action: str) -> Agent:
    """Only the single proposed action is exposed, so the agent cannot do anything else."""
    w = aws_write_tools(aws)
    if action not in w:
        raise ValueError(f"action {action} is not on the allowlist")
    reads = [t for t in aws_read_tools(aws) if t.name == "cloudfront_summary"]
    return Agent("infra_agent", FIX_SYSTEM, reads + [w[action]] + cache_tools(aws), REPORT, ctx)
