/* The semester calendar.

   Two modes, because the two things you do here have opposite consequences and
   opposite frequencies. Planning an absence spends budget and happens weekly;
   marking a holiday removes a day's lectures from the maths entirely and
   happens a handful of times a term. Sharing one tap between them would make
   the rare, destructive one the easiest mistake to make.

   The day sheet itself lives in daysheet.js — the plan strip opens the same one. */
(function () {
  "use strict";

  var modes = document.getElementById("cal-mode");
  if (!modes) return;

  var hint = document.getElementById("cal-hint");

  var HINTS = {
    absence: "Tap a day to see its classes and plan which ones you'll miss.",
    holiday: "Tap a day to mark it a holiday — its classes stop counting entirely."
  };

  var mode = window.localStorage.getItem("bunkr.calendar-mode") || "absence";

  var rangeCard = document.getElementById("cal-range");

  function setMode(next) {
    mode = next;
    window.localStorage.setItem("bunkr.calendar-mode", next);
    [].forEach.call(modes.querySelectorAll(".tabs__tab"), function (tab) {
      tab.setAttribute("aria-selected", String(tab.dataset.mode === next));
    });
    hint.textContent = HINTS[next];
    // Marking a stretch off is a holiday action; it has no meaning in the other
    // mode, where a range would be a very different (and much worse) mistake.
    if (rangeCard) rangeCard.hidden = next !== "holiday";
  }

  setMode(mode);

  modes.addEventListener("click", function (event) {
    var tab = event.target.closest(".tabs__tab");
    if (tab) setMode(tab.dataset.mode);
  });

  /* ---- holidays ---------------------------------------------------------- */

  function paintHoliday(cell, isHoliday) {
    cell.classList.toggle("is-holiday", isHoliday);
    cell.dataset.kind = isHoliday ? "holiday" : "";
    var tag = cell.querySelector(".cal__tag");
    if (isHoliday && !tag) {
      tag = document.createElement("span");
      tag.className = "cal__tag";
      tag.textContent = "off";
      cell.appendChild(tag);
    } else if (!isHoliday && tag) {
      tag.remove();
    }
  }

  function saveHoliday(cell, isHoliday) {
    cell.disabled = true;
    return window.BunkrApi
      .post("/api/calendar/day", {
        date: cell.dataset.date,
        kind: isHoliday ? "holiday" : null
      })
      .then(function (res) {
        cell.disabled = false;
        if (!res.ok) {
          paintHoliday(cell, !isHoliday);       // put it back
          window.BunkrToast.error(res.body.error || "Couldn't save that day.");
          return false;
        }
        return true;
      });
  }

  function toggleHoliday(cell) {
    if (!cell) return;
    var was = cell.dataset.kind === "holiday";
    paintHoliday(cell, !was);                   // taps must feel instant
    saveHoliday(cell, !was).then(function (ok) {
      if (!ok) return;
      window.BunkrToast.show(
        (was ? "Holiday removed for " : "Holiday added for ") +
        window.BunkrFmt.date(cell.dataset.date),
        {
          onUndo: function () {
            paintHoliday(cell, was);
            return saveHoliday(cell, was);
          }
        }
      );
    });
  }

  /* ---- a whole stretch at once -------------------------------------------- */

  function paintRange(dates, isHoliday) {
    dates.forEach(function (iso) {
      var cell = document.querySelector('.cal__day[data-date="' + iso + '"]');
      if (cell) paintHoliday(cell, isHoliday);
    });
  }

  function markRange(frm, to, name) {
    return window.BunkrApi
      .post("/api/calendar/range", { from: frm, to: to, name: name, kind: "holiday" })
      .then(function (res) {
        if (!res.ok) {
          window.BunkrToast.error(res.body.error || "Couldn't mark those days.");
          return null;
        }
        paintRange(res.body.dates, true);
        return res.body.dates;
      });
  }

  var rangeSave = document.getElementById("range-save");
  if (rangeSave) {
    rangeSave.addEventListener("click", function () {
      var frm = document.getElementById("range-from").value;
      var to = document.getElementById("range-to").value;
      var name = document.getElementById("range-name").value;
      if (!frm || !to) {
        window.BunkrToast.error("Pick both dates first.");
        return;
      }

      rangeSave.disabled = true;
      markRange(frm, to, name).then(function (dates) {
        rangeSave.disabled = false;
        if (!dates) return;
        window.BunkrToast.show(
          "Marked " + dates.length + " days off from " + window.BunkrFmt.date(frm),
          {
            onUndo: function () {
              return window.BunkrApi
                .post("/api/calendar/range", { from: frm, to: to, kind: null })
                .then(function (res) {
                  if (res.ok) paintRange(res.body.dates, false);
                });
            }
          }
        );
      });
    });
  }

  /* ---- one tap, routed by mode ------------------------------------------- */

  var sheet = window.BunkrDaySheet.mount({
    cellSelector: ".cal__day",
    countClass: "cal__count mono",
    onHoliday: toggleHoliday
  });

  document.addEventListener("click", function (event) {
    var cell = event.target.closest(".cal__day");
    if (!cell || cell.classList.contains("is-out") || cell.disabled) return;
    if (mode === "holiday") toggleHoliday(cell);
    else sheet.open(cell.dataset.date);
  });
})();
