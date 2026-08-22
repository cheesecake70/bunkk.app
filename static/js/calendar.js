/* Semester calendar: tap a day to cycle normal -> holiday -> swap -> normal.
   A swap needs to know whose timetable runs, so it asks once when chosen. */
(function () {
  "use strict";

  var WEEKDAYS = window.BUNKR_WEEKDAYS || ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function nextKind(current) {
    if (!current) return "holiday";
    if (current === "holiday") return "swap";
    return "";                       // back to a normal day
  }

  function paint(cell, kind, swapWeekday) {
    cell.classList.toggle("is-holiday", kind === "holiday");
    cell.classList.toggle("is-swap", kind === "swap");
    cell.dataset.kind = kind || "";
    cell.dataset.swap = (swapWeekday === null || swapWeekday === undefined) ? "" : swapWeekday;

    var tag = cell.querySelector(".cal__tag");
    var label = kind === "holiday" ? "off"
      : kind === "swap" ? WEEKDAYS[swapWeekday]
      : null;

    if (!label) {
      if (tag) tag.remove();
      return;
    }
    if (!tag) {
      tag = document.createElement("span");
      tag.className = "cal__tag";
      cell.appendChild(tag);
    }
    tag.textContent = label;
  }

  function askWeekday() {
    var answer = window.prompt(
      "Which day's timetable runs?\n0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun", "0");
    if (answer === null) return null;
    var n = parseInt(answer, 10);
    return (n >= 0 && n <= 6) ? n : null;
  }

  document.addEventListener("click", function (event) {
    var cell = event.target.closest(".cal__day");
    if (!cell || cell.classList.contains("is-out") || cell.disabled) return;

    var kind = nextKind(cell.dataset.kind);
    var swapWeekday = null;

    if (kind === "swap") {
      swapWeekday = askWeekday();
      if (swapWeekday === null) { kind = ""; }   // cancelled -> back to normal
    }

    var previous = { kind: cell.dataset.kind, swap: cell.dataset.swap };
    paint(cell, kind, swapWeekday);              // optimistic: taps must feel instant
    cell.disabled = true;

    fetch("/api/calendar/day", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date: cell.dataset.date,
        kind: kind || null,
        swap_weekday: swapWeekday
      })
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) {
          paint(cell, previous.kind, previous.swap === "" ? null : parseInt(previous.swap, 10));
          alert(res.body.error || "Couldn't save that day.");
        }
      })
      .catch(function () {
        paint(cell, previous.kind, previous.swap === "" ? null : parseInt(previous.swap, 10));
        alert("Couldn't reach the server — that day wasn't saved.");
      })
      .then(function () { cell.disabled = false; });
  });
})();
