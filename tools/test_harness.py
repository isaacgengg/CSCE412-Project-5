"""Deterministic harness for LLM-generated tests (used by the Test Engineer). No LLM here.

A candidate test file is KEPT only if it:
  1. passes static checks (allowlisted imports, no file writes/exec/dunder access, no exact
     visible copy text, no hard-coded release SHA),
  2. passes against the real built site, and
  3. fails against at least one automatically built mutant of the site.
Kept tests are written to tests/generated/, which the pipeline's Test stage runs.

Candidates run in a subprocess with a minimal environment (no AWS or OpenAI credentials) and
a timeout. The static checks are a guard rail for coursework, not a security sandbox.
"""
from __future__ import annotations
import ast
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

from cicd import release
from core.config import ROOT

STAGING = ROOT / "build" / "test-staging"
GENERATED = ROOT / "tests" / "generated"
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
NAME = re.compile(r"[a-z][a-z0-9_]{2,40}")

MUTANTS = {
    "delete_page": "about.html is deleted",
    "break_link": "index.html links to /no-such-page.html instead of /about.html",
    "remove_script_tag": "projects.html no longer loads /js/main.js",
    "drop_nav_link": "contact.html's navigation loses the Resume link",
    "corrupt_release_json": "release.json is not valid JSON",
}
ALLOWED_IMPORTS = {"json", "re", "pathlib", "pytest", "urllib.parse", "html.parser", "tools.site_tools"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals", "vars",
                "getattr", "setattr", "delattr", "breakpoint"}
BANNED_ATTRS = {"write_text", "write_bytes", "unlink", "rmdir", "rename", "chmod", "touch", "mkdir",
                "symlink_to", "hardlink_to", "system", "popen", "rmtree"}
STAGING_CONFTEST = '''import os
import pathlib
import pytest


@pytest.fixture(scope="session")
def site_dir():
    return pathlib.Path(os.environ["SITE_DIR"])
'''


# ---------------- site variants ----------------
def _sub(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if old not in text:
        raise RuntimeError(f"mutant pattern not found in {path.name}: {old!r}")
    path.write_text(text.replace(old, new))


def _mutate_source(name: str, src: Path) -> None:
    if name == "delete_page":
        (src / "about.html").unlink()
    elif name == "break_link":
        _sub(src / "index.html", 'href="/about.html"', 'href="/no-such-page.html"')
    elif name == "remove_script_tag":
        _sub(src / "projects.html", '<script src="/js/main.js" defer></script>', "")
    elif name == "drop_nav_link":
        _sub(src / "contact.html", '<li><a href="/resume.html">Resume</a></li>', "")


def build_variants(root: Path = STAGING / "sites") -> dict[str, Path]:
    """The real build plus one build per mutant (source mutated first, then built)."""
    shutil.rmtree(root, ignore_errors=True)
    out = {"real": release.build(FAKE_SHA, out=root / "real")}
    for m in MUTANTS:
        src = root / "src" / m
        shutil.copytree(release.cfg().site_dir, src)
        _mutate_source(m, src)
        out[m] = release.build(FAKE_SHA, src=src, out=root / m)
        if m == "corrupt_release_json":
            (out[m] / "release.json").write_text('{"release_id": ')
    return out


# ---------------- static checks ----------------
class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.chunks.append(" ".join(data.split()).lower())


def visible_copy(site_dir: Path) -> str:
    p = _Text()
    for f in sorted(site_dir.rglob("*.html")):
        p.feed(f.read_text())
    return "\n".join(p.chunks)


def static_check(code: str, copy: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    problems = []
    if not any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_") for n in tree.body):
        problems.append("no top-level test_ functions")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            problems += [f"import {a.name} not allowed" for a in node.names if a.name not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and node.module not in ALLOWED_IMPORTS:
            problems.append(f"from {node.module} import not allowed")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            problems.append(f"call to {node.func.id}() not allowed")
        elif isinstance(node, ast.Attribute) and (node.attr in BANNED_ATTRS or node.attr.startswith("__")):
            problems.append(f"attribute .{node.attr} not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = " ".join(node.value.split()).lower()
            if len(s) >= 12 and s in copy:
                problems.append(f"asserts exact visible copy text {node.value[:60]!r}")
            if FAKE_SHA[:7] in s:
                problems.append("hard-codes a release SHA")
    return sorted(set(problems))


# ---------------- running ----------------
def run_pytest(test_file: Path, site: Path, timeout: int = 60) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "SITE_DIR": str(site), "PYTHONPATH": str(ROOT),
           "EXPECTED_SHA": FAKE_SHA, "HOME": str(STAGING)}
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider",
                            "--rootdir", str(test_file.parent)], cwd=test_file.parent, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "output": "timed out"}
    return {"exit_code": p.returncode, "output": (p.stdout + p.stderr)[-1200:]}


def evaluate(candidates: dict[str, str]) -> dict:
    sites = build_variants()
    copy = visible_copy(sites["real"])
    cdir = STAGING / "candidates"
    shutil.rmtree(cdir, ignore_errors=True)
    cdir.mkdir(parents=True)
    (cdir / "conftest.py").write_text(STAGING_CONFTEST)
    results = []
    for name, code in candidates.items():
        r = {"name": name, "kept": False, "mutants_killed": []}
        results.append(r)
        problems = ([] if NAME.fullmatch(name) else ["invalid name"]) + static_check(code, copy)
        if problems:
            r["reason"] = "rejected: " + "; ".join(problems)
            continue
        f = cdir / f"test_{name}.py"
        f.write_text(code)
        real = run_pytest(f, sites["real"])
        if real["exit_code"] != 0:
            r.update(reason="fails (or collects no tests) on the real site", output=real["output"])
            continue
        r["mutants_killed"] = [m for m in MUTANTS if run_pytest(f, sites[m])["exit_code"] in (1, 2)]
        r["kept"] = bool(r["mutants_killed"])
        r["reason"] = "kept" if r["kept"] else "passes on the real site but kills no mutant"
    covered = {m for r in results if r["kept"] for m in r["mutants_killed"]}
    return {"candidates": results, "kept": [r["name"] for r in results if r["kept"]],
            "surviving_mutants": [m for m in MUTANTS if m not in covered], "mutants": MUTANTS}


def write_kept(evaluation: dict, candidates: dict[str, str], run_id: str) -> list[str]:
    GENERATED.mkdir(parents=True, exist_ok=True)
    written = []
    for r in evaluation["candidates"]:
        if r["kept"]:
            path = GENERATED / f"test_gen_{r['name']}.py"
            path.write_text(f"# Generated by the Test Engineer agent (run {run_id}).\n"
                            f"# Kept by tools/test_harness.py: passes on the real site and kills mutants: "
                            f"{', '.join(r['mutants_killed'])}.\n" + candidates[r["name"]].lstrip())
            written.append(str(path.relative_to(ROOT)))
    return written
