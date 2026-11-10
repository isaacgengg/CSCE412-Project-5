"""Verification agent.

Step 1 (deterministic): run the Part D check suite through the probe tools and
compute pass/fail from the raw evidence. The LLM cannot change these results.
Step 2 (LLM): interpret the evidence, run extra probes if something looks off,
and summarize what failed and what it points to.
"""
from __future__ import annotations
import time

from core.agent import Agent, Context
from tools.aws_tools import AWS
from tools.registry import probe_tools

SYSTEM = """You are the Verification Agent. You receive the results of a deterministic check suite
run against the live website. The pass/fail values are computed from evidence and are final.
Your job: explain the results, run additional probes when a failure needs more evidence
(for example: is it every page or one page? does DNS resolve? is the certificate valid but
the page failing?), and describe the symptoms precisely so the Diagnoser can find the cause.
Do not guess at root causes inside AWS; describe observed behavior."""

REPORT = {"type": "object", "properties": {
    "all_passed": {"type": "boolean"},
    "failed_checks": {"type": "array", "items": {"type": "string"}},
    "symptoms": {"type": "array", "items": {"type": "string"}},
    "additional_evidence": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"},
}, "required": ["all_passed", "failed_checks", "symptoms", "summary"]}


def build(ctx: Context, aws: AWS) -> Agent:
    return Agent("verifier_agent", SYSTEM, probe_tools(ctx.cfg, aws), REPORT, ctx)


def run_suite(agent: Agent) -> list[dict]:
    cfg = agent.ctx.cfg
    d = cfg.domain
    call = lambda tool, **a: agent.invoke(tool, a, requested_by="deterministic-suite")
    checks: list[dict] = []

    def add(cid, desc, passed, evidence):
        checks.append({"id": cid, "description": desc, "passed": bool(passed), "evidence": evidence})

    home = call("http_get", url=f"https://{d}/")
    add("domain_accessible", f"https://{d}/ returns 200", home.get("status") == 200, home)

    tls = call("tls_certificate", host=d)
    add("https_valid", "TLS handshake succeeds with a trusted certificate chain", tls.get("verified"), tls)
    add("certificate_matches_domain", f"certificate covers {d} and is not expired",
        tls.get("hostname_in_sans") and tls.get("days_until_expiry", -1) > 0, tls)

    http = call("http_get", url=f"http://{d}/")
    loc = http.get("headers", {}).get("Location", "") or http.get("headers", {}).get("location", "")
    add("http_redirects_to_https", f"http://{d}/ redirects (301/308) to https://{d}/",
        http.get("status") in (301, 308) and loc.startswith(f"https://{d}"), http)

    page_results = {}
    for page in cfg.required_pages:
        r = call("http_get", url=f"https://{d}/{page}")
        page_results[page] = r.get("status")
    add("required_pages_accessible", "every required page returns 200",
        all(s == 200 for s in page_results.values()), page_results)

    hdrs = {k.lower(): v for k, v in home.get("headers", {}).items()}
    via_cf = "cloudfront" in hdrs.get("via", "").lower() or "cloudfront" in hdrs.get("x-cache", "").lower()
    add("cloudfront_serves_content", "responses carry CloudFront headers (Via / X-Cache)", via_cf,
        {k: hdrs.get(k) for k in ("via", "x-cache", "x-amz-cf-pop", "server")})

    s3 = call("s3_direct_access", key="index.html")
    add("s3_direct_access_denied", "anonymous request to the S3 endpoint is denied (403)",
        s3.get("status") in (401, 403), s3)

    missing = call("http_get", url=f"https://{d}/verify-missing-{int(time.time())}.html")
    add("custom_error_page", "missing pages return 404 with the custom error page",
        missing.get("status") == 404 and "html" in str(missing.get("headers", {})).lower(), missing)

    for alt in cfg.alt_names:
        r = call("http_get", url=f"https://{alt}/")
        add(f"alt_name_{alt}", f"https://{alt}/ returns 200", r.get("status") == 200, r)
    return checks
