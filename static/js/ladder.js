/* The skip ladder: "what would skipping 1, 2, 3... more of this subject cost?"

   Lived inside plan.js until the same question started being asked from a
   subject's own page. Nothing about the answer is page-specific, so it moved
   here rather than being written a second time — two copies of "is this rung
   safe?" is exactly the kind of drift that makes the two pages quote different
   numbers for the same subject.

   The caller owns the host element and when to ask; this owns the fetch, the
   markup, and the wording. */
(function () {
  "use strict";

  var esc = window.BunkrHtml.esc;

  function note(rung, under, data) {
    if (under) {
      return data.subject.already_broken
        ? "still under " + data.subject.limit + "%"
        : "drops below " + data.subject.limit + "%";
    }
    /* Subject codes are whatever the student renamed them to, and this is an
       innerHTML string. */
    if (rung.breaks.length) return esc(rung.breaks.join(", ")) + " breaks";
    if (!rung.is_safe) return "breaks the overall limit";
    return "still safe";
  }

  function ordinal(n) {
    if (n % 100 >= 11 && n % 100 <= 13) return "th";
    return ["th", "st", "nd", "rd"][n % 10] || "th";
  }

  /* `lead` lets a page that already names the subject in its heading skip
     repeating the code in the sentence above the rungs. */
  function render(host, data, options) {
    var opts = options || {};

    if (!data.ladder.length) {
      host.innerHTML =
        '<p class="text-muted">No lectures of this subject left in the window.</p>';
      return;
    }

    var horizon = data.horizon.is_checkpoint
      ? "before " + (data.horizon.label || "your checkpoint")
      : "before the end of term";

    var broken = data.subject.already_broken;
    var subject = opts.lead === "short" ? "" : esc(data.subject.code) + " ";

    var html = '<p class="text-muted" style="font-size:var(--text-sm)">' +
      "Skipping this many more " + subject + "lectures " + esc(horizon) +
      ":</p><div class=\"ladder\">";

    data.ladder.forEach(function (rung) {
      var under = rung.subject_pct !== null && rung.subject_pct < data.subject.limit;
      var tone = under ? "danger" : (rung.is_safe ? rung.subject_verdict : "danger");
      /* Which lecture the rung actually reaches. "3" is an abstraction; "through
         Mon 7 Sep" is the thing you can hold against a calendar. */
      var through = rung.through_date
        ? '<div class="ladder__through mono">through ' +
          esc(window.BunkrFmt.date(rung.through_date)) + "</div>"
        : "";
      html += '<div class="ladder__rung ladder__rung--' + tone + '">' +
        '<div class="ladder__n num">' + rung.n + '</div>' +
        '<div class="ladder__pct mono">' + rung.subject_pct.toFixed(1) + "%</div>" +
        '<div class="ladder__note">' + note(rung, under, data) + "</div>" +
        through + "</div>";
    });

    html += "</div>";

    if (broken) {
      /* Every rung "breaks nothing new" when the subject is already under its
         line, so saying "still safe" here would be plainly false. */
      html += '<p class="text-danger" style="font-size:var(--text-sm)">' +
        esc(data.subject.code) + " is already below its " + data.subject.limit +
        "% line at " + data.subject.current_pct.toFixed(1) +
        "%. Skipping more digs the hole deeper — none of these are safe.</p>";
    } else {
      var firstUnsafe = data.ladder.filter(function (r) { return !r.is_safe; })[0];
      html += firstUnsafe
        ? '<p class="text-danger" style="font-size:var(--text-sm)">The ' +
            firstUnsafe.n + ordinal(firstUnsafe.n) +
            " is where it stops being safe.</p>"
        : '<p class="text-safe" style="font-size:var(--text-sm)">' +
            "Every step shown here keeps you above your limits.</p>";
    }
    host.innerHTML = html;
  }

  /* Resolves to true when something was rendered, so a caller can leave a
     toggle closed rather than opening onto an error it already reported. */
  function load(host, subjectId, options) {
    host.innerHTML = '<p class="text-muted">Working it out…</p>';
    return window.BunkrApi
      .get("/api/subjects/" + subjectId + "/skip-ladder")
      .then(function (res) {
        if (!res.ok) {
          host.innerHTML = "";
          window.BunkrToast.error(res.body.error || "Couldn't work that out.");
          return false;
        }
        render(host, res.body, options);
        return true;
      });
  }

  window.BunkrLadder = { render: render, load: load };
})();
