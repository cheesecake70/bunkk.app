/* "Timetable updated · Undo · Confirm"

   Saves land immediately — the toast reports what happened rather than asking
   permission for it. Undo runs the inverse of the action that raised it, so
   every caller supplies its own undo rather than this file guessing one.
   Confirm only dismisses; letting the toast time out means the same thing,
   which is why the change is already written by the time you see this. */
(function () {
  "use strict";

  var DISMISS_MS = 7000;
  var host = null;

  function ensureHost() {
    if (host && document.body.contains(host)) return host;
    host = document.createElement("div");
    host.className = "toast-host";
    /* polite, not assertive: a save confirmation shouldn't interrupt whatever
       a screen-reader user is in the middle of. */
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
    return host;
  }

  function showToast(message, options) {
    options = options || {};
    var onUndo = options.onUndo;

    var toast = document.createElement("div");
    /* `tone: "danger"` for a save that landed but cost more than it had — it is
       still a confirmation with an Undo, not an error, so it keeps its actions. */
    toast.className = "toast" + (options.tone ? " toast--" + options.tone : "");

    var text = document.createElement("span");
    text.className = "toast__text";
    text.textContent = message;
    toast.appendChild(text);

    var timer = null;
    var done = false;

    function close() {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      toast.classList.add("is-leaving");
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 150);
    }

    if (typeof onUndo === "function") {
      var undo = document.createElement("button");
      undo.type = "button";
      undo.className = "toast__action toast__action--undo";
      undo.textContent = "Undo";
      undo.addEventListener("click", function () {
        undo.disabled = true;
        /* Close on the way out regardless: if the undo itself fails, the
           caller raises its own error toast, and two stacked toasts about the
           same action read as one thing having gone wrong twice. */
        Promise.resolve(onUndo()).then(close, close);
      });
      toast.appendChild(undo);
    }

    /* "OK", not "Confirm": the change is already written by the time this is on
       screen, and asking people to confirm what has happened made them think
       the save was waiting on them. */
    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "toast__action";
    dismiss.textContent = "OK";
    dismiss.addEventListener("click", close);
    toast.appendChild(dismiss);

    ensureHost().appendChild(toast);
    timer = setTimeout(close, DISMISS_MS);
    return { close: close };
  }

  /* Failures get the same surface, minus the actions — there is nothing to
     undo when nothing was written. */
  function showError(message) {
    var toast = document.createElement("div");
    toast.className = "toast toast--danger";
    toast.textContent = message;
    ensureHost().appendChild(toast);
    setTimeout(function () {
      toast.classList.add("is-leaving");
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 150);
    }, DISMISS_MS);
  }

  window.BunkkToast = { show: showToast, error: showError };
})();
