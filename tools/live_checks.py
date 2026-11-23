"""Production verification checks, shared by the pipeline and the Verifier agent.

Every check takes `call(tool_name, **args)` so the same code runs two ways:
  - pipeline (tests/postdeploy): `direct_call(cfg)` calls the probe functions directly
  - Verifier agent: `agent.invoke`, so each probe is logged in runs/<id>/tools.jsonl
Each check returns a list of {id, description, passed, evidence}. No LLM involved.
"""
from __future__ import annotations
import json
import subprocess
import time

from core.config import ROOT, Config
from tools import http_tools
from tools.aws_tools import AWS

JSDOM_SCRIPT = ROOT / "tests" / "js" / "filter_check.mjs"

# Independent of cicd/content-types.json on purpose: a bad deploy mapping must fail here.
EXPECTED_TYPES = {
    "index.html": ("text/html",),
    "css/style.css": ("text/css",),
    "js/main.js": ("application/javascript", "text/javascript"),
    "img/favicon.svg": ("image/svg+xml",),
    "release.json": ("application/json",),
}


def jsdom_filter_check(target: str) -> dict:
    """Run projects.html + js/main.js from a directory or URL in jsdom and exercise the filter."""
    try:
        p = subprocess.run(["node", str(JSDOM_SCRIPT), target], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return {"target": target, "passed": False, "error": "node is not installed"}
    except subprocess.TimeoutExpired:
        return {"target": target, "passed": False, "error": "timed out"}
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"stdout": p.stdout[-500:]}
    return {"target": target, **out, "passed": p.returncode == 0 and out.get("passed") is True,
            "stderr": p.stderr[-500:]}


def probe_functions(aws: AWS) -> dict:
    return {
        "http_get": http_tools.http_get,
        "http_get_json": http_tools.http_get_json,
        "tls_certificate": http_tools.tls_certificate,
        "dns_resolve": http_tools.dns_resolve,
        "s3_direct_access": lambda key="index.html": http_tools.http_get(aws.s3_direct_url(key)),
        "s3_public_access_block": aws.s3_public_access_block,
        "jsdom_filter_check": jsdom_filter_check,
    }


def direct_call(cfg: Config):
    fns = probe_functions(AWS(cfg))
    return lambda tool, **args: fns[tool](**args)


def _r(cid, desc, passed, evidence) -> dict:
    return {"id": cid, "description": desc, "passed": bool(passed), "evidence": evidence}


def _headers(resp: dict) -> dict:
    return {k.lower(): v for k, v in resp.get("headers", {}).items()}


# ---------------- checks ----------------
def domain_and_tls(call, cfg, release):
    d = cfg.domain
    home = call("http_get", url=f"https://{d}/")
    tls = call("tls_certificate", host=d)
    return [_r("domain_accessible", f"https://{d}/ returns 200", home.get("status") == 200, home),
            _r("https_valid", "TLS handshake succeeds with a trusted certificate chain", tls.get("verified"), tls),
            _r("certificate_matches_domain", f"certificate covers {d} and is not expired",
               tls.get("hostname_in_sans") and tls.get("days_until_expiry", -1) > 0, tls)]


def http_redirect(call, cfg, release):
    d = cfg.domain
    r = call("http_get", url=f"http://{d}/")
    loc = _headers(r).get("location", "")
    return [_r("http_redirects_to_https", f"http://{d}/ returns 301 to https://{d}/",
               r.get("status") == 301 and loc.startswith(f"https://{d}"), r)]


def pages_via_cloudfront(call, cfg, release):
    statuses, via = {}, {}
    for page in cfg.required_pages:
        r = call("http_get", url=f"https://{cfg.domain}/{page}")
        h = _headers(r)
        statuses[page] = r.get("status")
        via[page] = "cloudfront" in (h.get("via", "") + h.get("x-cache", "")).lower()
    return [_r("required_pages_accessible", "every required page returns 200",
               all(s == 200 for s in statuses.values()), statuses),
            _r("cloudfront_serves_content", "every required page carries CloudFront headers (Via / X-Cache)",
               all(via.values()), via)]


def release_identity(call, cfg, release):
    r = call("http_get_json", url=f"https://{cfg.domain}/release.json")
    live = (r.get("json") or {}).get("release_id")
    want = release or "(any)"
    return [_r("live_release_matches", f"/release.json release_id is {want}",
               r.get("status") == 200 and live and (release is None or live == release),
               {"expected": release, "live": live, "response": r})]


def content_types(call, cfg, release):
    seen = {}
    for path, allowed in EXPECTED_TYPES.items():
        ctype = _headers(call("http_get", url=f"https://{cfg.domain}/{path}")).get("content-type", "")
        seen[path] = {"content_type": ctype, "ok": ctype.split(";")[0].strip().lower() in allowed}
    return [_r("content_types_correct", "html, css, js, svg and json are served with correct Content-Type",
               all(v["ok"] for v in seen.values()), seen)]


def critical_function(call, cfg, release):
    r = call("jsdom_filter_check", target=f"https://{cfg.domain}")
    return [_r("project_filter_works", "deployed projects.html + js/main.js: filter changes cards and count",
               r.get("passed"), r)]


def s3_security(call, cfg, release):
    key = f"releases/{release}/index.html" if release else "index.html"
    direct = call("s3_direct_access", key=key)
    pab = call("s3_public_access_block")
    return [_r("s3_direct_access_denied", f"anonymous GET of {key} on the S3 endpoint returns 403",
               direct.get("status") in (401, 403), direct),
            _r("public_access_block_enabled", "all four S3 Block Public Access settings are on",
               pab.get("all_blocked") is True, pab)]


def error_page_and_alt_names(call, cfg, release):
    missing = call("http_get", url=f"https://{cfg.domain}/verify-missing-{int(time.time())}.html")
    out = [_r("custom_error_page", "missing pages return 404 with the custom error page",
              missing.get("status") == 404 and "html" in str(missing.get("headers", {})).lower(), missing)]
    for alt in cfg.alt_names:
        r = call("http_get", url=f"https://{alt}/")
        out.append(_r(f"alt_name_{alt}", f"https://{alt}/ returns 200", r.get("status") == 200, r))
    return out


CHECKS = [domain_and_tls, http_redirect, pages_via_cloudfront, release_identity, content_types,
          critical_function, s3_security, error_page_and_alt_names]


def run_suite(call, cfg: Config, release: str | None = None) -> list[dict]:
    return [r for check in CHECKS for r in check(call, cfg, release)]
