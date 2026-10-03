/* Today: plan or unplan the day's classes — one at a time, or all at once.

   Each button is a toggle over one PlannedAbsence. It carries its own state in
   `data-absence-id`, which the server renders too — so the label survives a
   reload, which is what the old version got wrong: it flipped to "Skipping",
   then reloaded on a timer into a page that always said "Skip this".

   "Skip whole day" writes the same subject-less absence the calendar's day
   sheet does. One row covers every lecture on the date, so while it is on the
   per-lecture buttons are frozen: they can't be undone individually, only as a
   whole. Each of them remembers its own absence in `data-own-absence`, so
   lifting the day plan puts the page back the way it was underneath.

   Nothing reloads. The absence endpoints return the whole recomputed wallet,
   so the figures that actually moved are patched from that. */
(function () {
  "use strict";

  function lectureButtons() {
    return document.querySelectorAll(".js-skip-lecture");
  }

  function setState(button, absenceId) {
    var row = button.closest(".lecture");
    if (absenceId) {
      button.dataset.absenceId = absenceId;
      button.textContent = "Skipping";
      button.classList.add("btn--danger");
      if (row) row.classList.add("is-planned");
    } else {
      delete button.dataset.absenceId;
      button.textContent = "Skip this";
      button.classList.remove("btn--danger");
      if (row) row.classList.remove("is-planned");
    }
  }

  function setDayState(button, absenceId) {
    if (absenceId) {
      button.dataset.absenceId = absenceId;
      button.textContent = "Skipping all day";
      button.classList.add("btn--danger");
    } else {
      delete button.dataset.absenceId;
      button.textContent = "Skip whole day";
      button.classList.remove("btn--danger");
    }

    lectureButtons().forEach(function (lecture) {
      lecture.disabled = !!absenceId;
      if (absenceId) {
        lecture.title = "The whole day is planned off";
        setState(lecture, absenceId);
      } else {
        lecture.removeAttribute("title");
        setState(lecture, lecture.dataset.ownAbsence || null);
      }
    });
  }

  /* Only the per-subject budget badges and the unreported tile live on this
     page; everything else the payload carries belongs to /plan. */
  function repaint(payload) {
    (payload.subjects || []).forEach(function (subject) {
      document.querySelectorAll(
        '.js-skip-lecture[data-subject="' + subject.id + '"]'
      ).forEach(function (button) {
        var badge = button.closest(".lecture").querySelector(".badge");
        if (!badge) return;
        badge.textContent = subject.budget + " left";
        badge.className = "badge badge--" + subject.verdict;
      });
    });
  }

  /* The day's own verdict, repainted from the payload rather than reloaded.
     Committing used to leave a green SKIP hero over a day you had just spent
     past its budget — the numbers below it moved, the headline didn't. */
  function paintHero(day) {
    var hero = document.getElementById("hero");
    if (!hero || !day) return;

    hero.className = "hero-verdict hero-verdict--" + day.verdict +
      (day.over_budget ? " is-over" : "");

    var label = hero.querySelector(".hero-verdict__label");
    if (label) {
      /* The word underneath already says PLANNED; this line is worth more as
         the date, or as the warning when there is one. */
      label.textContent = day.over_budget
        ? "Planned · over budget"
        : (hero.dataset.label || label.textContent);
    }

    var word = hero.querySelector(".hero-verdict__word");
    if (word) {
      var words = window.BUNKK_VERDICTS || {};
      word.textContent = (words[day.verdict] || day.verdict).toUpperCase();
    }

    var reason = hero.querySelector(".hero-verdict__reason");
    if (reason) reason.textContent = day.reason || "";

    var meta = hero.querySelector(".hero-verdict__meta");
    var shape = day.whole_day ? "whole day"
      : (day.leave_after ? "leaving after " + window.BunkkFmt.time(day.leave_after)
      : (day.arrive_at ? "arriving by " + window.BunkkFmt.time(day.arrive_at) : ""));
    if (!meta && shape) {
      meta = document.createElement("span");
      meta.className = "hero-verdict__meta";
      hero.appendChild(meta);
    }
    if (meta) meta.textContent = shape;
  }

  function toggle(button) {
    var planned = button.dataset.absenceId;

    /* Only a new commitment is worth checking: dropping one can't break
       anything, and asking "are you sure?" about giving budget back is noise. */
    var ready = planned ? Promise.resolve(true) : window.BunkkCommit.guard([{
      date: button.dataset.date,
      subject_id: parseInt(button.dataset.subject, 10),
      start: button.dataset.start
    }]);

    return ready.then(function (go) {
      if (!go) return;
      button.disabled = true;

      var request = planned
        ? window.BunkkApi.del("/api/absences/" + planned)
        : window.BunkkApi.post("/api/absences", {
            date: button.dataset.date,
            subject_id: parseInt(button.dataset.subject, 10),
            start: button.dataset.start
          });

      return request.then(function (res) {
        button.disabled = false;
        if (!res.ok) {
          window.BunkkToast.error(res.body.error || "Couldn't change that.");
          return;
        }

        setState(button, planned ? null : res.body.absence_id);
        button.dataset.ownAbsence = button.dataset.absenceId || "";
        repaint(res.body);
        paintHero(res.body.day);

        var undo = function () { return toggle(button); };
        if (!window.BunkkCommit.report(res.body, { onUndo: undo })) {
          window.BunkkToast.show(
            planned ? button.dataset.code + " back on"
                    : button.dataset.code + " skip planned",
            { onUndo: undo }
          );
        }
      });
    });
  }

  function toggleDay(button) {
    var planned = button.dataset.absenceId;
    var ready = planned
      ? Promise.resolve(true)
      : window.BunkkCommit.guard([{ date: button.dataset.date }]);

    return ready.then(function (go) {
      if (!go) return;
      button.disabled = true;

      var request = planned
        ? window.BunkkApi.del("/api/absences/" + planned)
        : window.BunkkApi.post("/api/absences", { date: button.dataset.date });

      return request.then(function (res) {
        button.disabled = false;
        if (!res.ok) {
          window.BunkkToast.error(res.body.error || "Couldn't change that.");
          return;
        }

        setDayState(button, planned ? null : res.body.absence_id);
        repaint(res.body);
        paintHero(res.body.day);

        var undo = function () { return toggleDay(button); };
        if (!window.BunkkCommit.report(res.body, { onUndo: undo })) {
          window.BunkkToast.show(
            planned ? "Back on for the whole day" : "Skipping the whole day",
            { onUndo: undo }
          );
        }
      });
    });
  }

  document.addEventListener("click", function (event) {
    var day = event.target.closest(".js-skip-day");
    if (day && !day.disabled) return toggleDay(day);

    var button = event.target.closest(".js-skip-lecture");
    if (button && !button.disabled) toggle(button);
  });
})();
