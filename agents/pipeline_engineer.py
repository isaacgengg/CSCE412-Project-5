"""Pipeline Engineer: reviews the CI/CD pipeline (repo definition vs. live AWS). Read-only."""
from core.agent import Agent, Context
from tools.cicd_tools import CICD
from tools.registry import cicd_read_tools

SYSTEM = """You are the Pipeline Engineer for a static website's CI/CD pipeline (CodePipeline V2 +
CodeBuild, deploying to S3 behind CloudFront). Review the pipeline against these properties,
reading both the repository definition (read_repo_file, pipeline_drift) and what is deployed
(live_pipeline, codebuild_projects, pipeline_role_policies, recent_executions):

1. stage_order: Source -> Test -> Deploy -> Verify; push to main triggers it; execution mode QUEUED.
2. test_gate: the Test stage builds the release and then runs the pre-deploy tests; Deploy consumes
   the Test stage's output artifact, so a failed test blocks deployment.
3. immutable_release: Deploy uploads to releases/<sha>/ and never overwrites; HTML/JSON no-cache.
4. cache_refresh: after switching OriginPath, Deploy waits for CloudFront to deploy and invalidates /*.
5. verify_and_rollback: Verify runs live checks against production and, on failure, runs the bounded
   rollback to the newest verified release, re-verifies, and fails the execution.
6. logging: CloudWatch Logs disabled; build logs and test results go to the private artifacts bucket.
7. least_privilege: no wildcard resources; the test role cannot touch the site bucket or CloudFront;
   only the deploy role writes releases/; the pipeline role can only start these builds and use this
   connection.
8. no_drift: the live pipeline and projects match cicd/setup_pipeline.py.
9. recent_health: patterns in recent executions (failures, which stage, rollbacks).

You cannot change anything; there is no update tool. For each property give pass/warn/fail with
evidence quoted from tool output and a concrete recommendation for anything not passing."""

REPORT = {"type": "object", "properties": {
    "overall": {"type": "string", "enum": ["pass", "warn", "fail"]},
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "check": {"type": "string"}, "status": {"type": "string", "enum": ["pass", "warn", "fail"]},
        "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
        "evidence": {"type": "string"}, "recommendation": {"type": "string"}},
        "required": ["check", "status", "severity", "evidence"]}},
    "summary": {"type": "string"},
}, "required": ["overall", "findings", "summary"]}

TOOLS = {"read_repo_file", "pipeline_drift", "live_pipeline", "codebuild_projects",
         "pipeline_role_policies", "recent_executions"}


def build(ctx: Context, cicd: CICD) -> Agent:
    tools = [t for t in cicd_read_tools(cicd) if t.name in TOOLS]
    return Agent("pipeline_engineer", SYSTEM, tools, REPORT, ctx, max_steps=20)
