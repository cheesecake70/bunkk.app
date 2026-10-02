/* Escaping, in one place.

   Every list Bunkr builds on the client — the day sheet, the skip ladder — is
   assembled as an HTML string, and every one of them interpolates something a
   user typed: a subject's short code, a holiday's name. Each file grew its own
   escape helper, or forgot one, and the ones that forgot were the ones that
   mattered: a holiday called "<img src=x onerror=…>" ran as script the moment
   its day was opened.

   So: one helper, loaded globally beside api.js and toast.js, and the rule that
   goes with it — nothing user-typed reaches an innerHTML string without passing
   through `esc` first. Values the server generates (times, ids, numbers) still
   go through it, because "is this one safe?" is a question worth never asking.

   `attr` is the same thing for a quoted attribute value: `esc` leaves quotes
   alone, which is harmless between tags and is not harmless inside one. */
(function () {
  "use strict";

  var probe = document.createElement("span");

  /* Text context. Round-tripping through textContent escapes &, < and >, which
     is what closes an injection in element content. */
  function esc(value) {
    probe.textContent = value == null ? "" : String(value);
    return probe.innerHTML;
  }

  /* Attribute context: the above, plus the quotes that would end the value and
     let an event handler in beside it. */
  function attr(value) {
    return esc(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  window.BunkrHtml = { esc: esc, attr: attr };
})();
