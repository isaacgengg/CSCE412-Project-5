"""Network probes used for verification. Read-only; each returns raw evidence."""
from __future__ import annotations
import socket
import ssl
from datetime import datetime, timezone

import dns.resolver
import requests

KEEP_HEADERS = ("server", "via", "x-cache", "x-amz-cf-pop", "x-amz-cf-id", "content-type",
                "location", "strict-transport-security", "age", "x-amz-error-code")


def http_get(url: str, follow_redirects: bool = False, timeout: float = 10) -> dict:
    try:
        r = requests.get(url, allow_redirects=follow_redirects, timeout=timeout,
                         headers={"User-Agent": "csce412-verifier/1.0"})
    except requests.exceptions.SSLError as e:
        return {"url": url, "error": "SSLError", "message": str(e)[:300]}
    except requests.exceptions.RequestException as e:
        return {"url": url, "error": type(e).__name__, "message": str(e)[:300]}
    return {
        "url": url,
        "status": r.status_code,
        "final_url": r.url,
        "redirect_chain": [h.status_code for h in r.history],
        "headers": {k: v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS},
        "body_snippet": r.text[:160].replace("\n", " "),
        "elapsed_ms": int(r.elapsed.total_seconds() * 1000),
    }


def tls_certificate(host: str, port: int = 443) -> dict:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
                version = s.version()
    except ssl.SSLCertVerificationError as e:
        return {"host": host, "verified": False, "error": e.verify_message, "message": str(e)[:300]}
    except (OSError, ssl.SSLError) as e:
        return {"host": host, "verified": False, "error": type(e).__name__, "message": str(e)[:300]}
    not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    subject = dict(x[0] for x in cert["subject"])
    issuer = dict(x[0] for x in cert["issuer"])
    return {
        "host": host,
        "verified": True,          # chain + hostname validated by Python's default context
        "tls_version": version,
        "subject_cn": subject.get("commonName"),
        "issuer": issuer.get("organizationName"),
        "subject_alt_names": sans,
        "not_before": cert["notBefore"],
        "not_after": cert["notAfter"],
        "days_until_expiry": (not_after - datetime.now(timezone.utc)).days,
        "hostname_in_sans": host in sans or any(s.startswith("*.") and host.endswith(s[1:]) for s in sans),
    }


def dns_resolve(name: str, record_type: str = "A") -> dict:
    try:
        ans = dns.resolver.resolve(name, record_type, lifetime=10)
        return {"name": name, "type": record_type, "answers": sorted(r.to_text() for r in ans),
                "ttl": ans.rrset.ttl}
    except dns.resolver.NXDOMAIN:
        return {"name": name, "type": record_type, "error": "NXDOMAIN"}
    except dns.resolver.NoAnswer:
        return {"name": name, "type": record_type, "error": "NoAnswer"}
    except Exception as e:
        return {"name": name, "type": record_type, "error": type(e).__name__, "message": str(e)[:200]}
