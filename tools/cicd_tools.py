"""Read-only CI/CD evidence for the agents: CodePipeline, CodeBuild, S3 build logs, test
results, release history, the repo's pipeline files and local git. Nothing here changes AWS."""
from __future__ import annotations
import gzip
import json
import re
import subprocess
from functools import cached_property

import boto3

from cicd import release
from cicd.setup_pipeline import PIPELINE, STAGES, build_spec
from core.config import ROOT, Config
from core.redact import redact

ROLES = [f"csce412-codebuild-{s}" for s in STAGES] + ["csce412-codepipeline"]
PROJECTS = [f"csce412-site-{s}" for s in STAGES]
REPO_FILES = [f"cicd/buildspec-{s}.yml" for s in STAGES] + [
    "cicd/content-types.json", "cicd/release.py", "cicd/setup_pipeline.py", "tools/live_checks.py",
    "package.json", "requirements-ci.txt", "site/js/main.js", "site/projects.html"]


def summarize_action(a: dict) -> dict:
    res = (a.get("output") or {}).get("executionResult") or {}
    return {"stage": a.get("stageName"), "action": a.get("actionName"), "status": a.get("status"),
            "start": str(a.get("startTime")), "end": str(a.get("lastUpdateTime")),
            "build_id": res.get("externalExecutionId"),
            "summary": redact(res.get("externalExecutionSummary") or "")[:500]}


def diff(want, have, path="") -> list[str]:
    """Differences between the repo definition (want) and live (have); extra live keys are ignored."""
    if isinstance(want, dict):
        have = have or {}
        return [d for k, v in want.items() for d in diff(v, have.get(k), f"{path}.{k}")]
    if isinstance(want, list):
        have = have or []
        if len(want) != len(have):
            return [f"{path}: expected {len(want)} items, live has {len(have)}"]
        return [d for i, (w, h) in enumerate(zip(want, have)) for d in diff(w, h, f"{path}[{i}]")]
    if isinstance(want, str) and want.startswith("[") and isinstance(have, str):   # JSON-in-string config
        try:
            return diff(json.loads(want), json.loads(have), path)
        except ValueError:
            pass
    return [] if want == have else [f"{path}: expected {want!r}, live {have!r}"]


class CICD:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @cached_property
    def cp(self):
        return boto3.client("codepipeline", region_name=self.cfg.region)

    @cached_property
    def cb(self):
        return boto3.client("codebuild", region_name=self.cfg.region)

    @cached_property
    def s3(self):
        return boto3.client("s3", region_name=self.cfg.region)

    @cached_property
    def iam(self):
        return boto3.client("iam")

    # ---------- repository ----------
    def read_repo_file(self, path: str, start_line: int = 1, max_lines: int = 150) -> dict:
        if path not in REPO_FILES:
            return {"error": f"not allowed; choose one of {REPO_FILES}"}
        lines = (ROOT / path).read_text().splitlines()
        chunk = lines[max(start_line, 1) - 1:max(start_line, 1) - 1 + max_lines]
        return {"path": path, "total_lines": len(lines), "start_line": start_line, "content": "\n".join(chunk)}

    def git_commit(self, sha: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
            return {"error": "sha must be 7-40 lowercase hex characters"}
        p = subprocess.run(["git", "show", "--stat", "--patch", "--format=%H%n%an %ad%n%s%n%b", sha, "--",
                            "site", "cicd", "tests", "tools", "package.json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=20)
        if p.returncode:
            return {"error": "commit not in the local clone (run git fetch)", "stderr": p.stderr[-300:]}
        return {"sha": sha, "show": redact(p.stdout)[:5000]}

    # ---------- CodePipeline ----------
    def live_pipeline(self) -> dict:
        r = self.cp.get_pipeline(name=PIPELINE)
        p = r["pipeline"]
        return {"name": p["name"], "pipelineType": p.get("pipelineType"), "executionMode": p.get("executionMode"),
                "roleArn": p["roleArn"], "artifactStore": p.get("artifactStore"), "triggers": p.get("triggers"),
                "version": p.get("version"), "updated": str(r["metadata"].get("updated")),
                "stages": [{"name": s["name"], "actions": [{
                    "name": a["name"], "provider": a["actionTypeId"]["provider"],
                    "inputs": [i["name"] for i in a.get("inputArtifacts", [])],
                    "outputs": [o["name"] for o in a.get("outputArtifacts", [])],
                    "configuration": a.get("configuration", {})} for a in s["actions"]]} for s in p["stages"]]}

    def pipeline_drift(self) -> dict:
        live = self.cp.get_pipeline(name=PIPELINE)["pipeline"]
        conn = live["stages"][0]["actions"][0]["configuration"].get("ConnectionArn", "")
        want = build_spec(conn)
        diffs = diff(want["pipeline"], live, "pipeline")
        projects = {p["name"]: p for p in self.cb.batch_get_projects(names=PROJECTS)["projects"]}
        for s, spec in want["projects"].items():
            have = projects.get(spec["name"])
            if not have:
                diffs.append(f"project {spec['name']}: missing")
                continue
            have = {**have, "source": {k: have["source"].get(k) for k in ("type", "buildspec")}}
            diffs += diff({k: spec[k] for k in ("source", "serviceRole", "logsConfig", "timeoutInMinutes")},
                          have, f"project.{spec['name']}")
        return {"matches_repo": not diffs, "differences": diffs[:40]}

    def recent_executions(self, max_results: int = 10) -> dict:
        r = self.cp.list_pipeline_executions(pipelineName=PIPELINE, maxResults=max_results)
        out = []
        for e in r["pipelineExecutionSummaries"]:
            rev = (e.get("sourceRevisions") or [{}])[0]
            out.append({"id": e["pipelineExecutionId"], "status": e["status"], "start": str(e.get("startTime")),
                        "updated": str(e.get("lastUpdateTime")), "trigger": (e.get("trigger") or {}).get("triggerType"),
                        "commit": rev.get("revisionId"), "commit_message": (rev.get("revisionSummary") or "")[:200],
                        "status_summary": e.get("statusSummary")})
        return {"pipeline": PIPELINE, "executions": out}

    def execution_actions(self, execution_id: str) -> dict:
        items = []
        for page in self.cp.get_paginator("list_action_executions").paginate(
                pipelineName=PIPELINE, filter={"pipelineExecutionId": execution_id}):
            items += page["actionExecutionDetails"]
        actions = sorted((summarize_action(a) for a in items), key=lambda a: a["start"])
        return {"execution_id": execution_id, "actions": actions,
                "failed": [a for a in actions if a["status"] == "Failed"]}

    # ---------- CodeBuild ----------
    def codebuild_build(self, build_id: str) -> dict:
        builds = self.cb.batch_get_builds(ids=[build_id])["builds"]
        if not builds:
            return {"error": f"build {build_id} not found"}
        b = builds[0]
        return {"id": b["id"], "project": b["projectName"], "status": b["buildStatus"],
                "source_version": b.get("resolvedSourceVersion"),
                "env": {v["name"]: v["value"] for v in b.get("environment", {}).get("environmentVariables", [])
                        if v.get("type") == "PLAINTEXT"},
                "phases": [{"phase": p["phaseType"], "status": p.get("phaseStatus"),
                            "seconds": p.get("durationInSeconds"),
                            "messages": [c["message"] for c in p.get("contexts", []) if c.get("message")]}
                           for p in b.get("phases", [])],
                "s3_log": (b.get("logs") or {}).get("s3LogsArn"), "reports": b.get("reportArns", [])}

    def build_log(self, build_id: str, tail: int = 120, grep: str = "") -> dict:
        arn = self.codebuild_build(build_id).get("s3_log")
        if not arn:
            return {"error": "build has no S3 log"}
        bucket, key = arn.split(":::", 1)[1].split("/", 1)
        raw = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
        lines = raw.decode(errors="replace").splitlines()
        if grep:
            lines = [l for l in lines if re.search(grep, l, re.I)]
        return {"build_id": build_id, "log": f"s3://{bucket}/{key}", "matching_lines": len(lines),
                "lines": [redact(l)[:400] for l in lines[-max(1, min(tail, 300)):]]}

    def failed_test_cases(self, report_arn: str) -> dict:
        cases = self.cb.describe_test_cases(reportArn=report_arn, filter={"status": "FAILED"})["testCases"]
        return {"report": report_arn, "failed": [{"name": c.get("name"), "prefix": c.get("prefix"),
                                                  "message": redact(c.get("message") or "")[:800]} for c in cases]}

    def codebuild_projects(self) -> dict:
        return {"projects": [{"name": p["name"], "serviceRole": p["serviceRole"],
                              "buildspec": p["source"].get("buildspec"), "image": p["environment"]["image"],
                              "logsConfig": p.get("logsConfig"), "timeoutInMinutes": p.get("timeoutInMinutes")}
                             for p in self.cb.batch_get_projects(names=PROJECTS)["projects"]]}

    def pipeline_role_policies(self) -> dict:
        out = {}
        for role in ROLES:
            names = self.iam.list_role_policies(RoleName=role)["PolicyNames"]
            out[role] = {
                "inline": {n: self.iam.get_role_policy(RoleName=role, PolicyName=n)["PolicyDocument"] for n in names},
                "attached": [p["PolicyArn"] for p in
                             self.iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]]}
        return out

    # ---------- results and releases ----------
    def test_results(self, execution_id: str) -> dict:
        bucket, prefix = self.cfg.artifacts_bucket, f"results/{execution_id}/"
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])]
        results = {k.rsplit("/", 1)[1]: json.loads(self.s3.get_object(Bucket=bucket, Key=k)["Body"].read())
                   for k in keys if k.endswith(".json")}
        return {"execution_id": execution_id, "files": keys, "results": results}

    def release_status(self) -> dict:
        data, _ = release.read_history()
        return {**release.status(), "history": data["releases"][-8:]}
