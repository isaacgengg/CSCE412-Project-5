"""Pre-deploy gate: any failure here fails the Test stage and blocks deployment."""
from __future__ import annotations
import json
import re

import pytest

from tools.live_checks import jsdom_filter_check
from tools.site_tools import _parse, check_links, check_site_requirements

PAGES = ["index.html", "about.html", "projects.html", "resume.html", "contact.html", "404.html"]
CONTENT_PAGES = [p for p in PAGES if p != "404.html"]
# path -> (min bytes, max bytes)
ASSETS = {"css/style.css": (500, 200_000), "js/main.js": (500, 200_000), "img/favicon.svg": (100, 50_000),
          "img/portrait.svg": (100, 500_000), "img/proj-cloud.svg": (100, 500_000),
          "img/proj-fpga.svg": (100, 500_000), "img/proj-ml.svg": (100, 500_000),
          "img/proj-sim.svg": (100, 500_000)}


@pytest.mark.parametrize("page", PAGES)
def test_required_page_exists(site_dir, page):
    assert (site_dir / page).is_file(), f"missing required page {page}"


@pytest.mark.parametrize("asset", ASSETS)
def test_required_asset_present_with_sensible_size(site_dir, asset):
    f = site_dir / asset
    assert f.is_file(), f"missing required asset {asset}"
    lo, hi = ASSETS[asset]
    assert lo <= f.stat().st_size <= hi, f"{asset} is {f.stat().st_size} bytes, expected {lo}-{hi}"


def test_every_internal_reference_resolves(site_dir):
    r = check_links(site_dir)
    assert r["references_checked"] > 0
    assert r["broken"] == [], f"broken internal links/scripts/stylesheets/images: {r['broken']}"


@pytest.mark.parametrize("page", PAGES)
def test_page_structure(site_dir, page):
    text = (site_dir / page).read_text()
    p = _parse(site_dir / page)
    assert text.lstrip().lower().startswith("<!doctype html>"), "missing <!DOCTYPE html>"
    assert re.search(r"<html[^>]*\blang=\"[a-z]{2}", text), "missing <html lang>"
    assert p.title, "missing or empty <title>"
    assert len(p.h1) == 1, f"expected exactly one <h1>, found {len(p.h1)}"
    assert p.has_viewport, "missing viewport meta"
    assert "/js/main.js" in p.scripts, "main.js is not loaded"


def test_shared_navigation_on_every_content_page(site_dir):
    navs = {page: sorted(_parse(site_dir / page).nav_links) for page in CONTENT_PAGES}
    assert len({tuple(v) for v in navs.values()}) == 1, f"navigation differs between pages: {navs}"
    nav = next(iter(navs.values()))
    for page in CONTENT_PAGES:
        assert f"/{page}" in nav, f"navigation is missing /{page}"


def test_site_requirements_summary(site_dir, cfg):
    facts = check_site_requirements(site_dir, cfg.required_pages)
    assert facts["all_passed"], facts


def test_release_json_matches_commit(site_dir, expected_sha):
    rel = json.loads((site_dir / "release.json").read_text())
    assert rel["release_id"] == expected_sha
    assert rel["commit"] == expected_sha
    assert rel["short"] == expected_sha[:7]


@pytest.mark.parametrize("page", PAGES)
def test_footer_stamped_with_release(site_dir, expected_sha, page):
    assert f"Release <code>{expected_sha[:7]}</code>" in (site_dir / page).read_text()


def test_project_filter_behaviour_in_jsdom(site_dir):
    r = jsdom_filter_check(str(site_dir))
    assert r["passed"], json.dumps(r, indent=2)
