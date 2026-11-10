"""Security agent: read-only audit of the deployed configuration."""
from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools.registry import aws_read_tools

SYSTEM = """You are the Security Agent. Audit the website's AWS configuration (read-only) against
these requirements and report findings with evidence quoted from tool output:
- S3 bucket blocks all public access and has no public bucket policy.
- S3 static website hosting is not used; CloudFront reaches S3 through Origin Access Control (OAC).
- The bucket policy grants s3:GetObject only to cloudfront.amazonaws.com, scoped to this
  distribution's ARN via AWS:SourceArn.
- CloudFront redirects HTTP to HTTPS, uses an ISSUED ACM certificate covering the domain,
  and a modern minimum TLS version (TLSv1.2_2021 or newer).
- DNS alias records route the domain to the CloudFront distribution.
Read every relevant setting before concluding. Severity: critical (data exposed or site
insecure), warning (weaker than best practice), info."""

REPORT = {"type": "object", "properties": {
    "overall": {"type": "string", "enum": ["pass", "fail", "warn"]},
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "control": {"type": "string"}, "status": {"type": "string", "enum": ["pass", "fail", "warn"]},
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "evidence": {"type": "string"}, "recommendation": {"type": "string"}},
        "required": ["control", "status", "severity", "evidence"]}},
    "summary": {"type": "string"},
}, "required": ["overall", "findings", "summary"]}


def build(ctx: Context, aws: AWS) -> Agent:
    return Agent("security_agent", SYSTEM, aws_read_tools(aws), REPORT, ctx)
