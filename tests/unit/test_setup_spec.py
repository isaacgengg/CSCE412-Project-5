"""Safety properties of the pipeline definition in cicd/setup_pipeline.py (no AWS calls)."""
import json

from cicd.setup_pipeline import build_spec

ARN = "arn:aws:codeconnections:us-east-2:408588283693:connection/example"
SPEC = build_spec(ARN)


def test_stage_order_puts_test_gate_before_deploy():
    assert [s["name"] for s in SPEC["pipeline"]["stages"]] == ["Source", "Test", "Deploy", "Verify"]
    assert SPEC["pipeline"]["pipelineType"] == "V2" and SPEC["pipeline"]["executionMode"] == "QUEUED"


def test_deploy_and_verify_use_the_tested_artifact():
    stages = {s["name"]: s["actions"][0] for s in SPEC["pipeline"]["stages"]}
    assert stages["Test"]["outputArtifacts"] == [{"name": "BuildArtifact"}]
    for name in ("Deploy", "Verify"):
        assert {"name": "BuildArtifact"} in stages[name]["inputArtifacts"]
        assert stages[name]["configuration"]["PrimarySource"] == "SourceArtifact"


def test_every_build_gets_expected_sha_and_execution_id():
    for stage in SPEC["pipeline"]["stages"][1:]:
        env = {v["name"]: v["value"] for v in json.loads(stage["actions"][0]["configuration"]["EnvironmentVariables"])}
        assert env == {"EXPECTED_SHA": "#{SourceVariables.CommitId}",
                       "PIPELINE_EXECUTION_ID": "#{codepipeline.PipelineExecutionId}"}


def test_logs_go_to_s3_not_cloudwatch():
    for p in SPEC["projects"].values():
        assert p["logsConfig"]["cloudWatchLogs"]["status"] == "DISABLED"
        assert p["logsConfig"]["s3Logs"]["status"] == "ENABLED"


def test_no_wildcard_resources_or_actions():
    for role in SPEC["roles"].values():
        for s in role["policy"]["Statement"]:
            resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
            assert "*" not in resources, (role["RoleName"], s["Sid"])
            assert not any(a.endswith(":*") for a in s["Action"]), (role["RoleName"], s["Sid"])


def test_only_deploy_and_verify_can_touch_the_site():
    def actions(k):
        return {a for s in SPEC["roles"][k]["policy"]["Statement"] for a in s["Action"]}
    assert not any(a.startswith("cloudfront:") for a in actions("test") | actions("pipeline"))
    put = lambda k: [r for s in SPEC["roles"][k]["policy"]["Statement"] if "s3:PutObject" in s["Action"]
                     for r in s["Resource"] if "csce412isaacgeng-site" in r]
    assert put("test") == [] and put("pipeline") == []
    assert sorted(put("verify")) == ["arn:aws:s3:::csce412isaacgeng-site/deployments/history.json"]
