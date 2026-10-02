/* Committing an absence that breaks a limit.

   The app has never refused a plan and shouldn't start: the semester is the
   student's, and they may well have a reason to spend past the line. What it
   was doing instead was accepting the over-spend in silence, with a cheerful
   toast — you had to go to /plan and read the banner to find out that the day
   you just wrote off had cost you four subjects.

   So: ask first, say so after. `guard` runs the same simulation the server
   would run anyway, but with `light` so it skips projecting the strip nobody is
   about to look at, and only interrupts when the plan breaks something it
   wasn't already breaking — nagging about a limit you blew last week would
   train the dialog straight into muscle memory.
*/
(function () {
  "use strict";

  var esc = window.BunkrHtml.esc;

  /* What the page was already breaking when it rendered. Pages seed this; a
     commit that changes nothing about that set is not worth a dialog. */
  var baseline = { is_safe: true, breaks: [], overall_breaks: false };

  function observe(payload) {
    if (!payload || typeof payload.is_safe !== "boolean") return;
    baseline = {
      is_safe: payload.is_safe,
      breaks: payload.breaks || [],
      overall_breaks: !!payload.overall_breaks
    };
  }

  function newBreaks(sim) {
    if (!sim) return [];
    var had = baseline.breaks || [];
    var fresh = (sim.breaks || []).filter(function (code) {
      return had.indexOf(code) === -1;
    });
    if (sim.overall_breaks && !baseline.overall_breaks) fresh.push("your overall limit");
    return fresh;
  }

  function sentence(codes) {
    if (codes.length === 1) return codes[0];
    return codes.slice(0, -1).join(", ") + " and " + codes[codes.length - 1];
  }

  /* ---- the dialog -------------------------------------------------------- */

  function ask(title, body) {
    var sheet = document.getElementById("confirm-sheet");
    if (!sheet || typeof sheet.showModal !== "function") {
      return Promise.resolve(window.confirm(title + "\n\n" + body));
    }

    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-body").textContent = body;

    var yes = document.getElementById("confirm-yes");
    var no = document.getElementById("confirm-no");

    return new Promise(function (resolve) {
      var settled = false;

      /* Each button answers directly rather than through the dialog's `close`
         event. The event is the tidier route on paper, but it is one path for
         three outcomes, and it does not fire at all in some embedded browsers —
         which left the dialog closing on "Plan it anyway" and nothing being
         planned. `close` stays as the catch-all for Escape and the backdrop. */
      function done(answer) {
        if (settled) return;
        settled = true;
        sheet.removeEventListener("close", onDismiss);
        yes.removeEventListener("click", onYes);
        no.removeEventListener("click", onNo);
        resolve(answer);
      }

      /* An ambiguous dismissal reads as "no": the safe answer is the one that
         doesn't spend anything. */
      function onDismiss() { done(false); }
      function onYes() { sheet.close(); done(true); }
      function onNo() { sheet.close(); done(false); }

      sheet.addEventListener("close", onDismiss);
      yes.addEventListener("click", onYes);
      no.addEventListener("click", onNo);
      sheet.showModal();
    });
  }

  /* ---- the guard --------------------------------------------------------- */

  function guard(absences) {
    if (!absences || !absences.length) return Promise.resolve(true);

    return window.BunkrApi
      .post("/api/simulate", { absences: absences, light: true })
      .then(function (res) {
        /* A failed check must not block a commit: the server validates the
           real request anyway, and a network blip is not a reason to refuse
           someone their own plan. */
        if (!res.ok) return true;

        var codes = newBreaks(res.body);
        if (!codes.length) return true;

        return ask(
          "This breaks " + sentence(codes),
          "Planning this would put " + sentence(codes) + " below the limit for " +
          "the rest of the term. You can still do it — this is your call."
        );
      });
  }

  /* Say it again after the fact, because the dialog is dismissible and the
     numbers on screen have just moved. Undo is the caller's. */
  function report(payload, options) {
    var codes = newBreaks(payload);
    observe(payload);
    if (!codes.length) return false;
    window.BunkrToast.show("Planned — this breaks " + sentence(codes) + ".",
                           { onUndo: options && options.onUndo, tone: "danger" });
    return true;
  }

  window.BunkrCommit = {
    observe: observe,
    newBreaks: newBreaks,
    guard: guard,
    report: report,
    ask: ask,
    esc: esc
  };
})();
