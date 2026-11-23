"""tools/test_harness.py: candidates are kept only if they pass on the real site and kill a mutant."""
import pytest

from tools import test_harness as H

GOOD_NAV = '''
from tools.site_tools import _parse

PAGES = ["index.html", "about.html", "projects.html", "resume.html", "contact.html"]


def test_navigation_is_identical_on_content_pages(site_dir):
    navs = {p: sorted(_parse(site_dir / p).nav_links) for p in PAGES}
    assert len({tuple(v) for v in navs.values()}) == 1, navs
'''
GOOD_RELEASE = '''
import json


def test_release_json_is_valid(site_dir):
    rel = json.loads((site_dir / "release.json").read_text())
    assert rel["short"] == rel["commit"][:7]
'''
FAILS_ON_REAL = '''
def test_impossible(site_dir):
    assert (site_dir / "does-not-exist.html").exists()
'''
KILLS_NOTHING = '''
def test_css_exists(site_dir):
    assert (site_dir / "css" / "style.css").exists()
'''


@pytest.fixture(scope="module")
def copy():
    return H.visible_copy(H.build_variants()["real"])


@pytest.mark.parametrize("code,problem", [
    ("import os\ndef test_x(site_dir):\n    os.listdir(site_dir)\n", "import os not allowed"),
    ("def test_x(site_dir):\n    open('x')\n", "call to open() not allowed"),
    ("def test_x(site_dir):\n    (site_dir / 'a').write_text('x')\n", "attribute .write_text not allowed"),
    ("def test_x(site_dir):\n    ().__class__\n", "attribute .__class__ not allowed"),
    ("x = 1\n", "no top-level test_ functions"),
    ("def test_x(site_dir):\n    assert '0123456789abcdef' in ''\n", "hard-codes a release SHA"),
])
def test_static_check_rejects(code, problem, copy):
    assert problem in H.static_check(code, copy)


def test_static_check_rejects_exact_visible_copy(copy):
    code = "def test_t(site_dir):\n    assert 'I build systems from the gates up.' in (site_dir/'index.html').read_text()\n"
    assert any("visible copy" in p for p in H.static_check(code, copy))


def test_static_check_accepts_structural_test(copy):
    assert H.static_check(GOOD_NAV, copy) == []


def test_every_mutant_differs_from_the_real_build():
    sites = H.build_variants()
    real = {p.relative_to(sites["real"]): p.read_bytes() for p in sites["real"].rglob("*") if p.is_file()}
    for m in H.MUTANTS:
        mutant = {p.relative_to(sites[m]): p.read_bytes() for p in sites[m].rglob("*") if p.is_file()}
        assert mutant != real, m


def test_evaluate_keeps_only_useful_tests():
    result = H.evaluate({"nav": GOOD_NAV, "release": GOOD_RELEASE, "bad": FAILS_ON_REAL, "weak": KILLS_NOTHING,
                         "copy": "def test_c(site_dir):\n    assert 'I build systems from the gates up.'\n"})
    by = {r["name"]: r for r in result["candidates"]}
    assert by["nav"]["kept"] and "drop_nav_link" in by["nav"]["mutants_killed"]
    assert by["release"]["kept"] and by["release"]["mutants_killed"] == ["corrupt_release_json"]
    assert not by["bad"]["kept"] and "real site" in by["bad"]["reason"]
    assert not by["weak"]["kept"] and "kills no mutant" in by["weak"]["reason"]
    assert not by["copy"]["kept"] and by["copy"]["reason"].startswith("rejected")
    assert sorted(result["kept"]) == ["nav", "release"]
    assert "remove_script_tag" in result["surviving_mutants"]


@pytest.mark.parametrize("raw,want", [("test_site_structure.py", "site_structure"), ("Nav Checks", "nav_checks"),
                                      ("test_gen_release.py", "release")])
def test_candidate_names_are_normalized(raw, want):
    from agents.test_engineer import Session
    assert Session.normalize(raw) == want
