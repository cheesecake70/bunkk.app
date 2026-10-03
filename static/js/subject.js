/* One subject's page: the skip ladder, folded away until asked for, and the
   figures that a guess moves.

   Plan asks the same ladder question through a picker; here the subject is
   already the page, so the picker collapses to a single toggle. The answer
   itself is BunkkLadder's — this file only decides when to ask for it. */
(function () {
  "use strict";

  /* ---- repainting after a guess ------------------------------------------- */

  /* Guesses used to reload the page seven seconds later, which threw away the
     scroll position — after every single click, forty lectures down a list. */
  var page = document.querySelector("[data-subject-page]");
  if (page) {
    var subjectId = parseInt(page.dataset.subjectPage, 10);

    document.addEventListener("bunkk:stats", function (event) {
      var stats = event.detail || {};
      var subject = (stats.subjects || []).filter(function (s) {
        return s.id === subjectId;
      })[0];
      if (!subject) return;

      set("worst_pct", subject.worst_pct, "%");
      set("official_pct", subject.official_pct, "%");
      set("best_pct", subject.best_pct, "%");
      set("pending", subject.pending);
      set("present", subject.present);
      set("absent", subject.absent);
      set("can_miss", subject.can_miss);
      set("recover_needed", subject.recover_needed);

      var tile = document.querySelector('[data-tone="worst"]');
      if (tile) tile.className = "stat stat--" + subject.verdict;

      var meter = document.querySelector("[data-meter]");
      if (meter && subject.worst_pct !== null) {
        meter.style.width = Math.max(0, Math.min(100, subject.worst_pct)) + "%";
      }

      /* The ladder was computed against the old numbers. */
      loaded = false;
    });
  }

  function set(name, value, suffix) {
    [].forEach.call(document.querySelectorAll('[data-stat="' + name + '"]'),
      function (node) {
        node.textContent = value === null || value === undefined
          ? "—"
          : (typeof value === "number" && suffix === "%"
              ? Math.round(value) + "%"
              : String(value));
      });
  }

  /* ---- the ladder --------------------------------------------------------- */

  var toggle = document.getElementById("ladder-toggle");
  var host = document.getElementById("ladder");
  var loaded = false;
  if (!toggle || !host) return;

  toggle.addEventListener("click", function () {
    if (toggle.getAttribute("aria-expanded") === "true") {
      host.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.textContent = "Show me";
      return;
    }

    host.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = "Hide";

    /* The ladder only moves when attendance does, and marking a lecture
       reloads the page — so once fetched it stays good for this visit. */
    if (loaded) return;
    toggle.classList.add("is-loading");
    window.BunkkLadder
      .load(host, toggle.dataset.subject, { lead: "short" })
      .then(function (ok) {
        toggle.classList.remove("is-loading");
        loaded = ok;
        if (!ok) {
          host.hidden = true;
          toggle.setAttribute("aria-expanded", "false");
          toggle.textContent = "Show me";
        }
      });
  });
})();
