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

  function toggle(button) {
    var planned = button.dataset.absenceId;
    button.disabled = true;

    var request = planned
      ? window.BunkrApi.del("/api/absences/" + planned)
      : window.BunkrApi.post("/api/absences", {
          date: button.dataset.date,
          subject_id: parseInt(button.dataset.subject, 10),
          start: button.dataset.start
        });

    return request.then(function (res) {
      button.disabled = false;
      if (!res.ok) {
        window.BunkrToast.error(res.body.error || "Couldn't change that.");
        return;
      }

      setState(button, planned ? null : res.body.absence_id);
      button.dataset.ownAbsence = button.dataset.absenceId || "";
      repaint(res.body);

      window.BunkrToast.show(
        planned ? button.dataset.code + " back on"
                : button.dataset.code + " skip planned",
        { onUndo: function () { return toggle(button); } }
      );
    });
  }

  function toggleDay(button) {
    var planned = button.dataset.absenceId;
    button.disabled = true;

    var request = planned
      ? window.BunkrApi.del("/api/absences/" + planned)
      : window.BunkrApi.post("/api/absences", { date: button.dataset.date });

    return request.then(function (res) {
      button.disabled = false;
      if (!res.ok) {
        window.BunkrToast.error(res.body.error || "Couldn't change that.");
        return;
      }

      setDayState(button, planned ? null : res.body.absence_id);
      repaint(res.body);

      window.BunkrToast.show(
        planned ? "Back on for the whole day" : "Skipping the whole day",
        { onUndo: function () { return toggleDay(button); } }
      );
    });
  }

  document.addEventListener("click", function (event) {
    var day = event.target.closest(".js-skip-day");
    if (day && !day.disabled) return toggleDay(day);

    var button = event.target.closest(".js-skip-lecture");
    if (button && !button.disabled) toggle(button);
  });
})();
