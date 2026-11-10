"""Site agent: decides whether the website is ready to deploy."""
from core.agent import Agent, Context
from tools.registry import site_tools_for

SYSTEM = """You are the Site Agent. Your job is to decide whether the local static website meets
the website requirements and is ready to deploy. Use the tools to inspect the files.
Judge quality issues the deterministic checks cannot (unclear titles, empty headings,
placeholder content, navigation that would confuse a visitor) and report them separately
from hard requirement failures. A site with broken local references is not ready."""

REPORT = {"type": "object", "properties": {
    "ready_to_deploy": {"type": "boolean"},
    "requirements": {"type": "array", "items": {"type": "object", "properties": {
        "requirement": {"type": "string"}, "met": {"type": "boolean"}, "evidence": {"type": "string"}},
        "required": ["requirement", "met", "evidence"]}},
    "blocking_issues": {"type": "array", "items": {"type": "string"}},
    "quality_notes": {"type": "array", "items": {"type": "string"}},
    "pages": {"type": "array", "items": {"type": "string"}},
}, "required": ["ready_to_deploy", "requirements", "blocking_issues", "quality_notes", "pages"]}


def build(ctx: Context) -> Agent:
    return Agent("site_agent", SYSTEM, site_tools_for(ctx.cfg), REPORT, ctx)
