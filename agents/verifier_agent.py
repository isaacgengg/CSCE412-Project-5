"""Verification agent.

Step 1 (deterministic): run tools/live_checks.py (the same checks as the pipeline's
post-deploy stage) through the probe tools and
compute pass/fail from the raw evidence. The LLM cannot change these results.
Step 2 (LLM): interpret the evidence, run extra probes if something looks off,
and summarize what failed and what it points to.
"""
from __future__ import annotations
from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools import live_checks
from tools.registry import aws_read_tools, probe_tools

SYSTEM = """You are the Verification Agent. You receive the results of a deterministic check suite
run against the live website. The pass/fail values are computed from evidence and are final.
Your job: explain the results, run additional probes when a failure needs more evidence
(for example: is it every page or one page? does DNS resolve? is the certificate valid but
the page failing?), and describe the symptoms precisely so the Diagnoser can find the cause.
Do not guess at root causes inside AWS; describe observed behavior."""

REPORT = {"type": "object", "properties": {
    "all_passed": {"type": "boolean"},
    "failed_checks": {"type": "array", "items": {"type": "string"}},
    "symptoms": {"type": "array", "items": {"type": "string"}},
    "additional_evidence": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"},
}, "required": ["all_passed", "failed_checks", "symptoms", "summary"]}


def build(ctx: Context, aws: AWS) -> Agent:
    pab = [t for t in aws_read_tools(aws) if t.name == "s3_public_access_block"]
    return Agent("verifier_agent", SYSTEM, probe_tools(ctx.cfg, aws) + pab, REPORT, ctx)


def run_suite(agent: Agent, release: str | None = None) -> list[dict]:
    """The same checks the pipeline runs (tools/live_checks.py), with every probe logged."""
    call = lambda tool, **a: agent.invoke(tool, a, requested_by="deterministic-suite")
    return live_checks.run_suite(call, agent.ctx.cfg, release)
