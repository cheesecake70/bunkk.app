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

  var grid = document.querySelector(".plan-month");
  var ladderHost = document.getElementById("ladder");
  var subjectPicker = document.getElementById("ladder-subject");

  /* ---- the month grid ---------------------------------------------------- */

  function repaint(payload) {
    var byDate = {};
    (payload.days || []).forEach(function (day) { byDate[day.date] = day; });

    document.querySelectorAll(".plan-month .cal__day[data-date]").forEach(function (cell) {
      var day = byDate[cell.dataset.date];
      if (!day) return;
      cell.className = cell.className.replace(/\s*verdict--\S+/g, "");
      cell.classList.add("verdict--" + day.verdict);
      /* The day is still committed either way; over budget is what changed
         about it, and the colour is the only thing that says so at this size. */
      cell.classList.toggle("is-over", !!day.over_budget);
      cell.title = day.reason || "";
    });
    /* Only the grid is patched here. The wallet figures and the subjects table
       were rendered server-side and are caught up by the reload on Done —
       there were two selectors for them that no template has ever rendered, so
       what looked like a live update was a silent no-op. */
  }

  if (grid) {
    var sheet = window.BunkrDaySheet.mount({
      cellSelector: ".plan-month .cal__day",
      countClass: "cal__count mono",
      onChange: function (payload) {
        repaint(payload);
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

  /* ---- repainting after a bulk guess -------------------------------------- */

  /* A guess moves every figure on this page at once, and reloading to show that
     would throw away where you were in a fourteen-row table. */
  function pct(value) {
    return value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  }

  function cell(row, name, value) {
    var node = row.querySelector('[data-col="' + name + '"]');
    if (node && node.textContent.trim() !== "—") node.textContent = value;
  }

  /* Mirrors templates/_macros.html::status_badge. Two copies of a rule is how
     surfaces drift, so keep them in step: a subject whose pending lectures have
     all been guessed must not still be badged "9 pending". */
  function badge(subject, plan) {
    var verdict = plan ? plan.verdict : subject.verdict;
    var headroom = plan ? plan.budget : subject.can_miss;

    if (verdict === "safe") return ["safe", "Safe"];
    if (verdict === "warn") {
      return ["warn", headroom === 0 ? "No slack yet" : "Tight"];
    }
    if (subject.pending_dominated) {
      return ["pending", subject.pending + " pending"];
    }
    if (verdict === "danger") return ["danger", "Attend " + subject.recover_needed];
    return ["neutral", "No data"];
  }

  document.addEventListener("bunkr:stats", function (event) {
    var stats = event.detail || {};

    var overall = document.querySelector('[data-stat="overall_worst"]');
    if (overall && stats.overall) {
      overall.textContent = Math.round(stats.overall.worst_pct) + "%";
      var tile = document.querySelector('[data-tone="overall"]');
      if (tile) tile.className = "stat stat--" + stats.overall.verdict;
    }

    var budgets = {};
    ((stats.wallet || {}).subjects || []).forEach(function (s) { budgets[s.id] = s; });

    var walletTile = document.querySelector('[data-stat="overall_budget"]');
    if (walletTile && stats.wallet) {
      walletTile.textContent = stats.wallet.overall_budget;
    }

    (stats.subjects || []).forEach(function (subject) {
      var row = document.querySelector('[data-subject-row="' + subject.id + '"]');
      if (!row) return;
      cell(row, "worst_pct", pct(subject.worst_pct));
      cell(row, "best_pct", pct(subject.best_pct));
      cell(row, "pending", subject.pending);

      var plan = budgets[subject.id];
      if (plan) {
        cell(row, "budget", plan.budget);
        cell(row, "planned", plan.planned);
        cell(row, "projected_pct", pct(plan.projected_pct));
      }

      var status = row.querySelector('[data-col="status"]');
      if (status) {
        var pair = badge(subject, plan);
        status.innerHTML = '<span class="badge badge--' + pair[0] + '">' +
          window.BunkrHtml.esc(pair[1]) + "</span>";
      }
    });
  });

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
