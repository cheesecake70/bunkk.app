/* The weekly grid editor.

   Blocks are the source of truth in the DOM. Pressing Done in the editor now
   writes the whole grid straight through to the server, so an edit is never
   left sitting in a page you might navigate away from. The form and its submit
   button stay in the markup as the no-JavaScript path; this file swaps the
   button for a saved-status line once it has taken over.

   Free periods are drawn as break blocks as you edit, mirroring
   `planning.fill_gaps` on the server (which seeds the first drafted grid).
   Filling them here rather than on save is what lets a break you delete stay
   deleted. Keep the two rules in step. */
(function () {
  "use strict";

  var grid = document.getElementById("tt");
  if (!grid) return;

  var form = document.getElementById("grid-form");
  var fields = document.getElementById("grid-fields");
  var dialog = document.getElementById("block-editor");
  var actions = document.getElementById("tt-actions");

  var kindEl = document.getElementById("editor-kind");
  var subjectEl = document.getElementById("editor-subject");
  var subjectField = document.getElementById("editor-subject-field");
  var labelEl = document.getElementById("editor-label");
  var labelField = document.getElementById("editor-label-field");
  var startEl = document.getElementById("editor-start");
  var endEl = document.getElementById("editor-end");
  var errorEl = document.getElementById("editor-error");
  var titleEl = document.getElementById("editor-title");
  var deleteBtn = document.getElementById("editor-delete");

  var editing = null;        // the block being edited, or null when adding
  var editingWeekday = 0;

  /* ---- the saved-status line, in place of a Save button ------------------ */

  var status = document.createElement("span");
  status.id = "tt-status";
  status.className = "tt-status";
  status.setAttribute("aria-live", "polite");

  function setStatus(text, state) {
    status.textContent = text;
    status.dataset.state = state || "";
  }

  /* A drafted grid keeps its Save button: nothing is stored yet, and
     autosaving a week the user hasn't read would commit guesses on their
     behalf. Once accepted, the page comes back without the draft flag and this
     takes over. */
  var isDraft = !!(actions && actions.dataset.draft);

  if (actions && !isDraft) {
    var submitBtn = actions.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.replaceWith(status);
    else actions.prepend(status);
    setStatus("All changes saved", "saved");
  }

  /* ---- gap filling ------------------------------------------------------- */

  var MIN_GAP_MINUTES = 15;
  var MAX_GAP_MINUTES = 180;

  function minutes(hhmm) {
    var parts = hhmm.split(":");
    return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
  }

  function gapLabel(start, mins) {
    var at = minutes(start);
    var midday = at >= minutes("11:30") && at <= minutes("13:30");
    return mins >= 45 && midday ? "Lunch" : "";
  }

  function fillGaps() {
    [].forEach.call(grid.querySelectorAll(".tt__blocks"), function (column) {
      var blocks = [].slice.call(column.querySelectorAll(".tt__block"));
      for (var i = 0; i + 1 < blocks.length; i++) {
        var prevEnd = blocks[i].dataset.end.slice(0, 5);
        var nextStart = blocks[i + 1].dataset.start.slice(0, 5);
        if (nextStart <= prevEnd) continue;          // touching, or overlapping
        var mins = minutes(nextStart) - minutes(prevEnd);
        if (mins < MIN_GAP_MINUTES || mins > MAX_GAP_MINUTES) continue;

        var block = makeBlock();
        block.dataset.kind = "break";
        block.dataset.weekday = column.dataset.weekday;
        block.dataset.start = prevEnd;
        block.dataset.end = nextStart;
        block.dataset.subject = "";
        block.dataset.label = gapLabel(prevEnd, mins);
        paint(block);
        column.insertBefore(block, blocks[i + 1]);
        blocks.splice(i + 1, 0, block);
        i++;                                          // skip what we just added
      }
    });
  }

  /* ---- one day at a time, on a phone -------------------------------------- */

  /* The tabs are hidden by CSS above 640px, where all seven columns fit. The
     class they toggle is only honoured inside that media query, so a desktop
     never loses six days to a stale selection. */
  (function () {
    var tabs = document.querySelector(".tt-daytabs");
    if (!tabs) return;

    function show(weekday) {
      [].forEach.call(grid.querySelectorAll(".tt__col"), function (col) {
        col.classList.toggle("is-day", col.dataset.weekday === String(weekday));
      });
      [].forEach.call(tabs.querySelectorAll(".tabs__tab"), function (tab) {
        tab.setAttribute("aria-selected",
                         String(tab.dataset.weekday === String(weekday)));
      });
    }

    tabs.addEventListener("click", function (event) {
      var tab = event.target.closest(".tabs__tab");
      if (tab) show(tab.dataset.weekday);
    });

    function isEmpty(weekday) {
      var column = grid.querySelector('.tt__blocks[data-weekday="' + weekday + '"]');
      return !column || !column.querySelector(".tt__block");
    }

    // Monday is 0 here; JavaScript's Sunday is 0, hence the shuffle. Opening on
    // an empty Sunday is technically "today" and useless — fall through to the
    // first day that has anything on it.
    var today = new Date().getDay();
    var start = today === 0 ? 6 : today - 1;
    if (isEmpty(start)) {
      for (var i = 1; i <= 6 && isEmpty(start); i++) start = (start + 1) % 7;
    }
    show(start);
  })();

  /* ---- clashes ----------------------------------------------------------- */

  /* Mirrors planning.overlapping on the server, for the same reason fillGaps
     mirrors fill_gaps: the grid has to keep telling the truth while you edit it,
     not only after a reload. */
  function markOverlaps() {
    var blocks = [].slice.call(grid.querySelectorAll('.tt__block[data-kind="class"]'));
    blocks.forEach(function (block) {
      block.classList.remove("is-overlap");
      var hint = block.querySelector(".tt__hint");
      if (hint) hint.remove();
    });

    blocks.forEach(function (a, i) {
      blocks.slice(i + 1).forEach(function (b) {
        if (a.dataset.weekday !== b.dataset.weekday) return;
        if (a.dataset.start < b.dataset.end && b.dataset.start < a.dataset.end) {
          [a, b].forEach(function (block) {
            if (block.classList.contains("is-overlap")) return;
            block.classList.add("is-overlap");
            var hint = document.createElement("span");
            hint.className = "tt__hint";
            hint.textContent = "shares this slot";
            block.appendChild(hint);
          });
        }
      });
    });
  }

  /* ---- persistence ------------------------------------------------------- */

  function collect() {
    return [].map.call(grid.querySelectorAll(".tt__block"), function (block) {
      return {
        kind: block.dataset.kind,
        weekday: block.dataset.weekday,
        start: block.dataset.start,
        end: block.dataset.end,
        subject_id: block.dataset.subject || "",
        label: block.dataset.label || ""
      };
    });
  }

  function persist() {
    if (!actions || isDraft) return;                  // no JS takeover, no autosave
    setStatus("Saving…", "saving");
    window.BunkrApi.put("/api/timetable", { blocks: collect() }).then(function (res) {
      if (!res.ok) {
        setStatus("Couldn't save — reloading", "error");
        window.BunkrToast.error(res.body.error || "Couldn't save the timetable.");
        /* Reload rather than leave the page showing a grid the server rejected:
           what is on screen would otherwise claim to be saved and not be. */
        window.setTimeout(function () { window.location.reload(); }, 1500);
        return;
      }
      setStatus("All changes saved", "saved");
    });
  }

  /* ---- the editor -------------------------------------------------------- */

  function subjectCode(id) {
    var option = subjectEl.querySelector('option[value="' + id + '"]');
    // Options read "CODE · Full name"; the grid only has room for the code.
    return option ? option.textContent.split("·")[0].trim() : "—";
  }

  function syncKindFields() {
    var isBreak = kindEl.value === "break";
    subjectField.hidden = isBreak;
    labelField.hidden = !isBreak;
  }

  function openEditor(block, weekday) {
    editing = block;
    editingWeekday = weekday;
    errorEl.hidden = true;
    deleteBtn.hidden = !block;
    titleEl.textContent = block ? "Edit this block" : "Add to the grid";

    kindEl.value = block ? block.dataset.kind : "class";
    startEl.value = block ? block.dataset.start.slice(0, 5) : "09:00";
    endEl.value = block ? block.dataset.end.slice(0, 5) : "10:00";
    labelEl.value = block ? block.dataset.label : "";
    if (block && block.dataset.subject) subjectEl.value = block.dataset.subject;

    syncKindFields();
    dialog.showModal();
  }

  function paint(block) {
    var isBreak = block.dataset.kind === "break";
    block.className = "tt__block tt__block--" + block.dataset.kind;
    block.querySelector(".tt__time").textContent =
      block.dataset.start.slice(0, 5) + "–" + block.dataset.end.slice(0, 5);
    block.querySelector(".tt__name").textContent = isBreak
      ? (block.dataset.label || "Break")
      : subjectCode(block.dataset.subject);
  }

  function insertInOrder(container, block) {
    // Keep each day in clock order, so the grid reads like a day.
    var siblings = [].slice.call(container.querySelectorAll(".tt__block"));
    var after = null;
    for (var i = 0; i < siblings.length; i++) {
      if (siblings[i] !== block && siblings[i].dataset.start > block.dataset.start) {
        after = siblings[i];
        break;
      }
    }
    container.insertBefore(block, after);
  }

  function makeBlock() {
    var block = document.createElement("div");
    block.className = "tt__block";
    block.tabIndex = 0;
    block.setAttribute("role", "button");
    block.innerHTML = '<span class="tt__time mono"></span><span class="tt__name"></span>';
    return block;
  }

  document.getElementById("editor-save").addEventListener("click", function () {
    if (!startEl.value || !endEl.value) {
      errorEl.textContent = "A block needs a start and an end.";
      errorEl.hidden = false;
      return;
    }
    if (startEl.value >= endEl.value) {
      errorEl.textContent = "It has to end after it starts.";
      errorEl.hidden = false;
      return;
    }

    var block = editing || makeBlock();
    block.dataset.kind = kindEl.value;
    block.dataset.weekday = editingWeekday;
    block.dataset.start = startEl.value;
    block.dataset.end = endEl.value;
    block.dataset.subject = kindEl.value === "class" ? subjectEl.value : "";
    block.dataset.label = kindEl.value === "break" ? labelEl.value : "";

    paint(block);
    insertInOrder(
      grid.querySelector('.tt__blocks[data-weekday="' + editingWeekday + '"]'),
      block
    );
    dialog.close();
    fillGaps();
    markOverlaps();
    persist();
  });

  document.getElementById("editor-cancel").addEventListener("click", function () {
    dialog.close();
  });

  deleteBtn.addEventListener("click", function () {
    if (editing) editing.remove();
    dialog.close();
    markOverlaps();
    /* No fillGaps() here on purpose: deleting a break must not immediately
       redraw it. Deleting a class can only widen an existing gap, and widening
       one past MAX_GAP_MINUTES is the user saying the day ends there. */
    persist();
  });

  kindEl.addEventListener("change", syncKindFields);

  grid.addEventListener("click", function (event) {
    var add = event.target.closest(".tt__add");
    if (add) {
      openEditor(null, parseInt(add.dataset.weekday, 10));
      return;
    }
    var block = event.target.closest(".tt__block");
    if (block) openEditor(block, parseInt(block.dataset.weekday, 10));
  });

  grid.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var block = event.target.closest(".tt__block");
    if (!block) return;
    event.preventDefault();
    openEditor(block, parseInt(block.dataset.weekday, 10));
  });

  /* The no-JavaScript path still works: serialise on the way out. */
  form.addEventListener("submit", function () {
    fields.innerHTML = "";
    collect().forEach(function (row) {
      Object.keys(row).forEach(function (key) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = key === "subject_id" ? "subject_id" : key;
        input.value = row[key];
        fields.appendChild(input);
      });
    });
  });
})();
