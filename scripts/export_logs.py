#!/usr/bin/env python3
"""Packages real logs into <out>.zip for submission. Copies and re-redacts; never writes new results.

  AWS_PROFILE=csce412-agent python scripts/export_logs.py \\
      --runs runs/<id>-review-pipeline runs/<id>-generate-tests runs/<id>-analyze-release ... \\
      --executions <pipeline execution id> [<id> ...] [--out project6-logs]

Contents:
  agent-runs/<run>/       timeline.log, agents/*.log, messages/tools/llm_calls/approvals .jsonl, evidence/
  pipeline/<execution>/   execution.json (actions + status), every CodeBuild log, stored test results
  releases.json           live release, known-good release, full deployments/history.json
"""
from __future__ import annotations
import argparse
import gzip
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cicd import release  # noqa: E402
from core.config import ROOT, load_config  # noqa: E402
from core.redact import redact  # noqa: E402
from tools.cicd_tools import CICD  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text))


def export_run(run: Path, out: Path) -> None:
    for f in run.rglob("*"):
        if f.is_file():
            write(out / "agent-runs" / run.name / f.relative_to(run), f.read_text(errors="replace"))


def export_execution(c: CICD, exec_id: str, out: Path) -> str:
    d = out / "pipeline" / exec_id
    summary = next((e for e in c.recent_executions(25)["executions"] if e["id"] == exec_id), {"id": exec_id})
    actions = c.execution_actions(exec_id)
    write(d / "execution.json", json.dumps({"execution": summary, **actions}, indent=2, default=str))
    for a in actions["actions"]:
        if not a["build_id"]:
            continue
        arn = c.codebuild_build(a["build_id"]).get("s3_log")
        if arn:
            bucket, key = arn.split(":::", 1)[1].split("/", 1)
            raw = c.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
            write(d / f"{a['stage'].lower()}-codebuild.log", raw.decode(errors="replace"))
    bucket = c.cfg.artifacts_bucket
    for o in c.s3.list_objects_v2(Bucket=bucket, Prefix=f"results/{exec_id}/").get("Contents", []):
        body = c.s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode(errors="replace")
        write(d / "results" / o["Key"].rsplit("/", 1)[1], body)
    return f"{exec_id}  {summary.get('status', '?')}  {summary.get('commit', '')[:7]}  " \
           f"failed: {[a['stage'] for a in actions['failed']] or 'none'}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", nargs="*", type=Path, default=[], help="runs/<id> directories")
    ap.add_argument("--executions", nargs="*", default=[], help="pipeline execution IDs")
    ap.add_argument("--out", default="project6-logs")
    a = ap.parse_args()
    out = ROOT / a.out
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    for run in a.runs:
        export_run(run, out)
    c = CICD(load_config())
    lines = [export_execution(c, e, out) for e in a.executions]
    history, _ = release.read_history()
    write(out / "releases.json", json.dumps({**release.status(), "history": history}, indent=2))
    write(out / "README.txt", README.format(
        runs="\n".join(f"  {r.name}" for r in a.runs) or "  (none)",
        executions="\n".join(f"  {l}" for l in lines) or "  (none)"))
    shutil.make_archive(str(out), "zip", out.parent, out.name)
    print(f"wrote {out}.zip")


README = """CSCE 412 Project 6 - Pipeline and Agent Execution Logs
=====================================================
Every file is copied from a real run (agent runs under runs/, pipeline evidence from CodePipeline,
CodeBuild's S3 logs and the private artifacts bucket). Credentials are redacted.

Agent runs (agent-runs/<run>/):
{runs}
  timeline.log      chronological log of every agent step
  agents/*.log      per-agent log
  messages.jsonl    agent-to-agent hand-offs on the bus
  tools.jsonl       every tool call: arguments, approval, raw result
  llm_calls.jsonl   every model call: model, tokens, tool chosen
  approvals.jsonl   human approval decisions
  evidence/         reports (pipeline_review, test_generation, release_analysis_*, verification_*, rollback)

Pipeline executions (pipeline/<execution>/):  id  status  commit  failed stages
{executions}
  execution.json          stages/actions with status, CodeBuild build IDs, failure summaries
  <stage>-codebuild.log   full CodeBuild log for that stage
  results/                JUnit XML and JSON results (predeploy, postdeploy, postdeploy-after-rollback)

releases.json: live release (CloudFront OriginPath), known-good release, and deployments/history.json
(deployed / verified / failed / rolled_back events per release).
"""

if __name__ == "__main__":
    main()
