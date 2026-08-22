/* Per-subject saving.

   The page is still one form and still posts the lot — that path works without
   JavaScript and is what the Save subjects button at the bottom does. This adds
   the other half: a Save button on the card you are actually typing in, so
   renaming one course never depends on noticing a button three screens down. */
(function () {
  "use strict";

  var cards = document.querySelectorAll(".js-subject");
  if (!cards.length) return;

  var FIELDS = ["name", "code", "custom_limit"];

  function inputs(card) {
    var found = {};
    FIELDS.forEach(function (field) {
      found[field] = card.querySelector('[data-field="' + field + '"]');
    });
    return found;
  }

  /* The baseline is the server-rendered `value` attribute, which is what
     `defaultValue` reports and — unlike `value` — does not move as you type.
     Measuring against it rather than a dirty flag means typing a change and
     typing it back hides the button again. */
  function isDirty(card) {
    var found = inputs(card);
    return FIELDS.some(function (field) {
      return found[field] && found[field].value !== found[field].defaultValue;
    });
  }

  function rebase(card) {
    var found = inputs(card);
    FIELDS.forEach(function (field) {
      if (found[field]) found[field].defaultValue = found[field].value;
    });
  }

  function setError(card, field, message) {
    var input = card.querySelector('[data-field="' + field + '"]');
    var slot = card.querySelector('[data-error-for="' + field + '"]');
    if (input) input.classList.toggle("is-error", !!message);
    if (slot) {
      slot.textContent = message || "";
      slot.hidden = !message;
    }
  }

  function clearErrors(card) {
    FIELDS.forEach(function (field) { setError(card, field, ""); });
  }

  function save(card) {
    var button = card.querySelector(".js-save");
    var found = inputs(card);
    var payload = {};
    FIELDS.forEach(function (field) {
      if (found[field]) payload[field] = found[field].value;
    });

    button.disabled = true;
    clearErrors(card);

    window.BunkrApi
      .put("/api/subjects/" + card.dataset.subjectId, payload)
      .then(function (res) {
        button.disabled = false;

        if (!res.ok) {
          var errors = res.body.errors;
          if (errors) {
            Object.keys(errors).forEach(function (field) {
              setError(card, field, errors[field]);
            });
          } else {
            window.BunkrToast.error(res.body.error || "Couldn't save that.");
          }
          return;
        }

        /* The saved values become the new baseline, so the button goes away and
           only comes back on a further change. */
        rebase(card);
        button.hidden = true;
        window.BunkrToast.show("Saved " + (res.body.subject.code || "subject") + ".");
      });
  }

  function refresh(card) {
    card.querySelector(".js-save").hidden = !isDirty(card);
  }

  document.addEventListener("input", function (event) {
    var card = event.target.closest(".js-subject");
    if (card && event.target.dataset.field) refresh(card);
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest(".js-save");
    if (button) save(button.closest(".js-subject"));
  });

  /* Enter in a subject field should save that subject, not submit the whole
     page — the card is the unit you were working in. */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    var card = event.target.closest(".js-subject");
    if (!card || !event.target.dataset.field || !isDirty(card)) return;
    event.preventDefault();
    save(card);
  });
})();
