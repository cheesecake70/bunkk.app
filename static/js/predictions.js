/* "The college hasn't marked this one — what will it be?"

   Pressing the same answer again clears the guess, so there is always a way
   back to "I don't know" without hunting for a third button. Every guess moves
   real numbers, so the page reloads once the toast has had its say. */
(function () {
  "use strict";

  var pending = null;

  function send(button) {
    var group = button.closest(".guess");
    var lectureId = button.dataset.lecture;
    var wasPressed = button.getAttribute("aria-pressed") === "true";

    var buttons = [].slice.call(group.querySelectorAll(".js-guess"));
    buttons.forEach(function (b) { b.disabled = true; });

    var request = wasPressed
      ? window.BunkrApi.del("/api/lectures/" + lectureId + "/prediction")
      : window.BunkrApi.put("/api/lectures/" + lectureId + "/prediction",
                            { predicted: button.dataset.value });

    return request.then(function (res) {
      buttons.forEach(function (b) { b.disabled = false; });
      if (!res.ok) {
        window.BunkrToast.error(res.body.error || "Couldn't save that guess.");
        return;
      }

      buttons.forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.value === res.body.predicted));
      });

      window.BunkrToast.show(
        res.body.predicted
          ? "Guessed " + (res.body.predicted === "P" ? "present" : "absent")
          : "Guess cleared",
        { onUndo: function () { return send(button); } }
      );

      /* Percentages elsewhere on the page are now stale. Reload after the
         toast, not before, so Undo stays reachable. */
      if (pending) clearTimeout(pending);
      pending = setTimeout(function () { window.location.reload(); }, 7200);
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest(".js-guess");
    if (button && !button.disabled) send(button);
  });
})();
