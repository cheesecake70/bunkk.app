/* Dates and times for humans.

   Everything on the wire is ISO — "2026-08-27", "09:00:00" — and that is right
   for a wire. It was also what the toasts and the day sheet's title were showing
   people, so the app spoke one dialect to itself and made the student learn it.

   Fixed English month and day names rather than toLocaleDateString: the server
   renders "Thursday 27 Aug" from strftime, and two spellings of the same day on
   the same screen is worse than one that ignores the browser's locale.
*/
(function () {
  "use strict";

  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday"];
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* Parsed by hand: `new Date("2026-08-27")` is UTC midnight, which is the day
     before in every timezone west of Greenwich. */
  function parse(iso) {
    var parts = String(iso || "").split("-");
    if (parts.length !== 3) return null;
    var d = new Date(+parts[0], +parts[1] - 1, +parts[2]);
    return isNaN(d.getTime()) ? null : d;
  }

  function date(iso) {
    var d = parse(iso);
    if (!d) return String(iso || "");
    return DAYS[d.getDay()].slice(0, 3) + " " + d.getDate() + " " + MONTHS[d.getMonth()];
  }

  function longDate(iso) {
    var d = parse(iso);
    if (!d) return String(iso || "");
    return DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()];
  }

  /* "09:00:00" and "09:00" both become "09:00". */
  function time(value) {
    return String(value || "").slice(0, 5);
  }

  window.BunkrFmt = { date: date, longDate: longDate, time: time, parse: parse };
})();
