"""Scrubs credentials from anything written to logs."""
import re

_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-***REDACTED***"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "AKIA***REDACTED***"),
    (re.compile(r"(?i)(aws_secret_access_key|secret|token|password)([\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9/+=]{16,}"),
     r"\1\2***REDACTED***"),
    (re.compile(r"X-Amz-(Signature|Credential|Security-Token)=[^&\s\"]+"), r"X-Amz-\1=***REDACTED***"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
