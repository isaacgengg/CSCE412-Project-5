#!/usr/bin/env python3
"""Human-run controlled failure injection (Part E). Not used by the agents.

Run with YOUR admin profile, not the agent profile:
  AWS_PROFILE=default python scripts/inject_failure.py list
  AWS_PROFILE=default python scripts/inject_failure.py inject bucket-policy
  AWS_PROFILE=default python scripts/inject_failure.py revert     # manual fallback only

Every injection saves the previous state to .failure_backup.json so it is reversible,
and invalidates the CloudFront cache so the failure is visible immediately
(otherwise edges keep serving cached pages and nothing appears broken).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ROOT, load_config  # noqa: E402
from tools.aws_tools import AWS  # noqa: E402

BACKUP = ROOT / ".failure_backup.json"

SCENARIOS = {
    "bucket-policy": "Delete the bucket policy: CloudFront (OAC) can no longer read S3 -> 403 on every page.",
    "http-allowed": "Viewer protocol policy -> allow-all: HTTP is served instead of redirecting to HTTPS.",
    "root-object": "Clear the default root object: https://domain/ fails while /index.html still works.",
}


def inject(aws: AWS, name: str) -> None:
    if BACKUP.exists():
        sys.exit(f"{BACKUP.name} exists; a failure is already injected. Recover or revert first.")
    cf = aws.cloudfront_summary()
    backup = {"scenario": name}
    if name == "bucket-policy":
        backup["policy"] = aws.s3_bucket_policy().get("policy")
        aws.s3.delete_bucket_policy(Bucket=aws.cfg.bucket)
    elif name == "http-allowed":
        backup["viewer_protocol_policy"] = cf["viewer_protocol_policy"]
        aws.set_viewer_protocol_policy("allow-all")
    elif name == "root-object":
        backup["default_root_object"] = cf["default_root_object"]
        aws.set_default_root_object("")
    else:
        sys.exit(f"unknown scenario {name}; choose from {list(SCENARIOS)}")
    BACKUP.write_text(json.dumps(backup, indent=2))
    if name != "bucket-policy":
        print("Waiting for CloudFront to deploy the change...")
        print(aws.cloudfront_wait_deployed())
    print(aws.cloudfront_invalidate(["/*"]))
    print(f"Injected '{name}': {SCENARIOS[name]}")
    print("Now run: python orchestrator.py verify")


def revert(aws: AWS) -> None:
    if not BACKUP.exists():
        sys.exit("nothing to revert")
    b = json.loads(BACKUP.read_text())
    if b["scenario"] == "bucket-policy" and b.get("policy"):
        aws.s3.put_bucket_policy(Bucket=aws.cfg.bucket, Policy=json.dumps(b["policy"]))
    elif b["scenario"] == "http-allowed":
        aws.set_viewer_protocol_policy(b["viewer_protocol_policy"])
    elif b["scenario"] == "root-object":
        aws.set_default_root_object(b["default_root_object"])
    aws.cloudfront_invalidate(["/*"])
    BACKUP.unlink()
    print(f"Reverted '{b['scenario']}'")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "inject", "revert", "clear"):
        sys.exit(__doc__)
    if sys.argv[1] == "list":
        for k, v in SCENARIOS.items():
            print(f"{k:15} {v}")
        return
    if sys.argv[1] == "clear":           # after the agents have fixed it
        BACKUP.unlink(missing_ok=True)
        print("backup cleared")
        return
    aws = AWS(load_config())
    if sys.argv[1] == "inject":
        inject(aws, sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        revert(aws)


if __name__ == "__main__":
    main()
