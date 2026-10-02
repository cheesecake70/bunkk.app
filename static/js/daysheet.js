/* The day sheet — one day's classes, and which of them you're missing.

   Lifted out of calendar.js so the plan strip can open the same thing. Deciding
   "I'll skip the two labs but go to the lecture" is the same decision wherever
   you happen to be standing, and it was only ever expressible on the calendar;
   the strip could commit whole days and nothing finer.

   The host page supplies what differs: how to find the cell for a date, what to
   do after something changes, and whether marking a holiday is in scope here.
*/
(function () {
  "use strict";

  function mount(options) {
    var sheet = document.getElementById("day-sheet");
    if (!sheet) return null;

    var title = document.getElementById("sheet-title");
    var body = document.getElementById("sheet-body");
    var holidayBtn = document.getElementById("sheet-holiday");
    var closeBtn = document.getElementById("sheet-close");

    var cellSelector = options.cellSelector;
    var countClass = options.countClass || "cal__count";
    var openDate = null;
    var dirty = false;

    var esc = window.BunkrHtml.esc;
    var attr = window.BunkrHtml.attr;

    function cellFor(date) {
      return document.querySelector(cellSelector + '[data-date="' + date + '"]');
    }

    /* A day is rarely all-or-nothing once per-lecture plans exist, so the cell
       carries a count rather than a binary marker.

       Derived from the sheet rather than incremented, because "miss the whole
       day" is worth every lecture on it — a +1 there would show 1 against a
       seven-lecture day until the next reload corrected it. */
    function sheetCount() {
      var lectures = body.querySelectorAll(".js-lecture");
      var whole = document.getElementById("sheet-whole-day");
      if (whole && whole.checked) return lectures.length;
      return body.querySelectorAll(".js-lecture:checked").length;
    }

    function setCount(cell, next) {
      if (!cell) return;
      var badge = cell.querySelector("." + countClass.split(" ")[0]);
      if (next <= 0) {
        if (badge) badge.remove();
        return;
      }
      if (!badge) {
        badge = document.createElement("span");
        badge.className = countClass;
        cell.appendChild(badge);
      }
      badge.textContent = next;
    }

    function boxFor(item) {
      return body.querySelector(
        '.js-lecture[data-subject="' + item.subject_id +
        '"][data-start="' + item.start + '"]'
      );
    }

    /* One request, not one per lecture. "Leave after 12:00" is a single
       decision; firing its four absences in parallel raced them against each
       other and recomputed the wallet four times over. */
    function takePartial(button, skippable) {
      var pending = skippable.filter(function (item) {
        var box = boxFor(item);
        return box && !box.checked;
      });
      if (!pending.length) return;

      button.disabled = true;
      window.BunkrCommit.guard(pending.map(function (item) {
        return { date: openDate, subject_id: item.subject_id, start: item.start };
      })).then(function (go) {
        if (!go) { button.disabled = false; return; }
        return window.BunkrApi
        .post("/api/absences/batch", { date: openDate, lectures: pending })
        .then(function (res) {
          button.disabled = false;
          if (!res.ok) {
            window.BunkrToast.error(res.body.error || "Couldn't save that.");
            return;
          }

          pending.forEach(function (item) {
            var box = boxFor(item);
            if (!box) return;
            box.checked = true;
            box.dataset.absence =
              res.body.absence_ids[item.subject_id + "|" + item.start] || "";
          });

          dirty = true;
          var cell = cellFor(openDate);
          setCount(cell, sheetCount());
          paintVerdict(res.body.day);
          if (options.onChange) options.onChange(res.body, openDate, cell);

          /* One at a time: the same reason the commit is one request — four
             deletes in flight together race the same wallet. */
          var undo = function () {
            pending.reduce(function (chain, item) {
              return chain.then(function () {
                var box = boxFor(item);
                if (!box || !box.checked || !box.dataset.absence) return;
                return window.BunkrApi
                  .del("/api/absences/" + box.dataset.absence)
                  .then(function (res) {
                    if (!res.ok) return;
                    box.checked = false;
                    box.dataset.absence = "";
                  });
              });
            }, Promise.resolve()).then(function () {
              /* Only the count is patched here. `dirty` is already set, so
                 closing the sheet reloads and every other figure catches up at
                 once rather than being patched from a payload the undo never
                 asked the server for. */
              setCount(cellFor(openDate), sheetCount());
            });
          };

          if (!window.BunkrCommit.report(res.body, { onUndo: undo })) {
            window.BunkrToast.show(
              "Planned to miss " + pending.length + " on " +
              window.BunkrFmt.date(openDate),
              { onUndo: undo }
            );
          }
        });
      });
    }

    /* The day's own verdict, above its classes. The sheet is where the decision
       is actually taken, and it was the one surface that never said what the
       decision would cost. */
    function paintVerdict(day) {
      var host = document.getElementById("sheet-verdict");
      if (!host || !day) return;
      var words = window.BUNKR_VERDICTS || {};
      host.className = "sheet-verdict verdict--" + day.verdict +
        (day.over_budget ? " is-over" : "");
      host.innerHTML =
        '<b>' + esc(words[day.verdict] || day.verdict) + "</b> " +
        '<span class="text-muted">' + esc(day.reason || "") + "</span>";
    }

    function render(data) {
      title.textContent = data.label || window.BunkrFmt.longDate(data.date);

      if (data.holiday) {
        body.innerHTML =
          '<p class="text-muted">Marked as a holiday' +
          (data.holiday.name ? " (" + esc(data.holiday.name) + ")" : "") +
          " — no classes count on this day.</p>";
        return;
      }
      if (!data.lectures.length) {
        body.innerHTML = '<p class="text-muted">Nothing scheduled.</p>';
        return;
      }

      var html = '<div id="sheet-verdict" class="sheet-verdict"></div>';
      if (!data.in_horizon) {
        html += '<div class="banner banner--info" style="margin-bottom:var(--sp-4)">' +
          '<div class="banner__body"><div class="banner__text">' +
          "This is past your next checkpoint. You can still plan it — it just " +
          "won't come out of the budget you're looking at until that checkpoint passes." +
          "</div></div></div>";
      }

      var wholeDayId = data.whole_day_absence_id || "";
      html += '<label class="check" style="margin-bottom:var(--sp-3)">' +
        '<input type="checkbox" id="sheet-whole-day"' +
        ' data-absence="' + attr(wholeDayId) + '"' +
        (wholeDayId ? " checked" : "") + ">" +
        "<span>Miss the whole day</span></label>";

      /* One quick action for the half-day the maths recommends. Ticking five
         boxes by hand is the same commitment, but only one of the two is an
         answer to "when can I leave?". Pointless while the whole day is off. */
      if (!wholeDayId && data.partial && data.partial.skippable.length) {
        var when = data.partial.leave_after
          ? "Leave after " + data.partial.leave_after.slice(0, 5)
          : (data.partial.arrive_at
              ? "Arrive by " + data.partial.arrive_at.slice(0, 5)
              : null);
        if (when) {
          html += '<div class="sheet-partial">' +
            '<div class="sheet-partial__note">' + esc(data.partial.reason) +
            "</div>" +
            '<button class="btn btn--sm btn--primary" type="button" id="sheet-partial">' +
            esc(when) + "</button></div>";
        }
      }

      /* Lectures and breaks in one clock-ordered list, so the sheet reads like
         the day rather than like a list of things with checkboxes. */
      var rows = data.lectures.map(function (lecture) {
        return { at: lecture.start, kind: "lecture", lecture: lecture };
      }).concat((data.breaks || []).map(function (span) {
        return { at: span.start, kind: "break", span: span };
      })).sort(function (a, b) { return a.at < b.at ? -1 : (a.at > b.at ? 1 : 0); });

      html += '<div class="stack">';
      rows.forEach(function (row) {
        if (row.kind === "break") {
          html += '<div class="lecture lecture--break">' +
            '<span><span class="mono">' + esc(row.span.start.slice(0, 5)) + "–" +
            esc(row.span.end.slice(0, 5)) + '</span>' +
            '<span style="margin-left:var(--sp-3)">' + esc(row.span.label) +
            "</span></span></div>";
          return;
        }
        /* A whole-day plan owns every lecture on the day, exactly as it does on
           Today: the boxes show as ticked and are frozen, because the day is
           what you'd undo, not the class. Each keeps its own row id in
           data-own-absence so lifting the day restores what was underneath. */
        var lecture = row.lecture;
        var own = lecture.absence_id || "";
        var covered = !!wholeDayId;
        /* The same "N left" the Today page shows against each class. This is
           where the decision is made, so this is where the number belongs. */
        var badge = lecture.budget === null || lecture.budget === undefined
          ? ""
          : ' <span class="badge badge--' + attr(lecture.verdict || "neutral") +
            '">' + esc(lecture.budget) + " left</span>";
        var clash = lecture.same_slot
          ? ' <span class="badge badge--neutral">same slot</span>' : "";
        html += '<label class="lecture' + (covered || own ? " is-planned" : "") +
          (lecture.same_slot ? " lecture--clash" : "") + '">' +
          '<span><span class="mono">' + esc(lecture.start.slice(0, 5)) + "–" +
          esc(lecture.end.slice(0, 5)) + '</span> <strong style="margin-left:var(--sp-3)">' +
          esc(lecture.code) + "</strong>" + badge + clash + "</span>" +
          '<span class="check"><input type="checkbox" class="js-lecture"' +
          ' data-subject="' + attr(lecture.subject_id) + '"' +
          ' data-start="' + attr(lecture.start) + '"' +
          ' data-absence="' + attr(covered ? wholeDayId : own) + '"' +
          ' data-own-absence="' + attr(own) + '"' +
          (covered || own ? " checked" : "") +
          (covered ? ' disabled title="The whole day is planned off"' : "") +
          "></span></label>";
      });
      html += "</div>";

      body.innerHTML = html;
      paintVerdict(data.plan);

      var partialBtn = document.getElementById("sheet-partial");
      if (partialBtn) {
        partialBtn.addEventListener("click", function () {
          takePartial(partialBtn, data.partial.skippable);
        });
      }
    }

    function open(date, label) {
      openDate = date;
      title.textContent = label || date;
      body.innerHTML = '<p class="text-muted">Loading…</p>';
      sheet.showModal();
      window.BunkrApi.get("/api/day/" + date).then(function (res) {
        if (!res.ok) {
          body.innerHTML = '<p class="text-danger">' +
            esc(res.body.error || "Couldn't load that day.") + "</p>";
          return;
        }
        res.body.label = label;
        render(res.body);
      });
    }

    /* Ticking "miss the whole day" takes the lectures with it, and unticking
       gives each one back its own plan. Mirrors setDayState in today.js — the
       same decision, so it has to look the same on both pages. */
    function applyWholeDay(absenceId) {
      [].forEach.call(body.querySelectorAll(".js-lecture"), function (box) {
        var own = box.dataset.ownAbsence || "";
        box.disabled = !!absenceId;
        if (absenceId) {
          box.title = "The whole day is planned off";
          box.checked = true;
          box.dataset.absence = absenceId;
        } else {
          box.removeAttribute("title");
          box.checked = !!own;
          box.dataset.absence = own;
        }
        var row = box.closest(".lecture");
        if (row) row.classList.toggle("is-planned", box.checked);
      });
    }

    body.addEventListener("change", function (event) {
      var box = event.target;
      var whole = box.id === "sheet-whole-day";
      if (!whole && !box.classList.contains("js-lecture")) return;

      var cell = cellFor(openDate);
      box.disabled = true;

      var payload = { date: openDate };
      if (!whole) {
        payload.subject_id = parseInt(box.dataset.subject, 10);
        payload.start = box.dataset.start;
      }

      // Unticking gives budget back, so only a new commitment is checked.
      var ready = box.checked
        ? window.BunkrCommit.guard([payload])
        : Promise.resolve(true);

      ready.then(function (go) {
        if (!go) {
          box.checked = false;                 // they said no; show that
          box.disabled = false;
          return;
        }

        var request = box.checked
          ? window.BunkrApi.post("/api/absences", payload)
          : window.BunkrApi.del("/api/absences/" + box.dataset.absence);

        return request.then(function (res) {
          box.disabled = false;
          if (!res.ok) {
            box.checked = !box.checked;        // the server said no; show that
            window.BunkrToast.error(res.body.error || "Couldn't save that.");
            return;
          }

          dirty = true;
          box.dataset.absence = box.checked ? res.body.absence_id : "";
          if (whole) {
            applyWholeDay(box.checked ? res.body.absence_id : "");
          } else {
            box.dataset.ownAbsence = box.dataset.absence;
            var row = box.closest(".lecture");
            if (row) row.classList.toggle("is-planned", box.checked);
          }
          setCount(cell, sheetCount());
          paintVerdict(res.body.day);
          if (options.onChange) options.onChange(res.body, openDate, cell);

          var undo = function () {
            box.checked = !box.checked;
            box.dispatchEvent(new Event("change", { bubbles: true }));
          };
          var when = window.BunkrFmt.date(openDate);
          if (!window.BunkrCommit.report(res.body, { onUndo: undo })) {
            window.BunkrToast.show(
              box.checked
                ? (whole ? "Planned to miss all of " + when
                         : "Planned to miss one class on " + when)
                : "Plan updated for " + when,
              { onUndo: undo }
            );
          }
        });
      });
    });

    if (holidayBtn && options.onHoliday) {
      holidayBtn.addEventListener("click", function () {
        var cell = cellFor(openDate);
        sheet.close();
        options.onHoliday(cell, openDate);
      });
    }

    closeBtn.addEventListener("click", function () {
      sheet.close();
      /* Only when something actually changed: every budget on the page was
         computed before these commitments, so leaving them on screen would be
         quoting stale numbers. An unchanged sheet has nothing to catch up on. */
      if (dirty) {
        dirty = false;
        if (options.onDone) options.onDone();
        else window.location.reload();
      }
    });

    return { open: open };
  }

  window.BunkrDaySheet = { mount: mount };
})();
