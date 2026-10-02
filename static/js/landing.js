/* BUNKK landing page. Vanilla, no libraries.

   One scroll handler, run through requestAnimationFrame, turns scroll position
   into a handful of CSS custom properties and classes. CSS does the drawing.
   With reduced motion requested the scrolling engine never starts and the page
   stays in its plain, fully readable state (the scene 1 buttons still work).

   What gets written, and where:
     <html>     .lp-js          enables the "before" states in landing.css. A tiny
                                inline script in <head> adds it before first paint;
                                it is added here too, and window.__lpReady tells
                                that script this file arrived.
                .lp-sticky      wide screens: scenes pile into one pinned stage
                .lp-stack       narrow screens: rows that play when they scroll in
                --pp            page scroll progress 0..1 (narrow-screen rail)
     .lp-stage  --p             story progress 0..1
                data-scene      1..6, flips halfway through the hand-off
     .lp-scene  --sp            progress inside the scene 0..1
                --wo            frame wipe-out 0..1 over the scene's last 20%
                --co            outgoing copy leaving 0..1  (first half of --wo)
                --ci            incoming copy arriving 0..1 (second half of the
                                previous scene's --wo)
                .is-s1..is-s4   cumulative steps reached at fixed --sp values
                .is-wiping      while 0 < --wo < 1 (shows the squeegee bar)
                .is-gone        once --wo reaches 1 (visibility:hidden)
     .lp-rail__seg  --f         how finished that scene is, 0..1 */
(function () {
  "use strict";

  var root = document.documentElement;

  function clamp(n, lo, hi) { return n < lo ? lo : (n > hi ? hi : n); }
  function $all(sel, from) { return Array.prototype.slice.call((from || document).querySelectorAll(sel)); }
  function restart(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;              // let the animation start over
    el.classList.add(cls);
  }

  var scenes = $all(".lp-scene");

  /* ---- Scene 1: you can press it ------------------------------------------
     Each row carries data-base (how many it could still spare before this
     click). Skipping spends one: the badge shows base-1 and its colour follows
     the number. The verdict card above restamps. The scripted press of the last
     row at step 1 stops the moment you have clicked anything yourself.
     These buttons are real (named, focusable), as are the ones in scene 3. */
  var play = (function () {
    var scene = scenes[0];
    if (!scene) return null;
    var rows = $all(".lp-row", scene);
    var dayBtn = scene.querySelector(".lp-daybtn");
    var card = document.getElementById("lp-s1-card");
    var word = document.getElementById("lp-s1-word");
    var reason = document.getElementById("lp-s1-reason");
    var scripted = rows[rows.length - 1];
    var touched = false;

    function isOn(row) { return row.getAttribute("data-on") === "1"; }

    function setRow(row, on, animate) {
      var left = parseInt(row.getAttribute("data-base"), 10) - (on ? 1 : 0);
      var btn = row.querySelector(".lp-skipbtn");
      var badge = row.querySelector(".lp-left");
      row.setAttribute("data-on", on ? "1" : "0");
      row.classList.toggle("is-planned", on);
      btn.classList.toggle("is-on", on);
      btn.classList.toggle("btn--danger", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      badge.textContent = left + " left";
      badge.className = "badge lp-left badge--" + (left <= 0 ? "danger" : (left === 1 ? "warn" : "safe"));
      if (on && animate) restart(btn, "is-anim");
    }

    function summarise(restamp) {
      var on = rows.filter(isOn);
      var dry = on.filter(function (r) { return parseInt(r.getAttribute("data-base"), 10) - 1 <= 0; });
      var codes = function (list) { return list.map(function (r) { return r.getAttribute("data-code"); }); };
      var text;
      if (!on.length) {
        text = "CS201, EE110 — all within budget.";
      } else {
        text = "Already planning to miss " + codes(on).join(", ") + ".";
        if (dry.length) {
          text += " " + codes(dry).join(", ") + (dry.length === 1 ? " has" : " have") +
            " no room left after this.";
        }
      }
      reason.textContent = text;
      var all = on.length === rows.length;
      dayBtn.classList.toggle("is-on", all);
      dayBtn.classList.toggle("btn--danger", all);
      dayBtn.setAttribute("aria-pressed", all ? "true" : "false");
      dayBtn.textContent = all ? "Skipping all day" : "Skip whole day";
      if (restamp) { restart(word, "is-flap"); restart(card, "is-thud"); }
    }

    scene.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest(".lp-skipbtn, .lp-daybtn") : null;
      if (!btn) return;
      touched = true;
      if (btn.classList.contains("lp-daybtn")) {
        var turnOn = !rows.every(isOn);
        rows.forEach(function (r) { setRow(r, turnOn, turnOn); });
      } else {
        var row = btn.closest(".lp-row");
        setRow(row, !isOn(row), true);
      }
      summarise(true);
    });

    return {
      /* Called by the scroll engine. Does nothing once you have taken over. */
      script: function (step) {
        if (touched) return;
        var on = step >= 1;
        if (isOn(scripted) === on) return;
        setRow(scripted, on, on);
        summarise(on);
      }
    };
  })();

  /* ---- Scene 3: you can press this one too --------------------------------
     The same month grid and day sheet as the plan page, run off a made-up
     timetable. Every verdict is worked out from BASE (what each subject can
     still spare) minus what has been ticked, so planning one day really does
     change the colour of the others. The scripted pick of Thursday stops the
     moment you click anything yourself. */
  var plan = (function () {
    var scene = scenes[2];
    var pane = scene && scene.querySelector(".lp-pane--plan");
    if (!pane) return null;
    var sheet = pane.querySelector(".lp-sheet");
    var title = pane.querySelector(".lp-sheet__title");
    var body = pane.querySelector(".lp-sheet__body");
    var list = pane.querySelector(".lp-committed");
    var cells = $all(".js-day", pane);
    if (!sheet || !title || !body || !list || !cells.length) return null;

    var BASE = { CS201: 3, EE110: 2, MA102: 0, PH105: 1 };
    var WEEK = {
      1: [["09:00", "10:00", "CS201"], ["10:00", "11:00", "EE110"], ["11:00", "12:00", "PH105"]],
      2: [["09:00", "10:00", "MA102"], ["10:00", "11:00", "CS201"]],
      3: [["09:00", "10:00", "CS201"], ["10:00", "11:00", "EE110"]],
      4: [["09:00", "10:00", "EE110"], ["11:00", "12:00", "MA102"], ["12:00", "13:00", "CS201"]],
      5: [["09:00", "10:00", "MA102"], ["10:00", "11:00", "MA102"]]
    };
    var WORDS = { skip: "Skip", partial: "Part skip", go: "Can't skip", planned: "Planned" };
    var SCRIPT_DAY = "15";

    var picked = {};            // day -> [bool per lecture]
    var whole = {};             // day -> true
    var open = null, hint = null, touched = false, stamped = null;

    function cellFor(day) {
      for (var i = 0; i < cells.length; i++) if (cells[i].getAttribute("data-day") === day) return cells[i];
      return null;
    }
    function lecturesOn(day) { return WEEK[cellFor(day).getAttribute("data-wd")] || []; }
    function isMissed(day, i) { return !!whole[day] || !!(picked[day] && picked[day][i]); }
    function missedOn(day) {
      return lecturesOn(day).filter(function (_, i) { return isMissed(day, i); }).length;
    }
    function left(code) {
      var spent = 0;
      cells.forEach(function (cell) {
        var day = cell.getAttribute("data-day");
        lecturesOn(day).forEach(function (lec, i) { if (lec[2] === code && isMissed(day, i)) spent++; });
      });
      return BASE[code] - spent;
    }
    function tone(n) { return n <= 0 ? "danger" : (n === 1 ? "warn" : "safe"); }
    function uniq(list) { return list.filter(function (x, i) { return list.indexOf(x) === i; }); }

    /* Judged on its own, like the real page: "if this is the only thing I skip,
       am I still safe?" A lecture is skippable when its subject can spare every
       class it has that day. */
    function judge(day) {
      var lectures = lecturesOn(day);
      var missed = missedOn(day);
      if (missed) {
        var over = lectures.some(function (lec, i) { return isMissed(day, i) && left(lec[2]) < 0; });
        return { verdict: "planned", over: over,
          reason: over ? "This goes past a limit. Untick one."
            : (missed === lectures.length ? "Missing all " + missed + " classes."
              : "Missing " + missed + " of " + lectures.length + " classes.") };
      }
      var ok = lectures.map(function (lec) {
        var same = lectures.filter(function (other) { return other[2] === lec[2]; }).length;
        return left(lec[2]) >= same;
      });
      var yes = uniq(lectures.filter(function (_, i) { return ok[i]; }).map(function (l) { return l[2]; }));
      var no = uniq(lectures.filter(function (_, i) { return !ok[i]; }).map(function (l) { return l[2]; }));
      if (!no.length) return { verdict: "skip", reason: yes.join(", ") + " — all within budget." };
      if (!yes.length) return { verdict: "go", reason: "No room left in " + no.join(", ") + "." };
      // The half day: whatever can be skipped at the end of the day.
      var tail = [];
      for (var i = lectures.length - 1; i >= 0 && ok[i]; i--) tail.unshift(i);
      var partial = null;
      if (tail.length) {
        var at = lectures[tail[0] - 1][1];
        partial = { tail: tail, label: "Leave after " + at,
          note: "Leave after " + at + " — skips " +
            tail.map(function (i) { return lectures[i][2]; }).join(", ") + ". " + tail.length + "h free." };
      }
      return { verdict: "partial", partial: partial,
        reason: "Skip " + yes.join(", ") + "; no room left in " + no.join(", ") + "." };
    }

    function paintGrid() {
      cells.forEach(function (cell) {
        var day = cell.getAttribute("data-day");
        var j = judge(day);
        cell.className = "cal__day js-day verdict--" + j.verdict +
          (j.over ? " is-over" : "") +
          (cell.getAttribute("data-today") === "1" ? " is-today" : "") +
          (day === open || day === hint ? " is-pick" : "");
        cell.title = j.reason;
        var n = missedOn(day);
        var badge = cell.querySelector(".cal__count");
        if (!n) { if (badge) badge.remove(); return; }
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "cal__count mono";
          cell.appendChild(badge);
        }
        badge.textContent = n;
        if (stamped === day) restart(badge, "is-new");
      });
      stamped = null;
    }

    function paintList() {
      var html = "";
      cells.forEach(function (cell) {
        var day = cell.getAttribute("data-day");
        var when = cell.getAttribute("data-label").replace(/^(\w{3})\w*/, "$1");
        var row = function (what, i) {
          html += '<div class="lecture"><span class="lecture__when">' + when + " — " + what +
            '</span><span></span><button class="btn btn--sm btn--danger lp-drop" type="button" data-day="' +
            day + '"' + (i === null ? "" : ' data-i="' + i + '"') +
            ' aria-label="Remove ' + when + " " + what + '">Remove</button></div>';
        };
        if (whole[day]) { row("whole day", null); return; }
        lecturesOn(day).forEach(function (lec, i) { if (isMissed(day, i)) row(lec[2], i); });
      });
      list.innerHTML = html ||
        '<p class="text-muted">Nothing committed yet. Your whole budget is discretionary.</p>';
    }

    function paintSheet() {
      sheet.classList.toggle("is-open", open !== null);
      if (open === null) return;
      var day = open;
      var j = judge(day);
      title.textContent = cellFor(day).getAttribute("data-label");
      var html = '<div class="sheet-verdict verdict--' + j.verdict + (j.over ? " is-over" : "") +
        '"><b>' + WORDS[j.verdict] + '</b> <span class="text-muted">' + j.reason + "</span></div>" +
        '<label class="check" style="margin-bottom:var(--sp-3)"><input type="checkbox" class="lp-whole"' +
        (whole[day] ? " checked" : "") + "><span>Miss the whole day</span></label>";
      if (j.partial) {
        html += '<div class="sheet-partial"><div class="sheet-partial__note">' + j.partial.note +
          '</div><button class="btn btn--sm btn--primary lp-partial" type="button">' +
          j.partial.label + "</button></div>";
      }
      html += '<div class="stack lp-sheet__rows">';
      lecturesOn(day).forEach(function (lec, i) {
        var n = left(lec[2]);
        html += '<label class="lecture' + (isMissed(day, i) ? " is-planned" : "") + '">' +
          '<span><span class="mono">' + lec[0] + "–" + lec[1] +
          '</span> <strong style="margin-left:var(--sp-3)">' + lec[2] + "</strong>" +
          ' <span class="badge badge--' + tone(n) + '">' + n + " left</span></span>" +
          '<span class="check"><input type="checkbox" class="lp-lec" data-i="' + i + '"' +
          (isMissed(day, i) ? " checked" : "") + (whole[day] ? " disabled" : "") + "></span></label>";
      });
      body.innerHTML = html + "</div>";
    }

    function paint() { paintGrid(); paintList(); paintSheet(); }
    function take() { touched = true; hint = null; }

    pane.addEventListener("click", function (e) {
      var el = e.target.closest ? e.target.closest(".js-day, .lp-drop, .lp-partial, .lp-sheet__done") : null;
      if (!el) return;
      take();
      var day = el.getAttribute("data-day");
      if (el.classList.contains("js-day")) {
        open = open === day ? null : day;
      } else if (el.classList.contains("lp-sheet__done")) {
        open = null;
      } else if (el.classList.contains("lp-partial")) {
        var half = judge(open).partial;
        picked[open] = picked[open] || [];
        half.tail.forEach(function (i) { picked[open][i] = true; });
        stamped = open;
      } else if (el.hasAttribute("data-i")) {
        picked[day][parseInt(el.getAttribute("data-i"), 10)] = false;
      } else {
        whole[day] = false;
      }
      paint();
    });

    pane.addEventListener("change", function (e) {
      var box = e.target;
      if (open === null || !box.classList) return;
      if (box.classList.contains("lp-whole")) {
        whole[open] = box.checked;
      } else if (box.classList.contains("lp-lec")) {
        picked[open] = picked[open] || [];
        picked[open][parseInt(box.getAttribute("data-i"), 10)] = box.checked;
      } else {
        return;
      }
      take();
      if (box.checked) stamped = open;
      paint();
    });

    cells.forEach(function (cell) {
      if (cell.classList.contains("is-today")) cell.setAttribute("data-today", "1");
    });
    pane.classList.add("lp-plan-live");
    paint();

    return {
      /* Called by the scroll engine: point at Thursday, open it, take the half
         day. Does nothing once you have taken over. */
      script: function (step) {
        if (touched) return;
        picked = {}; whole = {};
        hint = step >= 2 ? SCRIPT_DAY : null;
        open = step >= 3 ? SCRIPT_DAY : null;
        if (step >= 4) {
          var half = judge(SCRIPT_DAY).partial;
          picked[SCRIPT_DAY] = [];
          half.tail.forEach(function (i) { picked[SCRIPT_DAY][i] = true; });
          stamped = SCRIPT_DAY;
        }
        paint();
      }
    };
  })();

  /* ---- Scene 6: checkpoints, and you can add your own ------------------------
     The checkpoints page's form and list. The number above them is the same
     arithmetic the app does: what you can miss and still be at 75% on the date
     you are planning to, which is the next checkpoint if there is one and the
     semester end if not. The scripted mid-sem stops once you touch anything. */
  var checkpoints = (function () {
    var scene = scenes[5];
    var pane = scene && scene.querySelector(".lp-pane--cp");
    if (!pane) return null;
    var form = pane.querySelector(".lp-cp-form");
    var dateIn = document.getElementById("lp-cp-date");
    var labelIn = document.getElementById("lp-cp-label");
    var error = pane.querySelector(".lp-cp-error");
    var list = pane.querySelector(".lp-cp-list");
    var num = pane.querySelector(".lp-cp-n");
    var to = pane.querySelector(".lp-cp-to");
    if (!form || !dateIn || !labelIn || !error || !list || !num || !to) return null;

    var TODAY = "2026-10-14", END = "2027-01-20";
    var PRESENT = 38, TOTAL = 50, PER_DAY = 2, LIMIT = 0.75;
    var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var SCRIPT = [{ date: "2026-11-12", label: "Mid-sem audit" }, { date: "2026-12-04", label: "Lab viva" }];

    var items = [];             // [{date: "YYYY-MM-DD", label}], kept in date order
    var touched = false, added = null, shown = null;

    function esc(text) {
      return String(text).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }
    function parts(iso) { return iso.split("-").map(Number); }
    function short(iso) { var d = parts(iso); return (d[2] < 10 ? "0" : "") + d[2] + " " + MONTHS[d[1] - 1]; }
    function full(iso) { return short(iso) + " " + parts(iso)[0]; }

    /* Lectures still to come by `iso`: weekdays after today, PER_DAY each. */
    function remaining(iso) {
      var a = parts(TODAY), b = parts(iso), days = 0;
      var day = new Date(Date.UTC(a[0], a[1] - 1, a[2] + 1));
      var end = Date.UTC(b[0], b[1] - 1, b[2]);
      for (; day.getTime() <= end; day.setUTCDate(day.getUTCDate() + 1)) {
        var wd = day.getUTCDay();
        if (wd !== 0 && wd !== 6) days++;
      }
      return days * PER_DAY;
    }
    function budget(iso) {
      var left = remaining(iso);
      return Math.max(0, Math.floor(PRESENT + left - LIMIT * (TOTAL + left) + 1e-9));
    }

    function paint() {
      var next = items[0] || null;
      var n = budget(next ? next.date : END);
      num.textContent = n;
      to.textContent = next
        ? "to " + (next.label || "your checkpoint") + ", " + short(next.date)
        : "to " + short(END) + ", semester end";
      if (shown !== null && shown !== n) restart(num, "is-new");
      shown = n;

      var html = "";
      items.forEach(function (item, i) {
        html += '<div class="lecture' + (item.date === added ? " is-new" : "") + '"><div>' +
          '<span class="mono">' + full(item.date) + "</span>" +
          (item.label ? '<span style="margin-left:var(--sp-2)">' + esc(item.label) + "</span>" : "") +
          '</div><div class="row" style="gap:var(--sp-2)">' +
          (i === 0 ? '<span class="badge badge--warn">Planning to this one</span>'
                   : '<span class="badge badge--neutral">Upcoming</span>') +
          '<button class="btn btn--sm btn--danger lp-cp-drop" type="button" data-date="' + item.date +
          '" aria-label="Remove checkpoint on ' + full(item.date) + '">Remove</button></div></div>';
      });
      html += '<div class="lecture"><div><span class="mono">' + full(END) + "</span>" +
        '<span style="margin-left:var(--sp-2)">Semester end</span></div>' +
        '<div class="row" style="gap:var(--sp-2)"><span class="badge badge--' +
        (next ? 'neutral">Always last' : 'warn">Planning to this one') + "</span></div></div>";
      list.innerHTML = html;
      added = null;
    }

    function fail(message) {
      error.textContent = message;
      error.hidden = !message;
      dateIn.classList.toggle("is-error", !!message);
    }

    /* The same refusals, in the same words, as the real form. */
    function add(date, label) {
      if (!date) return "Pick a date first.";
      if (date <= TODAY) return "A checkpoint has to be in the future.";
      if (date > END) return "That's after your semester ends.";
      if (items.some(function (item) { return item.date === date; })) {
        return "You already have a checkpoint on that date.";
      }
      items.push({ date: date, label: label });
      items.sort(function (a, b) { return a.date < b.date ? -1 : 1; });
      added = date;
      return "";
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      touched = true;
      var message = add(dateIn.value, labelIn.value.trim());
      fail(message);
      if (!message) { dateIn.value = ""; labelIn.value = ""; }
      paint();
    });
    form.addEventListener("input", function () { touched = true; fail(""); });
    list.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest(".lp-cp-drop") : null;
      if (!btn) return;
      touched = true;
      var date = btn.getAttribute("data-date");
      items = items.filter(function (item) { return item.date !== date; });
      paint();
    });

    paint();

    return {
      /* Called by the scroll engine: type a mid-sem, add it, then a second one. */
      script: function (step) {
        if (touched) return;
        items = [];
        if (step >= 3) add(SCRIPT[0].date, SCRIPT[0].label);
        if (step >= 4) add(SCRIPT[1].date, SCRIPT[1].label);
        if (step < 3) added = null;
        dateIn.value = step === 2 ? SCRIPT[0].date : "";
        labelIn.value = step === 2 ? SCRIPT[0].label : "";
        fail("");
        paint();
      }
    };
  })();

  /* ---- Reduced motion: nothing below runs ------------------------------------ */
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (reduce.matches) return;

  root.classList.add("lp-js");

  // Steps land through the scene and keep landing until the hand-off begins.
  var STEPS = [0.08, 0.27, 0.46, 0.65];
  var WIPE_AT = 0.80;                // --sp where the hand-off window opens
  var COUNT_BY = 0.60;               // --sp by which the scrubbed meter and counts land

  var story = document.querySelector(".lp-story");
  var stage = document.querySelector(".lp-stage");
  var segs = $all(".lp-rail__seg");
  var wide = window.matchMedia("(min-width: 900px)");
  var sticky = false;
  var ticking = false;
  var reveals;
  var cache = scenes.map(function () { return { sp: -1, wo: -1, co: -1, ci: -1, step: -1, f: -1 }; });
  var lastPP = -1, lastP = -1, lastScene = 0, lastGone = [];

  function setStep(scene, step) {
    for (var k = 1; k <= STEPS.length; k++) scene.classList.toggle("is-s" + k, step >= k);
    if (scene === scenes[0] && play) play.script(step);
    if (scene === scenes[2] && plan) plan.script(step);
    if (scene === scenes[5] && checkpoints) checkpoints.script(step);
  }

  function setCounts(scene, k) {
    $all("[data-count]", scene).forEach(function (el) {
      var next = String(Math.round(parseFloat(el.getAttribute("data-count")) * k));
      if (el.textContent !== next) el.textContent = next;
    });
  }

  function prop(el, name, value, digits) { el.style.setProperty(name, value.toFixed(digits || 4)); }

  function update() {
    ticking = false;
    var vh = window.innerHeight;
    var room = root.scrollHeight - vh;
    var pp = room > 0 ? clamp(window.pageYOffset / room, 0, 1) : 0;
    if (Math.abs(pp - lastPP) > 0.0005) {
      lastPP = pp;
      root.style.setProperty("--pp", pp.toFixed(4));
    }
    checkReveals();
    if (!sticky) checkStack();
    if (!sticky || !story) return;

    var box = story.getBoundingClientRect();
    var span = box.height - vh;
    var p = span > 0 ? clamp(-box.top / span, 0, 1) : 0;
    if (Math.abs(p - lastP) > 0.0005) {
      lastP = p;
      prop(stage, "--p", p);
    }

    var n = scenes.length;
    var s = p * n;
    // The counter and the block behind the frame change at the midpoint of the
    // hand-off, when no copy is on screen.
    var mid = (1 - WIPE_AT) / 2;
    var current = Math.min(n, Math.floor(s + mid) + 1);
    if (current !== lastScene) {
      lastScene = current;
      stage.setAttribute("data-scene", String(current));
      segs.forEach(function (seg, i) {
        if (i + 1 === current) seg.setAttribute("aria-current", "true");
        else seg.removeAttribute("aria-current");
      });
    }

    var prevWo = 1;
    scenes.forEach(function (scene, i) {
      var sp = clamp(s - i, 0, 1);
      var wo = i < n - 1 ? clamp((sp - WIPE_AT) / (1 - WIPE_AT), 0, 1) : 0;
      var co = clamp(wo * 2, 0, 1);
      var ci = i === 0 ? 1 : clamp((prevWo - 0.5) * 2, 0, 1);
      prevWo = wo;
      var step = 0;
      for (var k = 0; k < STEPS.length; k++) if (sp >= STEPS[k]) step = k + 1;
      var c = cache[i];

      if (Math.abs(sp - c.sp) > 0.0005) {
        c.sp = sp;
        prop(scene, "--sp", sp);
        if (i === 1) setCounts(scene, clamp(sp / COUNT_BY, 0, 1));
        if (segs[i]) prop(segs[i], "--f", sp, 3);
      }
      if (Math.abs(wo - c.wo) > 0.001) {
        c.wo = wo;
        prop(scene, "--wo", wo);
        scene.classList.toggle("is-wiping", wo > 0 && wo < 1);
        scene.classList.toggle("is-gone", wo >= 1);
      }
      if (Math.abs(co - c.co) > 0.001) { c.co = co; prop(scene, "--co", co); }
      if (Math.abs(ci - c.ci) > 0.001) { c.ci = ci; prop(scene, "--ci", ci); }
      if (step !== c.step) {
        c.step = step;
        setStep(scene, step);
      }
    });
  }

  function request() {
    if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
  }

  /* Rail: jump to the start of a scene inside the pinned track. */
  segs.forEach(function (seg, i) {
    seg.addEventListener("click", function (e) {
      if (!sticky || !story) return;
      e.preventDefault();
      var top = story.getBoundingClientRect().top + window.pageYOffset;
      var span = story.offsetHeight - window.innerHeight;
      window.scrollTo({ top: top + span * (i + 0.02) / scenes.length, behavior: "smooth" });
    });
  });

  /* Narrow screens: each scene plays once, step by step, when its frame
     comes into view. */
  var timers = [];
  function countUp(scene) {
    var started = null;
    function frame(now) {
      if (started === null) started = now;
      var k = clamp((now - started) / 900, 0, 1);
      setCounts(scene, k);
      if (k < 1) window.requestAnimationFrame(frame);
    }
    window.requestAnimationFrame(frame);
  }
  function playScene(scene) {
    if (scene._lpPlayed) return;
    scene._lpPlayed = true;
    STEPS.forEach(function (_, i) {
      timers.push(window.setTimeout(function () {
        setStep(scene, i + 1);
        if (i === 0 && scene.getAttribute("data-scene") === "2") countUp(scene);
      }, 250 + i * 650));
    });
  }
  /* Checked from the scroll handler, like the reveals: a scene that never
     gets its cue would keep its "before" state for good. */
  function checkStack() {
    var line = window.innerHeight * 0.7;
    scenes.forEach(function (scene) {
      if (scene._lpPlayed) return;
      var frame = scene.querySelector(".lp-frame");
      if (!frame) return;
      var box = frame.getBoundingClientRect();
      if (box.top < line && box.bottom > 0) playScene(scene);
    });
  }

  function setMode() {
    sticky = wide.matches && !!story && !!stage;
    root.classList.toggle("lp-sticky", sticky);
    root.classList.toggle("lp-stack", !sticky);
    timers.forEach(window.clearTimeout); timers = [];
    scenes.forEach(function (scene, i) {
      cache[i] = { sp: -1, wo: -1, co: -1, ci: -1, step: -1, f: -1 };
      scene._lpPlayed = false;
      ["--sp", "--wo", "--co", "--ci"].forEach(function (name) { scene.style.removeProperty(name); });
      scene.classList.remove("is-wiping", "is-gone");
      setStep(scene, 0);
    });
    if (sticky) { lastScene = 0; update(); }
    else { if (stage) stage.setAttribute("data-scene", "1"); checkStack(); }
  }

  /* ---- One-shot reveals ------------------------------------------------------
     Checked in the scroll handler rather than with an IntersectionObserver: the
     hidden state is a clip-path, and a fully clipped box can count as "not
     intersecting", which would keep it hidden forever. */
  reveals = $all("[data-lp-reveal]");
  function checkReveals() {
    if (!reveals || !reveals.length) return;
    var line = window.innerHeight * 0.9;
    reveals = reveals.filter(function (el) {
      if (el.getBoundingClientRect().top > line) return true;
      el.classList.add("is-in");
      // Lets go of the clip once it has opened, so hover shadows can't be cut.
      window.setTimeout(function () { el.classList.add("is-done"); }, 560);
      return false;
    });
  }

  if (wide.addEventListener) wide.addEventListener("change", setMode);
  else if (wide.addListener) wide.addListener(setMode);
  window.addEventListener("scroll", request, { passive: true });
  window.addEventListener("resize", request, { passive: true });
  setMode();
  request();

  /* ---- Pointer: which way is the light? ----------------------------------- */
  function pointerVars(target, host, e) {
    var r = host.getBoundingClientRect();
    var x = clamp(((e.clientX - r.left) / r.width - 0.5) * 2, -1, 1);
    var y = clamp(((e.clientY - r.top) / r.height - 0.5) * 2, -1, 1);
    target.style.setProperty("--tx", x.toFixed(3));
    target.style.setProperty("--ty", y.toFixed(3));
  }
  function track(host, target) {
    var pending = null;
    host.addEventListener("pointermove", function (e) {
      if (e.pointerType === "touch") return;
      pending = e;
      window.requestAnimationFrame(function () {
        if (pending) { pointerVars(target, host, pending); pending = null; }
      });
    }, { passive: true });
    host.addEventListener("pointerleave", function () {
      pending = null;
      target.style.setProperty("--tx", "0");
      target.style.setProperty("--ty", "0");
    });
  }

  var hero = document.querySelector(".lp-hero");
  var heroStage = document.querySelector("[data-lp-hero]");
  if (hero && heroStage) track(hero, heroStage);
  $all("[data-lp-tilt]").forEach(function (card) { track(card, card); });

  /* ---- Hero verdict: it keeps changing its mind ---------------------------- */
  var hv = document.getElementById("lp-hv");
  var word = document.getElementById("lp-hv-word");
  var reason = document.getElementById("lp-hv-reason");
  var VERDICTS = [
    { cls: "skip",    word: "SKIP",            reason: "CS201, EE110 — all within budget." },
    { cls: "partial", word: "PART SKIP",       reason: "Leave after 12:00 — skips CS201. 1h free." },
    { cls: "go",      word: "CAN’T SKIP", reason: "No room left in MA102." }
  ];
  if (hv && word && reason) {
    var at = 0, cycle = null, onScreen = true;
    var show = function (i) {
      var v = VERDICTS[i];
      hv.className = hv.className.replace(/hero-verdict--(skip|partial|go)/, "hero-verdict--" + v.cls);
      word.textContent = v.word;
      reason.textContent = v.reason;
      restart(word, "is-flap");
      restart(hv, "is-thud");
    };
    var tick = function () { at = (at + 1) % VERDICTS.length; show(at); };
    var sync = function () {
      var run = onScreen && !document.hidden;
      if (run && !cycle) cycle = window.setInterval(tick, 2800);
      if (!run && cycle) { window.clearInterval(cycle); cycle = null; }
    };
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        onScreen = entries[0].isIntersecting; sync();
      }, { threshold: 0.2 }).observe(hv);
    }
    document.addEventListener("visibilitychange", sync);
    sync();
  }

  checkReveals();
  window.__lpReady = true;      // tells the <head> snippet we loaded and ran
})();
