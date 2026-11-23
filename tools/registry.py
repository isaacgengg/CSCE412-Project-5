"""Builds the Tool objects each agent is allowed to use.

The allowlist is the security boundary: an agent can only do what its tools do,
and anything that changes infrastructure is marked consequential (human approval).
"""
from __future__ import annotations
from functools import partial

from core.agent import Tool, no_params, with_rationale
from core.config import Config
from tools import live_checks, site_tools
from cicd import release
from tools.aws_tools import AWS
from tools.cicd_tools import CICD, REPO_FILES


def site_tools_for(cfg: Config) -> list[Tool]:
    return [
        Tool("inventory_site", "List every page in the local site with title, headings, nav links, scripts, images.",
             no_params(), partial(site_tools.inventory_site, cfg.site_dir)),
        Tool("check_links", "Check that every local link, script, stylesheet and image reference resolves to a file.",
             no_params(), partial(site_tools.check_links, cfg.site_dir)),
        Tool("check_site_requirements", "Evaluate the site against the Part A requirements and return facts.",
             no_params(), partial(site_tools.check_site_requirements, cfg.site_dir, cfg.required_pages)),
    ]


def probe_tools(cfg: Config, aws: AWS) -> list[Tool]:
    fn = live_checks.probe_functions(aws)
    return [
        Tool("http_get", "HTTP GET a URL; returns status, redirect info and key headers.",
             {"type": "object", "properties": {
                 "url": {"type": "string"},
                 "follow_redirects": {"type": "boolean", "default": False}},
              "required": ["url"]}, fn["http_get"]),
        Tool("http_get_json", "HTTP GET a JSON document (e.g. https://domain/release.json) and return it parsed.",
             {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
             fn["http_get_json"]),
        Tool("tls_certificate", "Open a TLS connection and return the validated certificate details.",
             {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
             fn["tls_certificate"]),
        Tool("dns_resolve", "Resolve a DNS name (A, AAAA, NS, CNAME).",
             {"type": "object", "properties": {
                 "name": {"type": "string"},
                 "record_type": {"type": "string", "enum": ["A", "AAAA", "NS", "CNAME"]}},
              "required": ["name"]}, fn["dns_resolve"]),
        Tool("s3_direct_access", "Request an object directly from the S3 bucket endpoint without credentials "
             "(releases live under releases/<release_id>/).",
             {"type": "object", "properties": {"key": {"type": "string", "default": "index.html"}}},
             fn["s3_direct_access"]),
        Tool("jsdom_filter_check", "Run projects.html + js/main.js from a URL or local directory in jsdom, "
             "click a project filter, and report whether cards and the count update.",
             {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
             fn["jsdom_filter_check"]),
    ]


def aws_read_tools(aws: AWS) -> list[Tool]:
    return [
        Tool("cloudfront_summary", "Read the CloudFront distribution config (aliases, origins/OAC, "
             "viewer protocol policy, root object, certificate, error pages).", no_params(), aws.cloudfront_summary),
        Tool("s3_public_access_block", "Read the bucket's Block Public Access settings.", no_params(),
             aws.s3_public_access_block),
        Tool("s3_bucket_policy", "Read the bucket policy and whether AWS considers it public.", no_params(),
             aws.s3_bucket_policy),
        Tool("s3_website_hosting", "Check whether S3 static website hosting is enabled on the bucket.", no_params(),
             aws.s3_website_hosting),
        Tool("s3_list_objects", "Summarize the website bucket: object counts per releases/<id>/, "
             "deployments/, and legacy root objects.", no_params(), aws.s3_list_objects),
        Tool("acm_certificate", "Describe the ACM certificate attached to the distribution.", no_params(),
             aws.acm_certificate),
        Tool("route53_records", "List DNS records in the domain's hosted zone.", no_params(), aws.route53_records),
        Tool("release_status", "Live release (from CloudFront OriginPath), newest verified (known-good) release, "
             "and recent release history.", no_params(), lambda: {**release.status(),
                                                                  "history": release.read_history()[0]["releases"][-8:]}),
    ]


# Fix actions the Diagnoser may propose. Each maps to one consequential tool.
FIX_ACTIONS = {
    "restore_oac_bucket_policy": "Re-apply the bucket policy that lets only this CloudFront distribution read objects.",
    "enforce_public_access_block": "Turn on all four S3 Block Public Access settings.",
    "set_viewer_protocol_policy": "Set the default behavior's viewer protocol policy (use redirect-to-https).",
    "set_default_root_object": "Set the distribution's default root object (normally index.html).",
    "upsert_alias_records": "Create or repair A/AAAA alias records pointing the domain at CloudFront.",
    "rollback_to_known_good": "Restore the newest verified release as a whole by switching CloudFront's "
                              "OriginPath back to it (bounded: only existing, verified releases).",
}


def aws_write_tools(aws: AWS) -> dict[str, Tool]:
    return {
        "rollback_to_known_good": Tool("rollback_to_known_good", FIX_ACTIONS["rollback_to_known_good"] +
                                       " Waits for CloudFront to deploy and invalidates /*.",
                                       with_rationale(no_params()), lambda: release.rollback_to_known_good(),
                                       consequential=True),
        "restore_oac_bucket_policy": Tool("restore_oac_bucket_policy", FIX_ACTIONS["restore_oac_bucket_policy"],
                                          with_rationale(no_params()), aws.restore_oac_bucket_policy,
                                          consequential=True),
        "enforce_public_access_block": Tool("enforce_public_access_block", FIX_ACTIONS["enforce_public_access_block"],
                                            with_rationale(no_params()), aws.enforce_public_access_block,
                                            consequential=True),
        "set_viewer_protocol_policy": Tool("set_viewer_protocol_policy", FIX_ACTIONS["set_viewer_protocol_policy"],
                                           with_rationale({"type": "object", "properties": {"policy": {
                                               "type": "string",
                                               "enum": ["redirect-to-https", "https-only", "allow-all"]}},
                                               "required": ["policy"]}),
                                           aws.set_viewer_protocol_policy, consequential=True),
        "set_default_root_object": Tool("set_default_root_object", FIX_ACTIONS["set_default_root_object"],
                                        with_rationale({"type": "object", "properties": {
                                            "root_object": {"type": "string"}}, "required": ["root_object"]}),
                                        aws.set_default_root_object, consequential=True),
        "upsert_alias_records": Tool("upsert_alias_records", FIX_ACTIONS["upsert_alias_records"],
                                     with_rationale(no_params()), aws.upsert_alias_records, consequential=True),
    }


def cache_tools(aws: AWS) -> list[Tool]:
    return [
        Tool("cloudfront_invalidate", "Invalidate CloudFront cached paths so edges fetch fresh content.",
             {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}}},
             aws.cloudfront_invalidate),
        Tool("cloudfront_wait_deployed", "Wait until a CloudFront config change finishes deploying.",
             {"type": "object", "properties": {"timeout_s": {"type": "integer", "default": 900}}},
             aws.cloudfront_wait_deployed),
    ]


def cicd_read_tools(c: CICD) -> list[Tool]:
    exec_id = {"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]}
    build_id = {"type": "object", "properties": {"build_id": {"type": "string"}}, "required": ["build_id"]}
    return [
        Tool("read_repo_file", "Read a pipeline-related file from the repository (buildspecs, release.py, "
             "setup_pipeline.py, content-types.json, live checks, main.js, projects.html).",
             {"type": "object", "properties": {"path": {"type": "string", "enum": REPO_FILES},
                                               "start_line": {"type": "integer", "default": 1},
                                               "max_lines": {"type": "integer", "default": 150}},
              "required": ["path"]}, c.read_repo_file),
        Tool("git_commit", "Show a commit's message and diff (site, cicd, tests, tools) from the local clone.",
             {"type": "object", "properties": {"sha": {"type": "string"}}, "required": ["sha"]}, c.git_commit),
        Tool("live_pipeline", "Read the deployed CodePipeline definition (stages, actions, artifacts, triggers).",
             no_params(), c.live_pipeline),
        Tool("pipeline_drift", "Compare the deployed pipeline and CodeBuild projects with the repository "
             "definition in cicd/setup_pipeline.py.", no_params(), c.pipeline_drift),
        Tool("codebuild_projects", "Read the three CodeBuild projects (role, buildspec, image, logs config).",
             no_params(), c.codebuild_projects),
        Tool("pipeline_role_policies", "Read the IAM policies of the pipeline and CodeBuild roles.",
             no_params(), c.pipeline_role_policies),
        Tool("recent_executions", "List recent pipeline executions (status, commit, message).",
             {"type": "object", "properties": {"max_results": {"type": "integer", "default": 10}}},
             c.recent_executions),
        Tool("execution_actions", "List every action of one pipeline execution with status, CodeBuild build ID "
             "and failure summary.", exec_id, c.execution_actions),
        Tool("codebuild_build", "Read one CodeBuild build: phases and their messages, env, log and report ARNs.",
             build_id, c.codebuild_build),
        Tool("build_log", "Read a CodeBuild build's log from S3 (last `tail` lines, optionally filtered by a "
             "case-insensitive regex `grep`).",
             {"type": "object", "properties": {"build_id": {"type": "string"},
                                               "tail": {"type": "integer", "default": 120},
                                               "grep": {"type": "string", "default": ""}},
              "required": ["build_id"]}, c.build_log),
        Tool("failed_test_cases", "List failed test cases from a CodeBuild test report ARN.",
             {"type": "object", "properties": {"report_arn": {"type": "string"}}, "required": ["report_arn"]},
             c.failed_test_cases),
        Tool("test_results", "Read the JSON test/verification results the pipeline stored for an execution.",
             exec_id, c.test_results),
        Tool("release_status", "Live release, known-good release and recent release history.",
             no_params(), c.release_status),
    ]
