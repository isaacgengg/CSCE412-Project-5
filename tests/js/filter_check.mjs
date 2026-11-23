// Critical-function check: run projects.html with js/main.js in jsdom, click a filter,
// and assert that card visibility and the "Showing N of M projects" count change.
//   node tests/js/filter_check.mjs build/site                    (pre-deploy, local files)
//   node tests/js/filter_check.mjs https://csce412isaacgeng.me   (post-deploy, production)
// Prints one JSON line; exits 0 only if every assertion holds.
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { JSDOM, VirtualConsole } from "jsdom";

const target = process.argv[2];
const remote = /^https?:\/\//.test(target ?? "");

async function load(rel) {
  if (!remote) return readFile(join(target, rel), "utf8");
  const res = await fetch(new URL(rel, target.replace(/\/?$/, "/")), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${rel} -> ${res.status}`);
  return res.text();
}

const result = { target, passed: false, steps: [] };
const fail = (msg) => { result.error = msg; console.log(JSON.stringify(result)); process.exit(1); };

try {
  if (!target) fail("usage: filter_check.mjs <dir|url>");
  const [html, js] = await Promise.all([load("projects.html"), load("js/main.js")]);
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(String(e.message || e)));
  const dom = new JSDOM(html, { url: "https://site.test/projects.html", runScripts: "outside-only",
                                pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.eval(js);  // the page loads main.js with `defer`, i.e. after the DOM is parsed, as here
  if (errors.length) fail(`script error: ${errors.join("; ")}`);

  const doc = w.document;
  const cards = [...doc.querySelectorAll(".project")];
  const count = () => doc.querySelector("[data-count]")?.textContent.trim() ?? "";
  const visible = () => cards.filter((c) => !c.hidden).length;
  if (cards.length === 0) fail("no project cards found");

  const step = (filter, expected) => {
    const s = { filter, expected_visible: expected, visible: visible(), count: count() };
    s.ok = s.visible === expected && s.count === `Showing ${expected} of ${cards.length} projects`;
    result.steps.push(s);
    return s.ok;
  };
  if (!step("initial", cards.length)) fail("initial state should show every project");

  const buttons = [...doc.querySelectorAll(".filters button")].filter((b) => b.dataset.filter !== "all");
  if (buttons.length === 0) fail("no filter buttons found");
  const btn = buttons[0];
  const tag = btn.dataset.filter;
  const expected = cards.filter((c) => c.dataset.tags.split(" ").includes(tag)).length;
  if (expected === cards.length) fail(`filter "${tag}" matches every card; cannot observe a change`);
  btn.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  if (!step(tag, expected)) fail(`after clicking "${tag}" the cards or count did not update`);
  if (btn.getAttribute("aria-pressed") !== "true") fail(`"${tag}" button not marked pressed`);

  doc.querySelector('.filters button[data-filter="all"]')?.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  if (!step("all", cards.length)) fail('clicking "all" did not restore every project');

  result.passed = true;
  console.log(JSON.stringify(result));
} catch (e) {
  fail(String(e.message || e));
}
