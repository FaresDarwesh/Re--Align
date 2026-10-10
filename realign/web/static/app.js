/* =========================================================================
   re-align — front-end behaviour
   No dependencies, no network calls, no tracking. Everything degrades
   gracefully if the browser is old or JavaScript is unavailable.
   ========================================================================= */
(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;

  /* ------------------------------------------------------------ helpers -- */
  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function on(el, ev, fn) { if (el) { el.addEventListener(ev, fn, false); } }
  function cookie(name) {
    var m = doc.cookie.match(new RegExp("(^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[2]) : null;
  }
  function setCookie(name, value, days) {
    var parts = name + "=" + encodeURIComponent(value) + "; path=/; max-age=" + (days * 86400) + "; samesite=lax";
    if (location.protocol === "https:") { parts += "; secure"; }
    doc.cookie = parts;
  }
  function lang() { return (root.getAttribute("lang") || "ar").toLowerCase(); }
  /* A handful of runtime strings (toasts) still need both languages; the rest
     come from the server-side catalogue. */
  function tr(ar, en) { return lang() === "en" ? en : ar; }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---------------------------------------------------------- language --- */
  on(doc, "click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-lang-toggle]");
    var menu = $("[data-lang-menu]");
    if (!btn || !menu) { return; }
    e.preventDefault();
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    btn.setAttribute("aria-expanded", String(!menu.hidden));
  });
  on(doc, "click", function (e) {
    var menu = $("[data-lang-menu]");
    if (!menu || menu.hidden) { return; }
    if (e.target.closest && e.target.closest("[data-lang-toggle]")) { return; }
    menu.hidden = true;
  });
  on(doc, "keydown", function (e) {
    if (e.key === "Escape") {
      var menu = $("[data-lang-menu]");
      if (menu) { menu.hidden = true; }
      doc.body.classList.remove("nav-open");
    }
  });

  /* ------------------------------------------------------ mobile menu --- */
  on(doc, "click", function (e) {
    if (!(e.target.closest && e.target.closest("[data-burger]"))) { return; }
    e.preventDefault();
    var panel = $("[data-mobile-menu]");
    var btn = $("[data-burger]");
    if (!panel) { return; }
    var open = panel.classList.toggle("open");
    if (btn) { btn.setAttribute("aria-expanded", String(open)); }
  });
  on(doc, "click", function (e) {
    var panel = $("[data-mobile-menu]");
    if (!panel || !panel.classList.contains("open")) { return; }
    if (e.target.closest && e.target.closest("[data-burger]")) { return; }
    if (e.target.closest && e.target.closest("[data-mobile-menu] a")) {
      panel.classList.remove("open");
      var btn = $("[data-burger]");
      if (btn) { btn.setAttribute("aria-expanded", "false"); }
    }
  });
  on(doc, "keydown", function (e) {
    if (e.key === "Escape") {
      var panel = $("[data-mobile-menu]");
      if (panel) { panel.classList.remove("open"); }
    }
  });

  /* ----------------------------------------------------------- sidebar --- */
  function closeNav() { doc.body.classList.remove("nav-open"); }
  on(doc, "click", function (e) {
    if (e.target.closest && e.target.closest("[data-nav-toggle]")) {
      e.preventDefault();
      doc.body.classList.toggle("nav-open");
      return;
    }
    if (e.target.closest && e.target.closest(".scrim")) { closeNav(); return; }
    if (e.target.closest && e.target.closest(".sb-link")) { closeNav(); }
  });

  /* -------------------------------------------------------------- back --- */
  on(doc, "click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-back]");
    if (!btn) { return; }
    e.preventDefault();
    var fallback = btn.getAttribute("data-back") || "/app";
    var sameOrigin = false;
    try {
      sameOrigin = doc.referrer && new URL(doc.referrer).origin === location.origin;
    } catch (_) { sameOrigin = false; }
    if (sameOrigin && history.length > 1) { history.back(); return; }
    location.href = fallback;
  });

  /* ------------------------------------------------------------ reveal --- */
  function initReveal() {
    var items = $$(".reveal");
    if (!items.length) { return; }
    if (!("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        var el = entry.target;
        var delay = parseInt(el.getAttribute("data-delay") || "0", 10);
        setTimeout(function () { el.classList.add("in"); }, delay);
        io.unobserve(el);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.06 });
    items.forEach(function (el) { io.observe(el); });
  }

  /* ---------------------------------------------------------- counters --- */
  function initCounters() {
    var nums = $$("[data-count]");
    if (!nums.length) { return; }
    function run(el) {
      var target = parseFloat(el.getAttribute("data-count")) || 0;
      var dec = parseInt(el.getAttribute("data-decimals") || "0", 10);
      var suffix = el.getAttribute("data-suffix") || "";
      var dur = 1400;
      var start = null;
      function frame(ts) {
        if (start === null) { start = ts; }
        var p = Math.min((ts - start) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = (target * eased).toFixed(dec) + suffix;
        if (p < 1) { requestAnimationFrame(frame); }
      }
      requestAnimationFrame(frame);
    }
    if (!("IntersectionObserver" in window)) { nums.forEach(run); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { run(entry.target); io.unobserve(entry.target); }
      });
    }, { threshold: 0.4 });
    nums.forEach(function (el) { io.observe(el); });
  }

  /* ------------------------------------------------------ card spotlight - */
  function initSpotlight() {
    $$("[data-spotlight]").forEach(function (card) {
      card.addEventListener("pointermove", function (e) {
        var r = card.getBoundingClientRect();
        card.style.setProperty("--mx", ((e.clientX - r.left) / r.width) * 100 + "%");
        card.style.setProperty("--my", ((e.clientY - r.top) / r.height) * 100 + "%");
      });
    });
  }

  /* ------------------------------------------------------------- toasts -- */
  var toastHost = null;
  window.toast = function (msg, kind) {
    if (!toastHost) {
      toastHost = doc.createElement("div");
      toastHost.className = "toasts";
      doc.body.appendChild(toastHost);
    }
    var el = doc.createElement("div");
    el.className = "toast";
    if (kind) { el.style.borderColor = "var(--" + kind + ")"; }
    el.textContent = msg;
    toastHost.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .3s, transform .3s";
      el.style.opacity = "0";
      el.style.transform = "translateY(10px)";
      setTimeout(function () { el.remove(); }, 320);
    }, 2600);
  };

  /* --------------------------------------------------------------- copy -- */
  on(doc, "click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-copy]");
    if (!btn) { return; }
    e.preventDefault();
    var text = btn.getAttribute("data-copy");
    var target = text ? null : doc.getElementById(btn.getAttribute("data-copy-target"));
    var value = text || (target ? target.textContent : "");
    if (!value) { return; }
    var done = function () {
      var old = btn.textContent;
      btn.textContent = btn.getAttribute("data-copy-done") || "✓";
      setTimeout(function () { btn.textContent = old; }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done).catch(function () { /* ignore */ });
    } else {
      var ta = doc.createElement("textarea");
      ta.value = value;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      doc.body.appendChild(ta);
      ta.select();
      try { doc.execCommand("copy"); done(); } catch (_) { /* ignore */ }
      ta.remove();
    }
  });

  /* ------------------------------------------------------------ confirm -- */
  on(doc, "submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (!msg) { return; }
    if (!window.confirm(msg)) { e.preventDefault(); }
  });

  /* --------------------------------------------------- disable on submit - */
  on(doc, "submit", function (e) {
    var form = e.target;
    if (form.getAttribute("data-no-disable") === "1") { return; }
    var btn = form.querySelector("button[type=submit], button:not([type])");
    if (!btn || btn.disabled) { return; }
    setTimeout(function () {
      btn.disabled = true;
      btn.setAttribute("data-old", btn.innerHTML);
      var spinner = doc.createElement("span");
      spinner.className = "spin";
      spinner.textContent = "…";
      btn.appendChild(spinner);
      setTimeout(function () {
        if (btn.disabled && btn.isConnected) {
          btn.disabled = false;
          btn.innerHTML = btn.getAttribute("data-old") || "";
        }
      }, 12000);
    }, 0);
  });

  /* ---------------------------------------------- sticky header shadow --- */
  var head = $(".mk-head");
  if (head) {
    var onScroll = function () {
      head.classList.toggle("stuck", window.scrollY > 8);
    };
    on(window, "scroll", onScroll, { passive: true });
    onScroll();
  }

  /* --------------------------------------------------------- cookie bar -- */
  var banner = doc.getElementById("cookie-banner");
  if (banner) {
    if (!cookie("ra_cookie_consent")) { banner.hidden = false; }
    on(banner, "click", function (e) {
      var v = e.target && e.target.getAttribute && e.target.getAttribute("data-cookie");
      if (!v) { return; }
      setCookie("ra_cookie_consent", v, v === "accept" ? 180 : 30);
      banner.hidden = true;
      window.toast(v === "accept"
        ? tr("تم قبول الكوكيز", "Cookies accepted")
        : tr("تم رفض الكوكيز الاختيارية", "Optional cookies rejected"));
    });
  }

  /* ---------------------------------------------------------- page load -- */
  function boot() {
    initReveal();
    initCounters();
    initSpotlight();
    // Marquee needs duplicated content to loop seamlessly.
    $$(".marquee-track").forEach(function (track) {
      if (track.getAttribute("data-cloned") === "1") { return; }
      track.innerHTML = track.innerHTML + track.innerHTML;
      track.setAttribute("data-cloned", "1");
    });
    // Smooth-scroll the in-page anchors.
    $$('a[href^="#"]').forEach(function (a) {
      on(a, "click", function (e) {
        var id = a.getAttribute("href").slice(1);
        var el = id && doc.getElementById(id);
        if (!el) { return; }
        e.preventDefault();
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  /* ======================================================================
     Cinematic marketing motion
     Progressive enhancement only: if any of this fails, the page still
     renders and works — the reveal classes are opt-in via the `js` class.
     ====================================================================== */

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* --- sticky nav: transparent at the top, solid once you scroll ------- */
  (function initNav() {
    var nav = doc.getElementById("mkNav");
    if (!nav) { return; }
    var ticking = false;
    function apply() {
      nav.classList.toggle("scrolled", window.scrollY > 24);
      ticking = false;
    }
    on(window, "scroll", function () {
      if (ticking) { return; }
      ticking = true;
      window.requestAnimationFrame(apply);
    }, { passive: true });
    apply();

    /* mobile menu */
    var burger = doc.getElementById("mkBurger");
    var panel = doc.getElementById("mkMobile");
    if (!burger || !panel) { return; }
    on(burger, "click", function () {
      var open = panel.classList.toggle("open");
      burger.setAttribute("aria-expanded", open ? "true" : "false");
    });
    panel.querySelectorAll("a").forEach(function (a) {
      on(a, "click", function () {
        panel.classList.remove("open");
        burger.setAttribute("aria-expanded", "false");
      });
    });
    on(doc, "keydown", function (e) {
      if (e.key === "Escape" && panel.classList.contains("open")) {
        panel.classList.remove("open");
        burger.setAttribute("aria-expanded", "false");
        burger.focus();
      }
    });
  })();

  /* --- hero: stagger the headline lines, then lead, then buttons ------- */
  (function initHero() {
    var lines = $$(".hero h1 .ln");
    if (lines.length) {
      lines.forEach(function (ln, i) {
        var delay = REDUCED ? 0 : (parseFloat(getComputedStyle(ln).getPropertyValue("--d")) || i * 120);
        window.setTimeout(function () { ln.classList.add("in"); }, delay);
      });
    }
    var seq = [
      ['[data-hero="lead"]', 520],
      ['[data-hero="cta"]', 640],
      ['[data-hero="note"]', 760]
    ];
    seq.forEach(function (pair) {
      $$(pair[0]).forEach(function (el) {
        el.classList.add("rv");
        el.style.setProperty("--d", (REDUCED ? 0 : pair[1]) + "ms");
        window.setTimeout(function () { el.classList.add("in"); }, REDUCED ? 0 : 60);
      });
    });
  })();

  /* --- hero light ribbons: a restrained pointer parallax on desktop ---- */
  (function initParallax() {
    if (REDUCED) { return; }
    var layer = doc.getElementById("heroLight");
    var hero = doc.getElementById("hero");
    if (!layer || !hero) { return; }
    if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) { return; }

    var tx = 0, ty = 0, cx = 0, cy = 0, raf = null;
    on(hero, "mousemove", function (e) {
      var r = hero.getBoundingClientRect();
      tx = ((e.clientX - r.left) / r.width - 0.5) * 22;
      ty = ((e.clientY - r.top) / r.height - 0.5) * 14;
      if (!raf) { raf = window.requestAnimationFrame(loop); }
    });
    on(hero, "mouseleave", function () {
      tx = 0; ty = 0;
      if (!raf) { raf = window.requestAnimationFrame(loop); }
    });
    function loop() {
      cx += (tx - cx) * 0.06;
      cy += (ty - cy) * 0.06;
      layer.style.transform = "translate3d(" + cx.toFixed(2) + "px," + cy.toFixed(2) + "px,0)";
      if (Math.abs(tx - cx) > 0.05 || Math.abs(ty - cy) > 0.05) {
        raf = window.requestAnimationFrame(loop);
      } else { raf = null; }
    }
  })();

  /* --- scroll reveals: fire once, and never hide content --------------- */
  (function initReveals() {
    var items = $$(".rv");
    if (!items.length) { return; }
    if (REDUCED || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        entry.target.classList.add("in");
        io.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach(function (el) { io.observe(el); });

    /* Safety net: anything still hidden after 4s is shown anyway. */
    window.setTimeout(function () {
      items.forEach(function (el) { el.classList.add("in"); });
    }, 4000);
  })();

  /* --- feature showcase: keyboard-accessible tabs ---------------------- */
  (function initTabs() {
    var tabs = $$(".sc-tab");
    if (!tabs.length) { return; }
    var panels = $$("[data-feature-panel]");
    function select(index, focus) {
      tabs.forEach(function (t, i) {
        var on_ = i === index;
        t.setAttribute("aria-selected", on_ ? "true" : "false");
        t.tabIndex = on_ ? 0 : -1;
        if (on_ && focus) { t.focus(); }
      });
      panels.forEach(function (p, i) { p.hidden = i !== index; });
    }
    tabs.forEach(function (tab, i) {
      on(tab, "click", function () { select(i, false); });
      on(tab, "keydown", function (e) {
        var delta = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1
          : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 0;
        if (!delta) { return; }
        e.preventDefault();
        select((i + delta + tabs.length) % tabs.length, true);
      });
    });
  })();

  /* --- workflow: advance the active node, then hold -------------------- */
  (function initFlow() {
    var track = doc.getElementById("flowTrack");
    if (!track || REDUCED) { return; }
    var nodes = track.querySelectorAll(".flow-node");
    if (!nodes.length) { return; }
    var i = 0;
    window.setInterval(function () {
      nodes[i].classList.remove("active");
      i = (i + 1) % nodes.length;
      nodes[i].classList.add("active");
    }, 2600);
  })();

  /* --- execution bars fill when the panel scrolls into view ------------ */
  (function initBars() {
    var card = doc.getElementById("fcExec");
    if (!card) { return; }
    card.querySelectorAll(".fc-bar").forEach(function (bar) {
      var w = bar.style.getPropertyValue("--w");
      if (w) { bar.querySelector("i").style.width = w; }
    });
    if (!("IntersectionObserver" in window) || REDUCED) { card.classList.add("in"); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { card.classList.add("in"); io.disconnect(); }
      });
    }, { threshold: 0.3 });
    io.observe(card);
  })();

  if (doc.readyState === "loading") {
    on(doc, "DOMContentLoaded", boot);
  } else {
    boot();
  }

  /* Detect blocked cookies on the auth pages and say so plainly. */
  (function () {
    var warn = doc.getElementById("cookies-warning");
    if (!warn) { return; }
    var probe = "ra_probe=1";
    doc.cookie = probe + "; path=/; max-age=30; samesite=lax" +
      (location.protocol === "https:" ? "; secure" : "");
    var ok = doc.cookie.indexOf("ra_probe=1") !== -1;
    if (!ok) { warn.hidden = false; }
    else {
      doc.cookie = "ra_probe=1; path=/; max-age=0; samesite=lax" +
        (location.protocol === "https:" ? "; secure" : "");
    }
  })();
})();
