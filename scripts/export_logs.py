#!/usr/bin/env python3
"""Packages real run logs into project5-agent-logs.zip for Canvas.

  python scripts/export_logs.py --deploy runs/<deploy-run> --failure runs/<verify-run>

Copies (and re-redacts) the actual JSONL/log files; it never writes new results.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ROOT  # noqa: E402
from core.redact import redact  # noqa: E402

OUT = ROOT / "project5-agent-logs"


def copy(src: Path, dst_name: str, lines: list[str] | None = None) -> None:
    text = "".join(lines) if lines is not None else src.read_text()
    (OUT / dst_name).write_text(redact(text))


def filter_jsonl(path: Path, pred) -> list[str]:
    if not path.exists():
        return []
    return [l for l in path.read_text().splitlines(keepends=True) if pred(json.loads(l))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy", required=True, type=Path, help="run dir of `orchestrator.py deploy`")
    ap.add_argument("--failure", required=True, type=Path, help="run dir of the verify run after injection")
    a = ap.parse_args()
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    d, f = a.deploy, a.failure
    n = 1
    for agent in ["planner", "site_agent", "infra_agent", "security_agent", "verifier_agent"]:
        p = d / "agents" / f"{agent}.log"
        if p.exists():
            copy(p, f"{n:02d}-{agent}.log")
            n += 1
    copy(d / "messages.jsonl", f"{n:02d}-agent-communication.jsonl"); n += 1
    copy(d / "tools.jsonl", f"{n:02d}-tool-execution.jsonl"); n += 1
    copy(d / "llm_calls.jsonl", f"{n:02d}-llm-decisions.jsonl"); n += 1
    copy(d / "timeline.log", f"{n:02d}-deploy-timeline.log"); n += 1
    fr = OUT / f"{n:02d}-failure-recovery"
    fr.mkdir()
    for name in ["timeline.log", "messages.jsonl", "tools.jsonl", "llm_calls.jsonl", "approvals.jsonl"]:
        if (f / name).exists():
            (fr / name).write_text(redact((f / name).read_text()))
    shutil.copytree(f / "evidence", fr / "evidence")
    shutil.copytree(d / "evidence", OUT / "deploy-evidence")
    (OUT / "README.txt").write_text(README.format(deploy=d.name, failure=f.name))
    shutil.make_archive(str(OUT), "zip", ROOT, OUT.name)
    print(f"wrote {OUT}.zip")


README = """CSCE 412 Project 5 - Agent Execution Logs
==========================================
All files are copied from real runs of orchestrator.py (runs/{deploy} and runs/{failure}).
Credentials are redacted. Timestamps are UTC; "seq" gives the order within each file.

Deploy run ({deploy}):
  NN-<agent>.log            Human-readable log per agent: task received, tools called, report.
  *-agent-communication     Every bus message (sender, recipient, type, payload). Shows
                            planner -> site_agent -> infra_agent -> security_agent -> verifier_agent.
  *-tool-execution          Every tool call: agent, tool, arguments, approval, raw result.
  *-llm-decisions           Every model call: agent, model, tokens, tool chosen, output.
  *-deploy-timeline.log     All of the above interleaved chronologically.
  deploy-evidence/          plan.json, security_audit.json, verification_post_deploy.json.

Failure recovery run ({failure}):
  A failure was injected with scripts/inject_failure.py (human-run), then
  `orchestrator.py verify` detected it (verification_initial.json), the diagnoser
  proposed a fix (diagnosis_round1.json), a human approved it (approvals.jsonl), the
  infra agent applied it (tools.jsonl), and verification passed again
  (verification_after_fix_round1.json).
"""

if __name__ == "__main__":
    main()
