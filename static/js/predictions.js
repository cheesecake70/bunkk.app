/* "The college hasn't marked this one — what will it be?"

   Three ways to answer, all landing in the same place: one lecture, every
   pending lecture of a subject, or every pending lecture you have. Fifty NU
   lectures is a normal state two months into term, and answering them one at a
   time — with a full page reload after each, which threw away your scroll
   position — was a feature nobody was ever going to finish using.

   Nothing reloads now. Every endpoint answers with the recomputed figures, and
   the page patches itself from those; `bunkk:stats` is how the rest of the page
   hears about it.
*/
(function () {
  "use strict";

  function announce(stats) {
    document.dispatchEvent(new CustomEvent("bunkk:stats", { detail: stats }));
  }

  /* Pressed state lives on the buttons; the server's answer is what sets it, so
     a refused guess can't leave the page claiming something was saved. */
  function paintGroup(group, value) {
    [].forEach.call(group.querySelectorAll(".js-guess"), function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.value === value));
    });
  }

  function paintAll(stats) {
    announce(stats);
  }

  function busy(nodes, state) {
    [].forEach.call(nodes, function (node) { node.disabled = state; });
  }

  function undoWith(previous) {
    return function () {
      return window.BunkkApi
        .post("/api/predictions/bulk", { lectures: previous })
        .then(function (res) {
          if (!res.ok) {
            window.BunkkToast.error(res.body.error || "Couldn't undo that.");
            return;
          }
          announce(res.body.stats);
          /* Restoring is a whole-page concern — a subject's guesses may have
             come from three different places — so let the listeners repaint
             rather than trying to reconstruct which buttons to unpress. */
          window.location.reload();
        });
    };
  }

  function word(predicted, changed) {
    if (predicted === null) {
      return changed === 1 ? "Guess cleared" : "Cleared " + changed + " guesses";
    }
    var name = predicted === "P" ? "present" : "absent";
    return changed === 1
      ? "Guessed " + name
      : "Marked " + changed + " pending as " + name;
  }

  /* ---- one lecture -------------------------------------------------------- */

  function guessOne(button) {
    var group = button.closest(".guess");
    var buttons = group.querySelectorAll(".js-guess");
    var wasPressed = button.getAttribute("aria-pressed") === "true";

    busy(buttons, true);
    var lectureId = button.dataset.lecture;
    var request = wasPressed
      ? window.BunkkApi.del("/api/lectures/" + lectureId + "/prediction")
      : window.BunkkApi.put("/api/lectures/" + lectureId + "/prediction",
                            { predicted: button.dataset.value });

    return request.then(function (res) {
      busy(buttons, false);
      if (!res.ok) {
        window.BunkkToast.error(res.body.error || "Couldn't save that guess.");
        return;
      }
      paintGroup(group, res.body.predicted);
      paintAll(res.body.stats);
      window.BunkkToast.show(word(res.body.predicted, res.body.changed),
                             { onUndo: undoWith(res.body.previous) });
    });
  }

  /* ---- a subject, or all of them ------------------------------------------ */

  function guessMany(button) {
    var value = button.dataset.value || null;       // "" clears
    var subject = button.dataset.subject;
    var siblings = button.parentNode.querySelectorAll("button");

    busy(siblings, true);
    var url = subject
      ? "/api/subjects/" + subject + "/predictions"
      : "/api/predictions/bulk";

    return window.BunkkApi.post(url, { predicted: value }).then(function (res) {
      busy(siblings, false);
      if (!res.ok) {
        window.BunkkToast.error(res.body.error || "Couldn't save those guesses.");
        return;
      }
      if (!res.body.changed) {
        window.BunkkToast.show("Nothing pending to guess at.");
        return;
      }

      /* Per-lecture buttons on this page are now out of step with the server. */
      [].forEach.call(document.querySelectorAll(".guess"), function (group) {
        var lecture = group.querySelector(".js-guess");
        if (!lecture) return;
        if (!subject || group.dataset.subject === subject) {
          paintGroup(group, value);
        }
      });

      paintAll(res.body.stats);
      window.BunkkToast.show(word(value, res.body.changed),
                             { onUndo: undoWith(res.body.previous) });
    });
  }

  document.addEventListener("click", function (event) {
    var many = event.target.closest(".js-guess-all");
    if (many && !many.disabled) return guessMany(many);

    var one = event.target.closest(".js-guess");
    if (one && !one.disabled) guessOne(one);
  });
})();
