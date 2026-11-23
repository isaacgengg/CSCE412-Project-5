"""Test Engineer: the LLM writes candidate pre-deploy tests; tools/test_harness.py decides which
are kept (pass on the real site AND kill at least one mutant)."""
from __future__ import annotations
import re

from core.agent import Agent, Context, Tool, no_params
from tools import test_harness
from tools.registry import site_tools_for

SYSTEM = f"""You are the Test Engineer. Write pytest tests for the website's pre-deploy gate, derived
from the organization's requirements. Each candidate is one Python test file that:
- uses only the `site_dir` fixture (a pathlib.Path to the BUILT site, which also contains release.json
  with keys release_id, commit, short, built_at); no other fixtures, no network, no AWS, no subprocess;
- imports only: {sorted(test_harness.ALLOWED_IMPORTS)}. tools.site_tools provides _parse(path) (returns an
  object with .title, .h1 list, .nav_links, .links, .scripts, .images [dicts with src/alt], .styles,
  .has_viewport), check_links(site_dir) and check_site_requirements(site_dir, pages);
- reads files with Path.read_text() only (no open(), no writes);
- checks structure and behaviour contracts (pages, links, navigation, scripts, assets, release.json
  shape), NEVER exact visible copy text such as titles, headings or paragraph wording; those
  candidates are rejected automatically;
- must pass on the real site and fail on at least one of these automatically built mutants:
{chr(10).join(f'  - {k}: {v}' for k, v in test_harness.MUTANTS.items())}

Workflow: inspect the site (inventory_site, read_site_file); stage 2-5 focused candidates with
stage_candidate; run evaluate_candidates; fix or replace candidates that were rejected, failed on
the real site, or killed no mutant, until every mutant is killed by some kept candidate (or you
have tried three rounds). Then submit_report. The harness, not you, decides what is kept."""

REPORT = {"type": "object", "properties": {
    "candidates": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "purpose": {"type": "string"},
        "requirement": {"type": "string"}}, "required": ["name", "purpose"]}},
    "expected_surviving_mutants": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"},
}, "required": ["candidates", "summary"]}


class Session:
    """Holds the staged candidates between tool calls."""

    def __init__(self):
        self.candidates: dict[str, str] = {}
        self.last_evaluation: dict | None = None

    @staticmethod
    def normalize(name: str) -> str:
        """Accept 'test_nav.py', 'Nav Checks', etc.; the file is written as test_gen_<name>.py."""
        n = re.sub(r"[^a-z0-9_]+", "_", name.lower().removesuffix(".py")).strip("_")
        return n.removeprefix("test_").removeprefix("gen_")[:40]

    def stage(self, name: str, code: str) -> dict:
        name = self.normalize(name)
        if not test_harness.NAME.fullmatch(name):
            return {"error": f"use a short snake_case name such as nav_consistency (got {name!r})"}
        self.candidates[name] = code
        return {"staged": name, "total_staged": len(self.candidates), "staged_names": sorted(self.candidates)}

    def drop(self, name: str) -> dict:
        return {"dropped": self.candidates.pop(self.normalize(name), None) is not None}

    def evaluate(self) -> dict:
        if not self.candidates:
            return {"error": "no candidates staged"}
        self.last_evaluation = test_harness.evaluate(self.candidates)
        return self.last_evaluation


def read_site_file(site_dir, path: str) -> dict:
    f = (site_dir / path).resolve()
    if site_dir.resolve() not in f.parents or f.suffix not in (".html", ".js", ".css") or not f.is_file():
        return {"error": "only existing .html/.js/.css files inside site/"}
    return {"path": path, "content": f.read_text()[:6000]}


def build(ctx: Context, session: Session) -> Agent:
    tools = [t for t in site_tools_for(ctx.cfg) if t.name == "inventory_site"] + [
        Tool("read_site_file", "Read a site source file (.html/.js/.css).",
             {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
             lambda path: read_site_file(ctx.cfg.site_dir, path)),
        Tool("stage_candidate", "Stage (or replace) a candidate test file by name.",
             {"type": "object", "properties": {"name": {"type": "string"}, "code": {"type": "string"}},
              "required": ["name", "code"]}, session.stage),
        Tool("drop_candidate", "Remove a staged candidate.",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, session.drop),
        Tool("evaluate_candidates", "Run the deterministic harness on all staged candidates: static checks, "
             "real site, and every mutant.", no_params(), session.evaluate),
    ]
    return Agent("test_engineer", SYSTEM, tools, REPORT, ctx, max_steps=30)
