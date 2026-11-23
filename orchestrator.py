#!/usr/bin/env python3
"""Coordinates the agents. Every hand-off goes through the bus and is logged to runs/<id>/.

Deployment is done by the CI/CD pipeline (push to main), not by an agent. Agents assist:

  python orchestrator.py release-status                 # live release, known-good, executions (no LLM)
  python orchestrator.py review-pipeline                # Pipeline Engineer reviews the pipeline
  python orchestrator.py generate-tests                 # Site Agent -> Test Engineer -> harness -> tests/generated
  python orchestrator.py analyze-release [--execution-id ID]   # Diagnoser explains an execution
  python orchestrator.py rollback                       # Recovery Coordinator -> approval -> Verifier
  python orchestrator.py verify                         # Verifier; on failure Diagnoser -> fix -> re-verify
  python orchestrator.py audit                          # Security Agent
  python orchestrator.py plan                           # Planner
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

import yaml

from agents import (diagnoser_agent, pipeline_engineer, recovery_agent, security_agent, site_agent,
                    test_engineer, verifier_agent)
from agents.planner import Planner
from cicd import release
from core.agent import Approver, Context
from core.bus import Bus, RunLog
from core.config import ROOT, load_config
from core.llm import LLM
from tools import test_harness
from tools.aws_tools import AWS
from tools.cicd_tools import CICD

ORCH = "orchestrator"
COMMANDS = ["release-status", "review-pipeline", "generate-tests", "analyze-release", "rollback",
            "verify", "audit", "plan"]


def make_context(label: str, auto_deny: bool) -> Context:
    cfg = load_config()
    log = RunLog(ROOT / "runs", label)
    ctx = Context(cfg=cfg, log=log, bus=Bus(log), llm=LLM(cfg.model, log), approver=Approver(log, auto_deny))
    log.agent_log(ORCH, f"run {log.run_id} started (model={cfg.model}, domain={cfg.domain})")
    return ctx


def tasks_for(plan: dict | None, agent: str) -> list[dict]:
    return [t for t in (plan or {}).get("tasks", []) if t["agent"] == agent]


def record_rollback(ctx: Context, before: str, after: str, verified: bool) -> None:
    if before != after and release.RELEASE_ID.fullmatch(before):
        release.record(before, "rolled_back" if verified else "rollback_failed", rolled_back_to=after,
                       by=recovery_agent.NAME, run_id=ctx.log.run_id)


# ---------------- Project 5 steps (kept) ----------------
def step_plan(ctx: Context, requirements_file: Path) -> dict:
    reqs = yaml.safe_load(requirements_file.read_text())
    ctx.bus.send(ORCH, "planner", "task", {"requirements": reqs})
    plan = Planner(ctx).run(reqs)
    ctx.log.evidence("plan.json", plan)
    ctx.bus.send("planner", ORCH, "report", plan)
    return plan


def step_site(ctx: Context, plan: dict | None, recipient: str = ORCH) -> dict:
    task = {"tasks": tasks_for(plan, "site"), "required_pages": ctx.cfg.required_pages}
    ctx.bus.send(ORCH, "site_agent", "task", task)
    report = site_agent.build(ctx).run(task)
    ctx.bus.send("site_agent", recipient, "report", report)
    return report


def step_security(ctx: Context, aws: AWS, plan: dict | None) -> dict:
    task = {"tasks": tasks_for(plan, "security"), "domain": ctx.cfg.domain}
    ctx.bus.send(ORCH, "security_agent", "task", task)
    report = security_agent.build(ctx, aws).run(task)
    ctx.log.evidence("security_audit.json", report)
    ctx.bus.send("security_agent", ORCH, "report", report)
    return report


def step_verify(ctx: Context, aws: AWS, label: str, release_id: str | None = None,
                context: dict | None = None) -> dict:
    """Deterministic live suite (same as the pipeline's Verify stage), then the LLM explains it.
    release_id defaults to what CloudFront's OriginPath says is live."""
    release_id = release_id or release.current_release()
    expected = release_id if release.RELEASE_ID.fullmatch(release_id) else None
    agent = verifier_agent.build(ctx, aws)
    ctx.log.agent_log("verifier_agent", f"running deterministic check suite (expected release {expected})")
    checks = verifier_agent.run_suite(agent, expected)
    passed = all(c["passed"] for c in checks)
    ctx.log.evidence(f"verification_{label}.json", {"all_passed": passed, "expected_release": expected,
                                                    "checks": checks})
    for c in checks:
        ctx.log.agent_log("verifier_agent", f"{'PASS' if c['passed'] else 'FAIL'}  {c['id']}")
    task = {"context": context, "expected_release": expected,
            "check_results": [{k: c[k] for k in ("id", "description", "passed")} for c in checks],
            "evidence": {c["id"]: c["evidence"] for c in checks if not c["passed"]}}
    ctx.bus.send(ORCH, "verifier_agent", "task", {"check_ids": [c["id"] for c in checks], "all_passed": passed})
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
        ctx.bus.send("diagnoser_agent", recovery_agent.NAME, "proposal", diag)
        if not proposal:
            ctx.log.agent_log(ORCH, "diagnoser found no fix; escalating to human")
            return False
        before = release.current_release()
        fix = recovery_agent.build_fix(ctx, aws, proposal["action"]).run(
            {"proposed_action": proposal, "root_cause": diag["root_cause"]})
        ctx.bus.send(recovery_agent.NAME, "verifier_agent", "result", fix)
        if fix.get("status") in ("denied", "failed", "skipped"):
            ctx.log.agent_log(ORCH, f"fix not applied ({fix.get('status')}); stopping")
            return False
        after = release.current_release()
        verify_report = step_verify(ctx, aws, label=f"after_fix_round{rnd}", release_id=after)
        record_rollback(ctx, before, after, verify_report["all_passed"])
        if verify_report["all_passed"]:
            ctx.log.agent_log(ORCH, "recovery verified: all checks pass")
            return True
    return False


# ---------------- Project 6 commands ----------------
def cmd_release_status(ctx: Context, cicd: CICD) -> bool:
    status = release.status()
    try:
        status["recent_executions"] = cicd.recent_executions(5)["executions"]
    except Exception as e:      # e.g. agent policy without CodePipeline read access
        status["recent_executions"] = {"error": f"{type(e).__name__}: {e}"}
    ctx.log.evidence("release_status.json", status)
    print(json.dumps(status, indent=2, default=str))
    return True


def cmd_review_pipeline(ctx: Context, cicd: CICD) -> bool:
    task = {"pipeline": "csce412-site-pipeline", "repository": "cicd/", "region": ctx.cfg.region}
    ctx.bus.send(ORCH, "pipeline_engineer", "task", task)
    report = pipeline_engineer.build(ctx, cicd).run(task)
    ctx.log.evidence("pipeline_review.json", report)
    ctx.bus.send("pipeline_engineer", ORCH, "report", report)
    for f in report.get("findings", []):
        ctx.log.agent_log("pipeline_engineer", f"{f['status'].upper():5} {f['check']}")
    return report["overall"] != "fail"


def cmd_generate_tests(ctx: Context, requirements_file: Path) -> bool:
    site = step_site(ctx, None, recipient="test_engineer")
    session = test_engineer.Session()
    task = {"requirements": yaml.safe_load(requirements_file.read_text()), "site_report": site,
            "existing_generated_tests": sorted(p.name for p in test_harness.GENERATED.glob("test_*.py"))}
    ctx.bus.send(ORCH, "test_engineer", "task", {"mutants": test_harness.MUTANTS})
    report = test_engineer.build(ctx, session).run(task)
    ctx.bus.send("test_engineer", ORCH, "report", report)
    if not session.candidates:
        ctx.log.agent_log(ORCH, "test engineer staged no candidates")
        return False
    ctx.log.agent_log(ORCH, f"final deterministic evaluation of {len(session.candidates)} candidates")
    final = test_harness.evaluate(session.candidates)          # the harness decides, not the model
    written = test_harness.write_kept(final, session.candidates, ctx.log.run_id)
    ctx.log.evidence("test_generation.json", {"agent_report": report, "evaluation": final,
                                              "written": written, "candidates": session.candidates})
    for r in final["candidates"]:
        ctx.log.agent_log("test_engineer", f"{'KEEP' if r['kept'] else 'DROP'}  {r['name']}: {r['reason']}")
    ctx.log.agent_log(ORCH, f"wrote {written}; surviving mutants: {final['surviving_mutants'] or 'none'}")
    return bool(written)


def cmd_analyze_release(ctx: Context, aws: AWS, cicd: CICD, execution_id: str | None) -> bool:
    executions = cicd.recent_executions(10)["executions"]
    execution = next((e for e in executions if e["id"] == execution_id), None) if execution_id \
        else (executions[0] if executions else None)
    if not execution:
        ctx.log.agent_log(ORCH, f"execution {execution_id or '(latest)'} not found in the last 10")
        return False
    task = {"execution": execution, **cicd.execution_actions(execution["id"])}
    ctx.bus.send(ORCH, "diagnoser_agent", "task", task)
    report = diagnoser_agent.build_release(ctx, aws, cicd).run(task)
    ctx.log.evidence(f"release_analysis_{execution['id']}.json", report)
    ctx.bus.send("diagnoser_agent", ORCH, "report", report)
    return True


def cmd_rollback(ctx: Context, aws: AWS, cicd: CICD) -> bool:
    before = release.current_release()
    task = {"live_release": before, "request": "Restore the known-good release if production needs it."}
    ctx.bus.send(ORCH, recovery_agent.NAME, "task", task)
    report = recovery_agent.build_rollback(ctx, aws, cicd).run(task)
    after = release.current_release()                         # what actually happened, not what was claimed
    ctx.log.evidence("rollback.json", {"agent_report": report, "live_before": before, "live_after": after})
    if after == before:
        ctx.log.agent_log(ORCH, f"no rollback performed ({report.get('status')}); live release is {after}")
        return report.get("status") == "not_needed"
    ctx.bus.send(recovery_agent.NAME, "verifier_agent", "result", {**report, "live_before": before,
                                                                   "live_after": after})
    v = step_verify(ctx, aws, label="after_rollback", release_id=after, context={"rolled_back_from": before})
    record_rollback(ctx, before, after, v["all_passed"])
    return v["all_passed"]


def main() -> int:
    ap = argparse.ArgumentParser(description="CSCE 412 multi-agent system (Projects 5 and 6)")
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("--requirements", default=str(ROOT / "requirements.yaml"))
    ap.add_argument("--execution-id", help="analyze-release: pipeline execution ID (default: latest)")
    ap.add_argument("--no-recover", action="store_true", help="verify only; skip diagnosis/fix")
    ap.add_argument("--auto-deny", action="store_true", help="deny every approval prompt (dry run)")
    args = ap.parse_args()

    ctx = make_context(args.command, args.auto_deny)
    aws, cicd = AWS(ctx.cfg), CICD(ctx.cfg)
    ok = True
    if args.command == "release-status":
        ok = cmd_release_status(ctx, cicd)
    elif args.command == "review-pipeline":
        ok = cmd_review_pipeline(ctx, cicd)
    elif args.command == "generate-tests":
        ok = cmd_generate_tests(ctx, Path(args.requirements))
    elif args.command == "analyze-release":
        ok = cmd_analyze_release(ctx, aws, cicd, args.execution_id)
    elif args.command == "rollback":
        ok = cmd_rollback(ctx, aws, cicd)
    elif args.command == "plan":
        step_plan(ctx, Path(args.requirements))
    elif args.command == "audit":
        ok = step_security(ctx, aws, None)["overall"] != "fail"
    elif args.command == "verify":
        rep = step_verify(ctx, aws, label="initial")
        ok = rep["all_passed"] or (not args.no_recover and step_recover(ctx, aws, rep))
    ctx.log.agent_log(ORCH, f"run finished: {'SUCCESS' if ok else 'FAILED'}  logs: {ctx.log.dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
