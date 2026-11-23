"""tools/cicd_tools.py with fake AWS clients."""
import copy
import gzip
import io

from cicd.setup_pipeline import build_spec
from core.config import load_config
from tools import cicd_tools
from tools.cicd_tools import CICD

ARN = "arn:aws:codeconnections:us-east-2:408588283693:connection/example"


class Fake:
    def __init__(self, **methods):
        self.__dict__.update(methods)


def make(**clients):
    c = CICD(load_config())
    c.__dict__.update(clients)          # cached_property reads instance __dict__ first
    return c


def test_summarize_action_extracts_build_and_failure():
    a = {"stageName": "Verify", "actionName": "Verify", "status": "Failed",
         "output": {"executionResult": {"externalExecutionId": "csce412-site-verify:abc",
                                        "externalExecutionSummary": "Build terminated with state: FAILED"}}}
    s = cicd_tools.summarize_action(a)
    assert (s["stage"], s["status"], s["build_id"]) == ("Verify", "Failed", "csce412-site-verify:abc")
    assert "FAILED" in s["summary"]


def test_pipeline_drift_matches_then_detects_change():
    spec = build_spec(ARN)
    live = copy.deepcopy(spec["pipeline"]) | {"version": 3}
    projects = [{**p, "source": {**p["source"], "gitCloneDepth": 1}} for p in spec["projects"].values()]
    cp = Fake(get_pipeline=lambda name: {"pipeline": live})
    cb = Fake(batch_get_projects=lambda names: {"projects": projects})
    c = make(cp=cp, cb=cb)
    assert c.pipeline_drift() == {"matches_repo": True, "differences": []}

    live["executionMode"] = "SUPERSEDED"
    projects[0]["logsConfig"] = {"cloudWatchLogs": {"status": "ENABLED"}, "s3Logs": {"status": "DISABLED"}}
    d = c.pipeline_drift()
    assert not d["matches_repo"]
    assert any("executionMode" in x for x in d["differences"])
    assert any("cloudWatchLogs" in x for x in d["differences"])


def test_build_log_reads_gzipped_s3_log_and_filters():
    log = "line one\n[release] predeploy: FAIL\nAssertionError: broken link /x.html\nsecret=abcdefghijklmnopqrstuvwxyz\n"
    cb = Fake(batch_get_builds=lambda ids: {"builds": [{
        "id": ids[0], "projectName": "csce412-site-test", "buildStatus": "FAILED",
        "logs": {"s3LogsArn": "arn:aws:s3:::csce412isaacgeng-pipeline-artifacts/codebuild-logs/abc.gz"}}]})
    seen = {}

    def get_object(Bucket, Key):
        seen.update(bucket=Bucket, key=Key)
        return {"Body": io.BytesIO(gzip.compress(log.encode()))}

    c = make(cb=cb, s3=Fake(get_object=get_object))
    r = c.build_log("csce412-site-test:abc", grep="fail|assert")
    assert seen == {"bucket": "csce412isaacgeng-pipeline-artifacts", "key": "codebuild-logs/abc.gz"}
    assert r["lines"] == ["[release] predeploy: FAIL", "AssertionError: broken link /x.html"]
    assert "REDACTED" in c.build_log("csce412-site-test:abc", grep="secret")["lines"][0]


def test_read_repo_file_is_allowlisted():
    c = make()
    assert "error" in c.read_repo_file(".env")
    assert "error" in c.read_repo_file("../../etc/passwd")
    assert c.read_repo_file("cicd/buildspec-test.yml")["total_lines"] > 5


def test_git_commit_validates_sha():
    assert "error" in make().git_commit("HEAD; rm -rf /")
