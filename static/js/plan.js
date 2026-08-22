/* Plan mode.

   Two interactions: click a day to open its sheet and choose which classes
   you'll miss, and ask what skipping N lectures of one subject would cost.

   The day used to be a single toggle — whole day on, whole day off. That made
   the common case ("I'll skip the two labs but still go to the lecture")
   impossible to say here, even though the calendar's day sheet could already
   say it. Both pages now open the same sheet.

   Verdicts repaint from the wallet the endpoint returns, so the strip
   recolours as commitments land. The summary figures still come from the
   server — they are the point of the page and must never be guessed at. */
(function () {
  "use strict";

  var strip = document.querySelector(".day-strip");
  var ladderHost = document.getElementById("ladder");
  var subjectPicker = document.getElementById("ladder-subject");

  /* ---- the strip --------------------------------------------------------- */

  function repaint(payload) {
    var byDate = {};
    (payload.days || []).forEach(function (day) { byDate[day.date] = day; });

    document.querySelectorAll(".verdict[data-date]").forEach(function (cell) {
      var day = byDate[cell.dataset.date];
      if (!day) return;
      cell.className = cell.className.replace(/verdict--(skip|partial|go|off)/g, "");
      cell.classList.add("verdict--" + day.verdict);
      cell.title = day.reason || "";
      var tag = cell.querySelector(".verdict__tag");
      /* Labels come from the server so a repaint can't drift from what the page
         was rendered with — these used to be a third hardcoded copy. */
      if (tag) tag.textContent = (window.BUNKR_VERDICTS || {})[day.verdict] || day.verdict;
    });

    var budget = document.querySelector("[data-wallet-budget]");
    if (budget) budget.textContent = payload.overall_budget;
    var planned = document.querySelector("[data-wallet-planned]");
    if (planned) planned.textContent = payload.overall_planned;
  }

  if (strip) {
    var sheet = window.BunkrDaySheet.mount({
      cellSelector: ".verdict",
      countClass: "verdict__count mono",
      onChange: function (payload, date, cell) {
        repaint(payload);
        if (cell) cell.classList.toggle("is-planned", !!cell.querySelector(".verdict__count"));
      },
      /* The subjects table and the committed-absences list below were both
         rendered before these commitments; one reload is cheaper and more
         honest than teaching the client to patch two more structures. */
      onDone: function () { window.location.reload(); }
    });

    document.addEventListener("click", function (event) {
      var cell = event.target.closest(".js-day");
      if (cell && !cell.disabled) sheet.open(cell.dataset.date, cell.dataset.label);
    });
  }

  /* ---- the skip ladder --------------------------------------------------- */

  if (subjectPicker) {
    subjectPicker.addEventListener("change", function () {
      if (!subjectPicker.value) {
        ladderHost.innerHTML = "";
        return;
      }
      window.BunkrLadder.load(ladderHost, subjectPicker.value);
    });
  }

  /* ---- dropping a committed absence from the list ------------------------ */

  document.addEventListener("click", function (event) {
    var drop = event.target.closest(".js-drop");
    if (!drop) return;
    drop.disabled = true;
    window.BunkrApi.del("/api/absences/" + drop.dataset.id).then(function (res) {
      if (!res.ok) {
        drop.disabled = false;
        window.BunkrToast.error(res.body.error || "That didn't work.");
        return;
      }
      window.location.reload();
    });
  });
})();
