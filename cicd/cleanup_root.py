#!/usr/bin/env python3
"""One-time: delete the legacy Project 5 objects at the bucket root (no longer served).

  AWS_PROFILE=csce412-admin python cicd/cleanup_root.py

Refuses unless CloudFront serves a verified pipeline release (not the root, not the baseline) and
releases/p5-baseline/ still holds a complete copy of the root objects. Never touches releases/ or
deployments/. Asks before deleting.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cicd import release as R  # noqa: E402
from cicd.seed_baseline import PREFIX, root_objects  # noqa: E402


def main() -> int:
    live = R.current_release()
    data, _ = R.read_history()
    verified = {e["release_id"] for e in data["releases"] if e["status"] == "verified"}
    if live in ("root", "p5-baseline") or live not in verified:
        sys.exit(f"live release is {live}; cleanup needs a verified pipeline release to be live")
    if not R.object_exists(PREFIX + "release.json"):
        sys.exit(f"{PREFIX} is incomplete; keep the root objects")
    keys = root_objects()
    missing = [k for k in keys if not R.object_exists(PREFIX + k)]
    if missing:
        sys.exit(f"not in the baseline copy, refusing: {missing}")
    if not keys:
        R.say("no root objects left; nothing to do")
        return 0
    print(f"Live release: {live} (verified). Will delete {len(keys)} root objects from "
          f"s3://{R.cfg().bucket}/ (copies remain in {PREFIX}):\n  " + "\n  ".join(keys))
    if input("Delete them? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1
    r = R.s3().delete_objects(Bucket=R.cfg().bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
    if r.get("Errors"):
        sys.exit(f"some deletes failed: {r['Errors']}")
    R.say(f"deleted {len(keys)} root objects; production is unaffected (served from releases/{live}/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
