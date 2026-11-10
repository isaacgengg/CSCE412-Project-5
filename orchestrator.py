#!/usr/bin/env python3
"""Coordinates the agents. Every hand-off goes through the bus and is logged.

Usage:
  python orchestrator.py deploy    # plan -> site -> infra -> security -> verify (-> diagnose/fix)
  python orchestrator.py verify    # verify; on failure diagnose, propose, approve, fix, re-verify
  python orchestrator.py audit     # security audit only
  python orchestrator.py plan      # planner only
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

import yaml

from agents import diagnoser_agent, infra_agent, security_agent, site_agent, verifier_agent
from agents.planner import Planner
from core.agent import Approver, Context
from core.bus import Bus, RunLog
from core.config import ROOT, load_config
from core.llm import LLM
from tools.aws_tools import AWS

ORCH = "orchestrator"


def make_context(label: str, auto_deny: bool) -> Context:
    cfg = load_config()
    log = RunLog(ROOT / "runs", label)
    ctx = Context(cfg=cfg, log=log, bus=Bus(log), llm=LLM(cfg.model, log), approver=Approver(log, auto_deny))
    log.agent_log(ORCH, f"run {log.run_id} started (model={cfg.model}, domain={cfg.domain})")
    return ctx


def tasks_for(plan: dict | None, agent: str) -> list[dict]:
    return [t for t in (plan or {}).get("tasks", []) if t["agent"] == agent]


# ---------------- steps ----------------
def step_plan(ctx: Context, requirements_file: Path) -> dict:
    reqs = yaml.safe_load(requirements_file.read_text())
    ctx.bus.send(ORCH, "planner", "task", {"requirements": reqs})
    plan = Planner(ctx).run(reqs)
    ctx.log.evidence("plan.json", plan)
    ctx.bus.send("planner", ORCH, "report", plan)
    return plan


def step_site(ctx: Context, plan: dict | None) -> dict:
    task = {"tasks": tasks_for(plan, "site"), "required_pages": ctx.cfg.required_pages}
    ctx.bus.send(ORCH, "site_agent", "task", task)
    report = site_agent.build(ctx).run(task)
    ctx.bus.send("site_agent", "infra_agent", "report", report)
    return report


def step_deploy(ctx: Context, aws: AWS, plan: dict | None, site_report: dict) -> dict:
    task = {"tasks": tasks_for(plan, "infrastructure"), "site_report": site_report,
            "bucket": ctx.cfg.bucket, "domain": ctx.cfg.domain}
    ctx.bus.send(ORCH, "infra_agent", "task", task)
    report = infra_agent.build_deploy(ctx, aws).run(task)
    ctx.bus.send("infra_agent", "security_agent", "report", report)
    return report


def step_security(ctx: Context, aws: AWS, plan: dict | None, deploy_report: dict | None) -> dict:
    task = {"tasks": tasks_for(plan, "security"), "deployment": deploy_report, "domain": ctx.cfg.domain}
    ctx.bus.send(ORCH, "security_agent", "task", task)
    report = security_agent.build(ctx, aws).run(task)
    ctx.log.evidence("security_audit.json", report)
    ctx.bus.send("security_agent", "verifier_agent", "report", report)
    return report


def step_verify(ctx: Context, aws: AWS, plan: dict | None, security_report: dict | None, label: str) -> dict:
    agent = verifier_agent.build(ctx, aws)
    ctx.log.agent_log("verifier_agent", "running deterministic check suite")
    checks = verifier_agent.run_suite(agent)
    passed = all(c["passed"] for c in checks)
    ctx.log.evidence(f"verification_{label}.json", {"all_passed": passed, "checks": checks})
    for c in checks:
        ctx.log.agent_log("verifier_agent", f"{'PASS' if c['passed'] else 'FAIL'}  {c['id']}")
    task = {"tasks": tasks_for(plan, "verification"), "security_report": security_report,
            "check_results": [{k: c[k] for k in ("id", "description", "passed")} for c in checks],
            "evidence": {c["id"]: c["evidence"] for c in checks if not c["passed"]}}
    ctx.bus.send(ORCH, "verifier_agent", "task", {"check_ids": [c["id"] for c in checks],
                                                  "all_passed": passed})
    report = agent.run(task)
    report["all_passed"] = passed                      # evidence wins over the model
    report["failed_checks"] = [c["id"] for c in checks if not c["passed"]]
    ctx.bus.send("verifier_agent", "diagnoser_agent" if not passed else ORCH, "report", report)
    return report


def step_recover(ctx: Context, aws: AWS, verify_report: dict, max_rounds: int = 2) -> bool:
    for rnd in range(1, max_rounds + 1):
        ctx.log.agent_log(ORCH, f"recovery round {rnd}")
        diag = diagnoser_agent.build(ctx, aws).run({"verification_report": verify_report,
                                                     "domain": ctx.cfg.domain, "bucket": ctx.cfg.bucket})
        ctx.log.evidence(f"diagnosis_round{rnd}.json", diag)
        proposal = diag.get("proposed_action")
        ctx.bus.send("diagnoser_agent", "infra_agent", "proposal", diag)
        if not proposal:
            ctx.log.agent_log(ORCH, "diagnoser found no fix; escalating to human")
            return False
        fix_task = {"proposed_action": proposal, "root_cause": diag["root_cause"]}
        fix = infra_agent.build_fix(ctx, aws, proposal["action"]).run(fix_task)
        ctx.bus.send("infra_agent", "verifier_agent", "result", fix)
        if fix.get("status") in ("denied", "failed", "skipped"):
            ctx.log.agent_log(ORCH, f"fix not applied ({fix.get('status')}); stopping")
            return False
        verify_report = step_verify(ctx, aws, None, None, label=f"after_fix_round{rnd}")
        if verify_report["all_passed"]:
            ctx.log.agent_log(ORCH, "recovery verified: all checks pass")
            return True
    return False


# ---------------- commands ----------------
def main() -> int:
    ap = argparse.ArgumentParser(description="CSCE 412 Project 5 multi-agent system")
    ap.add_argument("command", choices=["deploy", "verify", "audit", "plan"])
    ap.add_argument("--requirements", default=str(ROOT / "requirements.yaml"))
    ap.add_argument("--no-recover", action="store_true", help="verify only; skip diagnosis/fix")
    ap.add_argument("--auto-deny", action="store_true", help="deny every approval prompt (dry run)")
    args = ap.parse_args()

    ctx = make_context(args.command, args.auto_deny)
    aws = AWS(ctx.cfg)
    ok = True
    if args.command == "plan":
        step_plan(ctx, Path(args.requirements))
    elif args.command == "audit":
        ok = step_security(ctx, aws, None, None)["overall"] != "fail"
    elif args.command == "verify":
        sec = None
        rep = step_verify(ctx, aws, None, sec, label="initial")
        ok = rep["all_passed"] or (not args.no_recover and step_recover(ctx, aws, rep))
    elif args.command == "deploy":
        plan = step_plan(ctx, Path(args.requirements))
        site = step_site(ctx, plan)
        if not site.get("ready_to_deploy"):
            ctx.log.agent_log(ORCH, f"site not ready: {site.get('blocking_issues')}")
            return 1
        dep = step_deploy(ctx, aws, plan, site)
        sec = step_security(ctx, aws, plan, dep)
        rep = step_verify(ctx, aws, plan, sec, label="post_deploy")
        ok = rep["all_passed"] or (not args.no_recover and step_recover(ctx, aws, rep))
    ctx.log.agent_log(ORCH, f"run finished: {'SUCCESS' if ok else 'FAILED'}  logs: {ctx.log.dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
