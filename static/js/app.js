// Mobile sidebar drawer toggle
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("drawerToggle");
  const sidebar = document.getElementById("sidebar");

  if (toggle && sidebar) {
    toggle.addEventListener("click", () => {
      sidebar.classList.toggle("open");
    });

    document.addEventListener("click", (e) => {
      if (
        sidebar.classList.contains("open") &&
        !sidebar.contains(e.target) &&
        !toggle.contains(e.target)
      ) {
        sidebar.classList.remove("open");
      }
    });
  }

  // Auto-dismiss toasts after 4s
  document.querySelectorAll(".toast").forEach((toast, i) => {
    setTimeout(() => {
      toast.style.transition = "opacity 200ms ease";
      toast.style.opacity = "0";
      setTimeout(() => toast.remove(), 200);
    }, 4000 + i * 300);
  });
});

// ---------------------------------------------------------------
// Upgrade additions
// ---------------------------------------------------------------
(function () {
  // Copy buttons (credentials dialog): data-copy="#elementId"
  function fallbackCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-copy]");
    if (btn) {
      const el = document.querySelector(btn.getAttribute("data-copy"));
      if (!el) return;
      const text = el.textContent.trim();
      const original = btn.textContent;
      const done = (ok) => {
        btn.textContent = ok ? "Copied ✓" : "Press Ctrl+C";
        if (!ok) { const r = document.createRange(); r.selectNodeContents(el); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
        setTimeout(() => (btn.textContent = original), 2000);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => done(true), () => done(fallbackCopy(text)));
      } else { done(fallbackCopy(text)); }
      return;
    }
    if (e.target.closest("[data-close-modal]")) {
      const modal = document.getElementById("credModal");
      if (modal) modal.remove();
    }
  });

  // Confirm dialogs for destructive/credential actions: <form data-confirm="...">
  document.addEventListener("submit", function (e) {
    const form = e.target;
    const msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    // Double-submit protection: lock submit buttons once the form is really going out
    setTimeout(function () {
      if (e.defaultPrevented || form.hasAttribute("data-no-lock")) return;
      form.querySelectorAll('button[type="submit"],input[type="submit"]').forEach((b) => { b.disabled = true; });
    }, 0);
  });
  // Re-enable buttons if the user returns via the back button
  window.addEventListener("pageshow", (ev) => {
    if (ev.persisted) document.querySelectorAll('button[type="submit"]').forEach((b) => (b.disabled = false));
  });

  // Favicon fallback: if the configured logo URL does not load, use the default badge
  document.addEventListener("DOMContentLoaded", function () {
    const icon = document.getElementById("siteIcon");
    if (icon && icon.getAttribute("data-fallback")) {
      const probe = new Image();
      probe.referrerPolicy = "no-referrer";
      probe.onerror = function () {
        icon.href = "data:image/svg+xml," + encodeURIComponent(
          "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='#0b3d5c'/><text x='32' y='41' font-size='26' text-anchor='middle' fill='white' font-family='Arial' font-weight='bold'>AIA</text></svg>");
      };
      probe.src = icon.href;
    }
  });
})();
