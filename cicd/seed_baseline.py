#!/usr/bin/env python3
"""One-time: turn the live Project 5 site (objects at the bucket root) into release p5-baseline,
so the very first pipeline release has a verified release to roll back to.

  AWS_PROFILE=csce412-admin python cicd/seed_baseline.py

1. Server-side copy every root object to releases/p5-baseline/ (metadata kept), then write
   releases/p5-baseline/release.json last (completion marker).
2. Switch CloudFront OriginPath to /releases/p5-baseline, wait for Deployed, invalidate /*.
3. Run the post-deploy suite with EXPECTED_RELEASE=p5-baseline.
4. Pass: record p5-baseline as verified. Fail: OriginPath back to the root (production exactly
   as before) and nothing is recorded.
Root objects are never modified or deleted. Safe to re-run.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cicd import release as R  # noqa: E402

BASE = "p5-baseline"
PREFIX = f"releases/{BASE}/"
SKIP = ("releases/", "deployments/")


def root_objects() -> list[str]:
    keys = []
    for page in R.s3().get_paginator("list_objects_v2").paginate(Bucket=R.cfg().bucket):
        keys += [o["Key"] for o in page.get("Contents", []) if not o["Key"].startswith(SKIP)]
    return keys


def copy_baseline(keys: list[str]) -> None:
    bucket = R.cfg().bucket
    for key in keys:
        R.s3().copy_object(Bucket=bucket, Key=PREFIX + key, CopySource={"Bucket": bucket, "Key": key},
                           MetadataDirective="COPY")
    commit = subprocess.check_output(["git", "rev-parse", "p5-final^{commit}"], cwd=R.ROOT, text=True).strip()
    body = {"release_id": BASE, "commit": commit, "short": commit[:7], "built_at": R.now(),
            "note": "server-side copy of the Project 5 objects at the bucket root"}
    R.s3().put_object(Bucket=bucket, Key=PREFIX + "release.json", Body=json.dumps(body, indent=2).encode(),
                      ContentType="application/json", CacheControl="no-cache")
    R.say(f"copied {len(keys)} objects to {PREFIX} (+ release.json)")


def main() -> int:
    live = R.current_release()
    data, _ = R.read_history()
    if any(e["release_id"] == BASE and e["status"] == "verified" for e in data["releases"]):
        R.say(f"{BASE} is already recorded as verified; nothing to do (live: {live})")
        return 0
    if live not in ("root", BASE):
        sys.exit(f"live release is {live}; the baseline is only for the Project 5 root layout")
    keys = [] if R.object_exists(PREFIX + "release.json") else root_objects()
    if not keys and not R.object_exists(PREFIX + "release.json"):
        sys.exit("no root objects found; nothing to seed")
    if keys and "index.html" not in keys:
        sys.exit("no index.html at the bucket root; refusing to seed")

    print(f"""
This will:
  - copy {len(keys) or 'no (already copied)'} root objects to s3://{R.cfg().bucket}/{PREFIX}
  - set CloudFront {R.cfg().distribution_id} OriginPath to /{PREFIX.rstrip('/')} and invalidate /*
  - run the post-deploy suite; on pass record {BASE} as verified, on failure revert OriginPath to the root
Root objects are not changed.""")
    if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1

    if keys:
        copy_baseline(keys)
    R.switch_release(BASE)
    if R.run_tests("postdeploy", label="baseline", env={"EXPECTED_RELEASE": BASE}) == 0:
        R.record(BASE, "verified", note="seeded from Project 5 root objects")
        R.say(f"{BASE} is live and verified. You can now create the pipeline.")
        return 0
    R.say("baseline verification FAILED; reverting OriginPath to the bucket root")
    R.set_origin_path("")
    R.invalidate_all("revert-baseline")
    R.say("production is back on the root objects; see build/results/baseline.json")
    return 1


if __name__ == "__main__":
    sys.exit(main())
