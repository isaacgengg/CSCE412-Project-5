"""Deterministic inspection of the local site/ directory (no network, no AWS)."""
from __future__ import annotations
from html.parser import HTMLParser
from pathlib import Path


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.h1 = []
        self.nav_links: list[str] = []
        self.links: list[str] = []
        self.scripts: list[str] = []
        self.images: list[dict] = []
        self.styles: list[str] = []
        self.has_viewport = False
        self._in_title = self._in_h1 = False
        self._nav_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
            self.h1.append("")
        elif tag == "nav":
            self._nav_depth += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
            if self._nav_depth:
                self.nav_links.append(a["href"])
        elif tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        elif tag == "img":
            self.images.append({"src": a.get("src"), "alt": a.get("alt")})
        elif tag == "link" and a.get("rel") == "stylesheet":
            self.styles.append(a.get("href", ""))
        elif tag == "meta" and a.get("name") == "viewport":
            self.has_viewport = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "nav":
            self._nav_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if self._in_h1 and self.h1:
            self.h1[-1] += data.strip() + " "


def _parse(path: Path) -> _PageParser:
    p = _PageParser()
    p.feed(path.read_text(errors="replace"))
    return p


def _is_local(ref: str) -> bool:
    return bool(ref) and not ref.startswith(("http://", "https://", "mailto:", "#", "data:", "//"))


def _resolve(site: Path, page: Path, ref: str) -> Path:
    ref = ref.split("#")[0].split("?")[0]
    return (site / ref.lstrip("/")) if ref.startswith("/") else (page.parent / ref)


def inventory_site(site_dir: Path) -> dict:
    pages = []
    for f in sorted(site_dir.rglob("*.html")):
        p = _parse(f)
        pages.append({"file": str(f.relative_to(site_dir)), "title": p.title,
                      "h1": [h.strip() for h in p.h1], "nav_links": p.nav_links,
                      "scripts": p.scripts, "stylesheets": p.styles,
                      "images": len(p.images), "responsive_viewport": p.has_viewport})
    assets = sorted(str(f.relative_to(site_dir)) for f in site_dir.rglob("*")
                    if f.is_file() and f.suffix != ".html")
    return {"site_dir": str(site_dir), "page_count": len(pages), "pages": pages, "assets": assets}


def check_links(site_dir: Path) -> dict:
    broken = []
    checked = 0
    for f in sorted(site_dir.rglob("*.html")):
        p = _parse(f)
        refs = p.links + p.scripts + p.styles + [i["src"] for i in p.images if i["src"]]
        for ref in refs:
            if not _is_local(ref):
                continue
            checked += 1
            target = _resolve(site_dir, f, ref)
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                broken.append({"page": str(f.relative_to(site_dir)), "ref": ref})
    missing_alt = [{"page": str(f.relative_to(site_dir)), "src": i["src"]}
                   for f in site_dir.rglob("*.html") for i in _parse(f).images if i["alt"] is None]
    return {"references_checked": checked, "broken": broken, "images_missing_alt": missing_alt}


def check_site_requirements(site_dir: Path, required_pages: list[str]) -> dict:
    """Maps Part A requirements to concrete, checkable facts."""
    pages = {str(f.relative_to(site_dir)): _parse(f) for f in site_dir.rglob("*.html")}
    content_pages = {k: v for k, v in pages.items() if k != "404.html"}
    nav_sets = {k: tuple(sorted(v.nav_links)) for k, v in content_pages.items()}
    consistent_nav = len(set(nav_sets.values())) == 1 and bool(next(iter(nav_sets.values()), ()))
    js_files = sorted({s for p in pages.values() for s in p.scripts if _is_local(s)})
    js_present = [s for s in js_files if (site_dir / s.lstrip("/")).exists()]
    facts = {
        "at_least_five_pages": len(content_pages) >= 5,
        "required_pages_present": {p: p in pages for p in required_pages},
        "consistent_navigation": consistent_nav,
        "responsive_viewport_on_all_pages": all(p.has_viewport for p in pages.values()),
        "client_side_javascript": js_present,
        "custom_error_page": "404.html" in pages,
        "images_present": sum(len(p.images) for p in pages.values()) > 0,
        "headings_on_all_pages": all(p.h1 for p in pages.values()),
    }
    facts["all_passed"] = (facts["at_least_five_pages"] and all(facts["required_pages_present"].values())
                           and consistent_nav and facts["responsive_viewport_on_all_pages"]
                           and bool(js_present) and facts["custom_error_page"]
                           and facts["images_present"] and facts["headings_on_all_pages"])
    return facts
