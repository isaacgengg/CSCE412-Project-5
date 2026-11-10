"""Planner agent: turns the organization's high-level requirements into tasks."""
from __future__ import annotations
from core.agent import Context

AGENTS = {
    "site": "Inspects the local website files against the website requirements.",
    "infrastructure": "Uploads site content to S3, invalidates the CloudFront cache, and reads the existing "
                      "S3/CloudFront config to confirm it matches requirements. It does not create resources "
                      "or change security settings (deploy/bootstrap.py creates the infrastructure).",
    "security": "Read-only audit of S3 public access settings, bucket policy, CloudFront OAC, HTTPS redirect, "
                "TLS policy, the ACM certificate, and Route 53 records. Cannot inspect IAM or source control.",
    "verification": "Probes the live site (HTTP, TLS, DNS, direct S3) and records evidence.",
}

SYSTEM = f"""You are the Planner in a multi-agent cloud engineering system.
Break the organization's requirements into concrete, ordered technical tasks and assign each
task to exactly one of these agents:
{chr(10).join(f'- {k}: {v}' for k, v in AGENTS.items())}

Return JSON only:
{{"summary": str,
  "tasks": [{{"id": "T1", "agent": one of {list(AGENTS)}, "objective": str,
             "success_criteria": [str], "depends_on": [task ids]}}],
  "human_actions": [str]   // steps that cannot be automated (e.g. domain registration)
}}
Rules:
- Make tasks fine-grained: one verifiable outcome per task (aim for 8-14 tasks). For example,
  "HTTPS redirect", "certificate covers the domain" and "bucket blocks public access" are
  separate tasks, even when the same agent handles them.
- Only assign a task to an agent whose description says it can do that work. If a requirement
  cannot be checked by any agent (for example, what is committed to source control), list it
  under human_actions instead.
- Every requirement must be covered by a task or a human action.
- Order tasks so dependencies come first; tasks for different agents may depend on each other."""


class Planner:
    name = "planner"

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, requirements: dict) -> dict:
        self.ctx.log.agent_log(self.name, "decomposing requirements")
        for attempt in range(2):
            plan = self.ctx.llm.json(self.name, SYSTEM, {"requirements": requirements,
                                                         "environment": {"domain": self.ctx.cfg.domain,
                                                                         "bucket": self.ctx.cfg.bucket}})
            problems = self._validate(plan)
            if not problems:
                self.ctx.log.agent_log(self.name, f"plan has {len(plan['tasks'])} tasks")
                return plan
            self.ctx.log.agent_log(self.name, f"plan rejected ({problems}); retrying")
        raise ValueError(f"planner produced an invalid plan: {problems}")

    @staticmethod
    def _validate(plan: dict) -> list[str]:
        problems = []
        tasks = plan.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return ["no tasks"]
        ids = {t.get("id") for t in tasks}
        for t in tasks:
            if t.get("agent") not in AGENTS:
                problems.append(f"{t.get('id')}: unknown agent {t.get('agent')}")
            for d in t.get("depends_on", []):
                if d not in ids:
                    problems.append(f"{t.get('id')}: unknown dependency {d}")
        missing = set(AGENTS) - {t.get("agent") for t in tasks}
        if missing:
            problems.append(f"agents with no tasks: {sorted(missing)}")
        return problems
