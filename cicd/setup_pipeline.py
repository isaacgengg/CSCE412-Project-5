#!/usr/bin/env python3
"""One-time, idempotent setup of the CI/CD pipeline (run by a human with the admin profile).

  AWS_PROFILE=csce412-admin python cicd/setup_pipeline.py --connection-arn ARN --dry-run
  AWS_PROFILE=csce412-admin python cicd/setup_pipeline.py --connection-arn ARN

Creates or updates, in the site's region (us-east-2):
  - S3 bucket <artifacts_bucket>: pipeline artifact store, CodeBuild logs, test results
  - CodeBuild report group csce412-site-tests
  - IAM roles: csce412-codepipeline, csce412-codebuild-{test,deploy,verify} (inline policies)
  - CodeBuild projects csce412-site-{test,deploy,verify} (CloudWatch logs off, S3 logs on)
  - CodePipeline V2 csce412-site-pipeline (QUEUED, push to main triggers it)

The GitHub connection is created and authorized by a human in the console; pass its ARN.
Creating a pipeline immediately releases main, so the pipeline is only created once
seed_baseline.py has recorded a verified release to roll back to.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cicd import release as R  # noqa: E402

REPO = "isaacgengg/CSCE412-Project-5"
BRANCH = "main"
PIPELINE = "csce412-site-pipeline"
REPORT_GROUP = "csce412-site-tests"
IMAGE = "aws/codebuild/standard:7.0"
STAGES = ("test", "deploy", "verify")


def st(sid, actions, resources, condition=None) -> dict:
    s = {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resources}
    if condition:
        s["Condition"] = condition
    return s


def doc(*statements) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def trust(service: str) -> dict:
    return doc({"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"})


def build_spec(connection_arn: str) -> dict:
    """Everything this script manages, as plain data (printed by --dry-run)."""
    c = R.cfg()
    region, account = c.region, connection_arn.split(":")[4]
    art, site = f"arn:aws:s3:::{c.artifacts_bucket}", f"arn:aws:s3:::{c.bucket}"
    report_group = f"arn:aws:codebuild:{region}:{account}:report-group/{REPORT_GROUP}"
    project = {s: f"csce412-site-{s}" for s in STAGES}
    project_arns = [f"arn:aws:codebuild:{region}:{account}:project/{n}" for n in project.values()]
    role = {s: f"csce412-codebuild-{s}" for s in STAGES} | {"pipeline": "csce412-codepipeline"}
    role_arn = {k: f"arn:aws:iam::{account}:role/{v}" for k, v in role.items()}
    other_prefix = (connection_arn.replace(":codeconnections:", ":codestar-connections:")
                    if ":codeconnections:" in connection_arn
                    else connection_arn.replace(":codestar-connections:", ":codeconnections:"))

    # ACLs are disabled on the bucket (BucketOwnerEnforced); PutObjectAcl only lets clients that
    # still send bucket-owner-full-control succeed.
    artifacts_rw = [st("ArtifactsObjects", ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
                                            "s3:PutObjectAcl"], [f"{art}/*"]),
                    st("ArtifactsBucket", ["s3:GetBucketAcl", "s3:GetBucketLocation"], [art])]
    reports = st("TestReports", ["codebuild:CreateReport", "codebuild:UpdateReport",
                                 "codebuild:BatchPutTestCases"], [report_group])
    site_list = st("SiteList", ["s3:ListBucket"], [site])          # 404 (not 403) for missing keys
    history = st("History", ["s3:GetObject", "s3:PutObject"], [f"{site}/{R.HISTORY_KEY}"])
    cloudfront = st("CloudFrontReleaseSwitch", [
        "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution",
        "cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"],
        [f"arn:aws:cloudfront::{account}:distribution/{c.distribution_id}"])

    policies = {
        "pipeline": doc(
            st("ArtifactsObjects", ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:PutObjectAcl"],
               [f"{art}/*"]),
            st("ArtifactsBucket", ["s3:GetBucketVersioning", "s3:GetBucketLocation"], [art]),
            st("RunBuilds", ["codebuild:StartBuild", "codebuild:BatchGetBuilds"], project_arns),
            st("GitHubConnection", ["codeconnections:UseConnection", "codestar-connections:UseConnection"],
               sorted({connection_arn, other_prefix}))),
        "test": doc(*artifacts_rw, reports),
        "deploy": doc(*artifacts_rw, site_list, history, cloudfront,
                      st("ReleasesWrite", ["s3:GetObject", "s3:PutObject"], [f"{site}/releases/*"])),
        "verify": doc(*artifacts_rw, reports, site_list, history, cloudfront,
                      st("ReleasesRead", ["s3:GetObject"], [f"{site}/releases/*"]),
                      st("SiteSecurityRead", ["s3:GetBucketPublicAccessBlock", "s3:GetBucketLocation"], [site])),
    }
    roles = {k: {"RoleName": role[k], "trust": trust("codepipeline.amazonaws.com" if k == "pipeline"
                                                      else "codebuild.amazonaws.com"),
                 "policy": policies[k]} for k in role}

    projects = {s: {
        "name": project[s],
        "description": f"CSCE 412 site pipeline: {s} stage",
        "source": {"type": "CODEPIPELINE", "buildspec": f"cicd/buildspec-{s}.yml"},
        "artifacts": {"type": "CODEPIPELINE"},
        "environment": {"type": "LINUX_CONTAINER", "image": IMAGE, "computeType": "BUILD_GENERAL1_SMALL",
                        "privilegedMode": False},
        "serviceRole": role_arn[s],
        "timeoutInMinutes": 20 if s == "test" else 60,     # CloudFront waits (x2 on rollback)
        "logsConfig": {"cloudWatchLogs": {"status": "DISABLED"},
                       "s3Logs": {"status": "ENABLED", "location": f"{c.artifacts_bucket}/codebuild-logs"}},
    } for s in STAGES}

    env_vars = json.dumps([
        {"name": "EXPECTED_SHA", "value": "#{SourceVariables.CommitId}", "type": "PLAINTEXT"},
        {"name": "PIPELINE_EXECUTION_ID", "value": "#{codepipeline.PipelineExecutionId}", "type": "PLAINTEXT"}])

    def build_action(s, inputs, outputs=()):
        conf = {"ProjectName": project[s], "EnvironmentVariables": env_vars}
        if len(inputs) > 1:
            conf["PrimarySource"] = "SourceArtifact"
        return {"name": s.capitalize(), "runOrder": 1, "configuration": conf,
                "actionTypeId": {"category": "Test" if s == "verify" else "Build",
                                 "owner": "AWS", "provider": "CodeBuild", "version": "1"},
                "inputArtifacts": [{"name": n} for n in inputs],
                "outputArtifacts": [{"name": n} for n in outputs]}

    pipeline = {
        "name": PIPELINE, "pipelineType": "V2", "executionMode": "QUEUED", "roleArn": role_arn["pipeline"],
        "artifactStore": {"type": "S3", "location": c.artifacts_bucket},
        "stages": [
            {"name": "Source", "actions": [{
                "name": "Source", "namespace": "SourceVariables", "runOrder": 1,
                "actionTypeId": {"category": "Source", "owner": "AWS", "provider": "CodeStarSourceConnection",
                                 "version": "1"},
                "configuration": {"ConnectionArn": connection_arn, "FullRepositoryId": REPO,
                                  "BranchName": BRANCH, "OutputArtifactFormat": "CODE_ZIP"},
                "outputArtifacts": [{"name": "SourceArtifact"}]}]},
            {"name": "Test", "actions": [build_action("test", ["SourceArtifact"], ["BuildArtifact"])]},
            {"name": "Deploy", "actions": [build_action("deploy", ["SourceArtifact", "BuildArtifact"])]},
            {"name": "Verify", "actions": [build_action("verify", ["SourceArtifact", "BuildArtifact"])]},
        ],
        "triggers": [{"providerType": "CodeStarSourceConnection", "gitConfiguration": {
            "sourceActionName": "Source", "push": [{"branches": {"includes": [BRANCH]}}]}}],
    }
    bucket = {"Bucket": c.artifacts_bucket, "region": region,
              "public_access_block": "all four settings on", "ownership": "BucketOwnerEnforced",
              "encryption": "SSE-S3 (AES256)",
              "policy": doc({"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*",
                             "Action": "s3:*", "Resource": [art, f"{art}/*"],
                             "Condition": {"Bool": {"aws:SecureTransport": "false"}}})}
    return {"bucket": bucket, "report_group": {"name": REPORT_GROUP, "arn": report_group},
            "roles": roles, "projects": projects, "pipeline": pipeline}


# ---------------- apply (idempotent) ----------------
def say(msg):
    print(f"[setup] {msg}", flush=True)


def ensure_bucket(s3, b: dict) -> None:
    try:
        s3.head_bucket(Bucket=b["Bucket"])
        say(f"bucket {b['Bucket']} exists")
    except ClientError:
        s3.create_bucket(Bucket=b["Bucket"], CreateBucketConfiguration={"LocationConstraint": b["region"]})
        say(f"created bucket {b['Bucket']}")
    s3.put_public_access_block(Bucket=b["Bucket"], PublicAccessBlockConfiguration=dict(
        BlockPublicAcls=True, IgnorePublicAcls=True, BlockPublicPolicy=True, RestrictPublicBuckets=True))
    s3.put_bucket_ownership_controls(Bucket=b["Bucket"], OwnershipControls={
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    s3.put_bucket_encryption(Bucket=b["Bucket"], ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_policy(Bucket=b["Bucket"], Policy=json.dumps(b["policy"]))


def ensure_role(iam, r: dict) -> None:
    try:
        iam.get_role(RoleName=r["RoleName"])
        iam.update_assume_role_policy(RoleName=r["RoleName"], PolicyDocument=json.dumps(r["trust"]))
        say(f"role {r['RoleName']} exists; trust policy refreshed")
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=r["RoleName"], AssumeRolePolicyDocument=json.dumps(r["trust"]),
                        Description="CSCE 412 Project 6 pipeline (managed by cicd/setup_pipeline.py)")
        say(f"created role {r['RoleName']}")
    iam.put_role_policy(RoleName=r["RoleName"], PolicyName="least-privilege",
                        PolicyDocument=json.dumps(r["policy"]))


def retry_iam(fn, what: str):
    """New IAM roles take a few seconds to become assumable by CodeBuild/CodePipeline."""
    for attempt in range(8):
        try:
            return fn()
        except ClientError as e:
            msg = str(e)
            if attempt < 7 and ("role" in msg.lower() or "assume" in msg.lower()) and \
                    e.response["Error"]["Code"] in ("InvalidInputException", "InvalidStructureException"):
                say(f"{what}: waiting for IAM propagation ({e.response['Error']['Code']})")
                time.sleep(10)
                continue
            raise


def ensure_report_group(cb, name: str) -> None:
    try:
        cb.create_report_group(name=name, type="TEST", exportConfig={"exportConfigType": "NO_EXPORT"})
        say(f"created report group {name}")
    except cb.exceptions.ResourceAlreadyExistsException:
        say(f"report group {name} exists")


def ensure_project(cb, p: dict) -> None:
    exists = bool(cb.batch_get_projects(names=[p["name"]])["projects"])
    retry_iam(lambda: (cb.update_project if exists else cb.create_project)(**p), p["name"])
    say(f"{'updated' if exists else 'created'} CodeBuild project {p['name']}")


def ensure_pipeline(cp, p: dict) -> None:
    try:
        cp.get_pipeline(name=p["name"])
        retry_iam(lambda: cp.update_pipeline(pipeline=p), p["name"])
        say(f"updated pipeline {p['name']} (no execution started)")
        return
    except cp.exceptions.PipelineNotFoundException:
        pass
    if not R.known_good():
        sys.exit("no verified release in history.json; run cicd/seed_baseline.py first "
                 "(creating the pipeline immediately releases main)")
    retry_iam(lambda: cp.create_pipeline(pipeline=p), p["name"])
    say(f"created pipeline {p['name']}; its first execution (a release of {BRANCH}) has started")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connection-arn", required=True,
                    help="arn:aws:codeconnections:us-east-2:<account>:connection/<id> (status AVAILABLE)")
    ap.add_argument("--dry-run", action="store_true", help="print every resource definition and exit")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a = ap.parse_args()
    spec = build_spec(a.connection_arn)
    if a.dry_run:
        print(json.dumps(spec, indent=2))
        return 0
    region = R.cfg().region
    print(f"Will create/update in {region}: bucket {spec['bucket']['Bucket']}, report group {REPORT_GROUP}, "
          f"roles {[r['RoleName'] for r in spec['roles'].values()]}, projects "
          f"{[p['name'] for p in spec['projects'].values()]}, pipeline {PIPELINE}.")
    if not a.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1
    ensure_bucket(boto3.client("s3", region_name=region), spec["bucket"])
    iam = boto3.client("iam")
    for r in spec["roles"].values():
        ensure_role(iam, r)
    cb = boto3.client("codebuild", region_name=region)
    ensure_report_group(cb, REPORT_GROUP)
    for p in spec["projects"].values():
        ensure_project(cb, p)
    ensure_pipeline(boto3.client("codepipeline", region_name=region), spec["pipeline"])
    say(f"console: https://{region}.console.aws.amazon.com/codesuite/codepipeline/pipelines/{PIPELINE}/view")
    return 0


if __name__ == "__main__":
    sys.exit(main())
