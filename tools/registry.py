"""Builds the Tool objects each agent is allowed to use.

The allowlist is the security boundary: an agent can only do what its tools do,
and anything that changes infrastructure is marked consequential (human approval).
"""
from __future__ import annotations
from functools import partial

from core.agent import Tool, no_params, with_rationale
from core.config import Config
from tools import http_tools, site_tools
from tools.aws_tools import AWS


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
    return [
        Tool("http_get", "HTTP GET a URL; returns status, redirect info and key headers.",
             {"type": "object", "properties": {
                 "url": {"type": "string"},
                 "follow_redirects": {"type": "boolean", "default": False}},
              "required": ["url"]}, http_tools.http_get),
        Tool("tls_certificate", "Open a TLS connection and return the validated certificate details.",
             {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]},
             http_tools.tls_certificate),
        Tool("dns_resolve", "Resolve a DNS name (A, AAAA, NS, CNAME).",
             {"type": "object", "properties": {
                 "name": {"type": "string"},
                 "record_type": {"type": "string", "enum": ["A", "AAAA", "NS", "CNAME"]}},
              "required": ["name"]}, http_tools.dns_resolve),
        Tool("s3_direct_access", "Request an object directly from the S3 bucket endpoint without credentials.",
             {"type": "object", "properties": {"key": {"type": "string", "default": "index.html"}}},
             lambda key="index.html": http_tools.http_get(aws.s3_direct_url(key))),
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
        Tool("s3_list_objects", "List the objects stored in the website bucket.", no_params(), aws.s3_list_objects),
        Tool("acm_certificate", "Describe the ACM certificate attached to the distribution.", no_params(),
             aws.acm_certificate),
        Tool("route53_records", "List DNS records in the domain's hosted zone.", no_params(), aws.route53_records),
    ]


# Fix actions the Diagnoser may propose. Each maps to one consequential tool.
FIX_ACTIONS = {
    "restore_oac_bucket_policy": "Re-apply the bucket policy that lets only this CloudFront distribution read objects.",
    "enforce_public_access_block": "Turn on all four S3 Block Public Access settings.",
    "set_viewer_protocol_policy": "Set the default behavior's viewer protocol policy (use redirect-to-https).",
    "set_default_root_object": "Set the distribution's default root object (normally index.html).",
    "upsert_alias_records": "Create or repair A/AAAA alias records pointing the domain at CloudFront.",
    "s3_sync_site": "Re-upload the local site to the bucket (restores missing or wrong objects).",
}


def aws_write_tools(aws: AWS) -> dict[str, Tool]:
    return {
        "s3_sync_site": Tool("s3_sync_site", FIX_ACTIONS["s3_sync_site"] + " Deletes bucket objects not in site/.",
                             with_rationale({"type": "object", "properties": {
                                 "delete_extraneous": {"type": "boolean", "default": True}}}),
                             aws.s3_sync_site, consequential=True),
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
