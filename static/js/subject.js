/* One subject's page: the skip ladder, folded away until asked for.

   Plan asks the same question through a picker; here the subject is already
   the page, so the picker collapses to a single toggle. The answer itself is
   BunkrLadder's — this file only decides when to ask for it. */
(function () {
  "use strict";

  var toggle = document.getElementById("ladder-toggle");
  var host = document.getElementById("ladder");
  if (!toggle || !host) return;

  var loaded = false;

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
    window.BunkrLadder
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
