// Edit this to your real address before publishing the contact page.
const CONTACT_EMAIL = "you@example.com";

(function () {
  const root = document.documentElement;

  // Theme toggle (remembers choice per browser)
  const themeBtn = document.querySelector("[data-theme-toggle]");
  const saved = (() => { try { return localStorage.getItem("theme"); } catch { return null; } })();
  if (saved) root.setAttribute("data-theme", saved);
  const isDark = () =>
    root.getAttribute("data-theme") === "dark" ||
    (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const syncThemeLabel = () => {
    if (!themeBtn) return;
    themeBtn.textContent = isDark() ? "Light mode" : "Dark mode";
    themeBtn.setAttribute("aria-pressed", String(isDark()));
  };
  themeBtn?.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch {}
    syncThemeLabel();
  });
  syncThemeLabel();

  // Mobile menu
  const nav = document.querySelector(".nav");
  const menuBtn = document.querySelector(".menu-btn");
  menuBtn?.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    menuBtn.setAttribute("aria-expanded", String(open));
  });

  // Footer year
  document.querySelectorAll("[data-year]").forEach((el) => (el.textContent = new Date().getFullYear()));

  // Project filter
  const filters = document.querySelectorAll(".filters button");
  const projects = document.querySelectorAll(".project");
  const count = document.querySelector("[data-count]");
  const applyFilter = (tag) => {
    let shown = 0;
    projects.forEach((p) => {
      const match = tag === "all" || p.dataset.tags.split(" ").includes(tag);
      p.hidden = !match;
      if (match) shown++;
    });
    if (count) count.textContent = `Showing ${shown} of ${projects.length} projects`;
  };
  filters.forEach((btn) =>
    btn.addEventListener("click", () => {
      filters.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      applyFilter(btn.dataset.filter);
    })
  );
  if (projects.length) applyFilter("all");

  // Contact form: validates in the browser, then opens the visitor's mail app
  const form = document.querySelector("#contact-form");
  if (form) {
    const status = form.querySelector(".status");
    const rules = {
      name: (v) => (v.trim().length >= 2 ? "" : "Enter your name."),
      email: (v) => (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim()) ? "" : "Enter a valid email address, like name@example.com."),
      message: (v) => (v.trim().length >= 10 ? "" : "Write at least 10 characters."),
    };
    const check = (input) => {
      const msg = rules[input.name](input.value);
      input.setAttribute("aria-invalid", String(Boolean(msg)));
      form.querySelector(`#${input.name}-error`).textContent = msg;
      return !msg;
    };
    Object.keys(rules).forEach((name) => {
      const input = form.elements[name];
      input.addEventListener("blur", () => check(input));
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const ok = Object.keys(rules).map((n) => check(form.elements[n])).every(Boolean);
      if (!ok) {
        status.textContent = "Fix the highlighted fields to send your message.";
        return;
      }
      const { name, email, message } = form.elements;
      const subject = encodeURIComponent(`Portfolio message from ${name.value.trim()}`);
      const body = encodeURIComponent(`${message.value.trim()}\n\nReply to: ${email.value.trim()}`);
      window.location.href = `mailto:${CONTACT_EMAIL}?subject=${subject}&body=${body}`;
      status.textContent = "Opening your email app with the message filled in.";
    });
  }
})();
