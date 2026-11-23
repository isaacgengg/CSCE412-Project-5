#!/usr/bin/env python3
"""Release management shared by the pipeline and the agents. Deterministic; no LLM.

Layout in the site bucket:
  releases/<id>/...           immutable release (id = full commit SHA, or p5-baseline)
  deployments/history.json    release history (private: outside every served path)
CloudFront's origin OriginPath (/releases/<id>) decides which release is live.

CLI (run from the repo root; the buildspecs call these):
  python -m cicd.release build                         # site/ -> build/site (+ release.json, footer stamp)
  python -m cicd.release test predeploy|postdeploy     # pytest -> build/results (JUnit + JSON)
  python -m cicd.release deploy --artifact DIR         # upload, switch OriginPath, invalidate, record
  python -m cicd.release verify-or-rollback --artifact DIR
  python -m cicd.release status                        # live release + recent history (read-only)
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ROOT, load_config  # noqa: E402
from core.redact import redact  # noqa: E402

BUILD = ROOT / "build" / "site"
RESULTS = ROOT / "build" / "results"
HISTORY_KEY = "deployments/history.json"
RELEASE_ID = re.compile(r"^([0-9a-f]{40}|p5-baseline)$")
TEST_PATHS = {"predeploy": ["tests/unit", "tests/predeploy", "tests/generated"], "postdeploy": ["tests/postdeploy"]}
FOOTER_END = re.compile(r"(\s*</div>\s*</footer>)")


def say(msg: str) -> None:
    print(redact(f"[release] {msg}"), flush=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@lru_cache
def cfg():
    return load_config()


@lru_cache
def s3():
    return boto3.client("s3", region_name=cfg().region)


@lru_cache
def cf():
    return boto3.client("cloudfront")


def check_release_id(rid: str) -> str:
    if not RELEASE_ID.fullmatch(rid or ""):
        raise ValueError(f"invalid release id {rid!r}")
    return rid


def expected_sha() -> str:
    sha = os.environ.get("EXPECTED_SHA") or os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION")
    if not sha:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"expected a full commit SHA, got {sha!r}")
    return sha


# ---------------- build ----------------
def build(sha: str, src: Path | None = None, out: Path = BUILD) -> Path:
    """Copy the site, stamp the short SHA into every page footer, write release.json."""
    src = Path(src or cfg().site_dir)
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(src, out)
    stamp = f'\n      <span class="release">Release <code>{sha[:7]}</code></span>'
    for page in sorted(out.rglob("*.html")):
        text, n = FOOTER_END.subn(lambda m: stamp + m.group(1), page.read_text(), count=1)
        if n != 1:
            raise SystemExit(f"{page.relative_to(out)}: no footer to stamp")
        page.write_text(text)
    release = {"release_id": sha, "commit": sha, "short": sha[:7], "built_at": now(),
               "pipeline_execution_id": os.environ.get("PIPELINE_EXECUTION_ID")}
    (out / "release.json").write_text(json.dumps(release, indent=2) + "\n")
    return out


def read_artifact_release(artifact: Path) -> str:
    """The artifact's release.json must match EXPECTED_SHA, or nothing gets deployed."""
    sha = expected_sha()
    rel = json.loads((Path(artifact) / "release.json").read_text())
    if rel.get("release_id") != sha or rel.get("commit") != sha:
        raise SystemExit(f"artifact release.json ({rel.get('release_id')}) does not match EXPECTED_SHA {sha}")
    return sha


# ---------------- tests ----------------
def junit_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    failed = []
    for case in root.iter("testcase"):
        bad = case.find("failure") if case.find("failure") is not None else case.find("error")
        if bad is not None:
            failed.append({"test": f"{case.get('classname')}::{case.get('name')}",
                           "message": (bad.get("message") or "")[:500]})
    total = lambda k: sum(int(s.get(k, 0)) for s in suites)
    return {"tests": total("tests"), "failures": total("failures"), "errors": total("errors"),
            "skipped": total("skipped"), "failed": failed}


def run_tests(suite: str, label: str | None = None, env: dict | None = None) -> int:
    """Run a pytest suite, write JUnit + JSON to build/results, upload them when in the pipeline."""
    label = label or suite
    RESULTS.mkdir(parents=True, exist_ok=True)
    junit = RESULTS / f"junit-{label}.xml"
    say(f"running {suite} tests ({label})")
    rc = subprocess.call([sys.executable, "-m", "pytest", *TEST_PATHS[suite], "-q", "-rfE",
                          f"--junitxml={junit}", "-o", f"junit_suite_name={label}"],
                         cwd=ROOT, env={**os.environ, **(env or {})})
    summary = {"label": label, "suite": suite, "exit_code": rc, "passed": rc == 0, "at": now(),
               "pipeline_execution_id": os.environ.get("PIPELINE_EXECUTION_ID"),
               **(junit_summary(junit) if junit.exists() else {"error": "no JUnit output"})}
    (RESULTS / f"{label}.json").write_text(json.dumps(summary, indent=2))
    upload_results(label, [junit, RESULTS / f"{label}.json"])
    say(f"{label}: {'PASS' if rc == 0 else 'FAIL'} ({summary.get('tests', '?')} tests, "
        f"{len(summary.get('failed', []))} failed)")
    return rc


def upload_results(label: str, files: list[Path]) -> None:
    exec_id, bucket = os.environ.get("PIPELINE_EXECUTION_ID"), cfg().artifacts_bucket
    if not (exec_id and bucket):
        return
    for f in files:
        if f.exists():
            s3().upload_file(str(f), bucket, f"results/{exec_id}/{f.name}")


# ---------------- S3 releases ----------------
def object_exists(key: str) -> bool:
    try:
        s3().head_object(Bucket=cfg().bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_release(build_dir: Path, rid: str) -> dict:
    """Upload once; release.json goes last and marks the release complete. Never overwrites."""
    check_release_id(rid)
    prefix = f"releases/{rid}/"
    if object_exists(prefix + "release.json"):
        say(f"{prefix} already complete; releases are immutable, reusing it")
        return {"release_id": rid, "reused": True}
    conf = json.loads((ROOT / "cicd" / "content-types.json").read_text())
    files = sorted((p for p in Path(build_dir).rglob("*") if p.is_file()),
                   key=lambda p: (p == Path(build_dir) / "release.json", str(p)))
    unknown = sorted({p.suffix for p in files if p.suffix.lower() not in conf["content_types"]})
    if unknown:
        raise SystemExit(f"no content type configured for {unknown}; add them to cicd/content-types.json")
    for f in files:
        ext = f.suffix.lower()
        cache = conf["cache_control"]["no_cache" if ext in conf["no_cache"] else "default"]
        s3().upload_file(str(f), cfg().bucket, prefix + f.relative_to(build_dir).as_posix(),
                         ExtraArgs={"ContentType": conf["content_types"][ext], "CacheControl": cache})
    say(f"uploaded {len(files)} objects to s3://{cfg().bucket}/{prefix}")
    return {"release_id": rid, "reused": False, "objects": len(files)}


# ---------------- CloudFront ----------------
def _origin(dist_config: dict) -> dict:
    items = dist_config["Origins"]["Items"]
    if len(items) != 1:
        raise RuntimeError(f"expected exactly one origin, found {len(items)}")
    return items[0]


def current_release() -> str:
    """'root' = pre-release layout (objects at the bucket root); otherwise the release id."""
    dc = cf().get_distribution_config(Id=cfg().distribution_id)["DistributionConfig"]
    path = _origin(dc).get("OriginPath", "")
    m = re.fullmatch(r"/releases/(.+)", path)
    return m.group(1) if m else (path or "root")


def wait_deployed() -> None:
    cf().get_waiter("distribution_deployed").wait(Id=cfg().distribution_id,
                                                 WaiterConfig={"Delay": 15, "MaxAttempts": 80})


def invalidate_all(tag: str) -> str:
    inv = cf().create_invalidation(DistributionId=cfg().distribution_id, InvalidationBatch={
        "Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": f"{tag}-{int(time.time())}"})
    inv_id = inv["Invalidation"]["Id"]
    cf().get_waiter("invalidation_completed").wait(DistributionId=cfg().distribution_id, Id=inv_id,
                                                  WaiterConfig={"Delay": 10, "MaxAttempts": 90})
    return inv_id


def set_origin_path(path: str) -> str:
    """Set the origin's OriginPath and wait for Deployed. Returns the previous path."""
    r = cf().get_distribution_config(Id=cfg().distribution_id)
    dc = r["DistributionConfig"]
    origin = _origin(dc)
    before = origin.get("OriginPath", "")
    if before != path:
        origin["OriginPath"] = path
        cf().update_distribution(Id=cfg().distribution_id, IfMatch=r["ETag"], DistributionConfig=dc)
        say(f"OriginPath {before or '(empty)'} -> {path or '(empty)'}; waiting for Deployed")
        wait_deployed()
    return before


def switch_release(rid: str) -> dict:
    """Point CloudFront at a complete release, wait for Deployed, invalidate /* and wait."""
    check_release_id(rid)
    if not object_exists(f"releases/{rid}/release.json"):
        raise ValueError(f"releases/{rid}/ is missing or incomplete; refusing to switch")
    after = f"/releases/{rid}"
    before = set_origin_path(after)
    inv = invalidate_all(f"release-{rid[:12]}")
    say(f"live release is now {rid} (invalidation {inv} complete)")
    return {"from_origin_path": before, "to_origin_path": after, "invalidation_id": inv}


# ---------------- history ----------------
def read_history() -> tuple[dict, str | None]:
    try:
        r = s3().get_object(Bucket=cfg().bucket, Key=HISTORY_KEY)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return {"releases": []}, None
        raise
    return json.loads(r["Body"].read()), r["ETag"]


def update_history(mutate, attempts: int = 5) -> dict:
    """Read-modify-write with S3 conditional writes. QUEUED pipeline mode already serializes
    pipeline runs; the ETag check protects against an agent rollback writing at the same time."""
    for _ in range(attempts):
        data, etag = read_history()
        mutate(data)
        cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            s3().put_object(Bucket=cfg().bucket, Key=HISTORY_KEY, ContentType="application/json",
                            Body=json.dumps(data, indent=2).encode(), **cond)
            return data
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise
            time.sleep(1)
    raise RuntimeError("history.json kept changing; giving up")


def record(rid: str, status: str, **detail) -> dict:
    check_release_id(rid)

    def mutate(data):
        entry = next((e for e in data["releases"] if e["release_id"] == rid), None)
        if entry is None:
            entry = {"release_id": rid, "short": rid[:7], "events": []}
            data["releases"].append(entry)
        entry["status"], entry["updated_at"] = status, now()
        entry["events"].append({"status": status, "at": entry["updated_at"], **detail})
    say(f"history: {rid[:12]} -> {status}")
    return update_history(mutate)


def known_good(exclude: str | None = None) -> str | None:
    """Newest release whose status is verified and whose files still exist."""
    data, _ = read_history()
    last_verified = lambda e: max((ev["at"] for ev in e["events"] if ev["status"] == "verified"), default="")
    candidates = sorted((ie for ie in enumerate(data["releases"]) if ie[1].get("status") == "verified"
                         and ie[1]["release_id"] != exclude and RELEASE_ID.fullmatch(ie[1]["release_id"])),
                        key=lambda ie: (last_verified(ie[1]), ie[0]), reverse=True)   # ties: later entry wins
    return next((e["release_id"] for _, e in candidates
                 if object_exists(f"releases/{e['release_id']}/release.json")), None)


def rollback_to_known_good(failed: str | None = None) -> dict:
    """Bounded rollback: only ever switches to an existing, verified release. Does not record
    history; callers record the outcome after re-verifying."""
    failed = failed or current_release()
    target = known_good(exclude=failed)
    if not target:
        raise LookupError("no verified release available to roll back to")
    say(f"rolling back {failed} -> {target}")
    return {"from": failed, "to": target, **switch_release(target)}


# ---------------- pipeline stages ----------------
def deploy(artifact: Path) -> int:
    sha = read_artifact_release(artifact)
    upload_release(artifact, sha)
    sw = switch_release(sha)
    record(sha, "deployed", execution_id=os.environ.get("PIPELINE_EXECUTION_ID"), **sw)
    return 0


def verify_or_rollback(artifact: Path) -> int:
    sha = read_artifact_release(artifact)
    exec_id = os.environ.get("PIPELINE_EXECUTION_ID")
    live = current_release()
    if live != sha:
        record(sha, "failed", execution_id=exec_id, reason=f"live release is {live}, not this one")
        say("not live; leaving production untouched")
        return 1
    if run_tests("postdeploy", env={"EXPECTED_RELEASE": sha}) == 0:
        record(sha, "verified", execution_id=exec_id)
        return 0
    record(sha, "failed", execution_id=exec_id, results=f"results/{exec_id}/postdeploy.json")
    try:
        rb = rollback_to_known_good(failed=sha)
    except LookupError as e:
        say(f"ROLLBACK IMPOSSIBLE: {e}")
        return 1
    ok = run_tests("postdeploy", label="postdeploy-after-rollback", env={"EXPECTED_RELEASE": rb["to"]}) == 0
    record(sha, "rolled_back" if ok else "rollback_failed", execution_id=exec_id, rolled_back_to=rb["to"])
    say(f"rolled back to {rb['to']}; re-verification {'PASSED' if ok else 'FAILED'}")
    return 1          # the release itself failed, so the execution must show Failed


def status() -> dict:
    data, _ = read_history()
    return {"live_release": current_release(), "known_good": known_good(),
            "recent": [{k: e.get(k) for k in ("release_id", "status", "updated_at")}
                       for e in data["releases"][-10:]]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("test").add_argument("suite", choices=list(TEST_PATHS))
    for name in ("deploy", "verify-or-rollback"):
        sub.add_parser(name).add_argument("--artifact", type=Path, required=True)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "build":
        say(f"built {build(expected_sha())}")
        return 0
    if a.cmd == "test":
        return run_tests(a.suite)
    if a.cmd == "deploy":
        return deploy(a.artifact)
    if a.cmd == "verify-or-rollback":
        return verify_or_rollback(a.artifact)
    print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
