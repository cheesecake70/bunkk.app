/* Upload flow: PDF -> parse/merge -> diff, with alias questions in between.
   Plain fetch + DOM, no framework (ADR-1). */
(function () {
  "use strict";

  var dropzone = document.getElementById("dropzone");
  var input = document.getElementById("file");
  var hint = document.getElementById("hint");
  var result = document.getElementById("result");

  var STATUS_TONE = { P: "safe", AG: "safe", A: "danger", L: "pending", NU: "pending" };
  var STATUS_WORD = { P: "Present", A: "Absent", AG: "Granted", L: "Late", NU: "Pending" };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function banner(kind, title, text) {
    var wrap = el("div", "banner" + (kind ? " banner--" + kind : ""));
    var body = el("div", "banner__body");
    body.appendChild(el("div", "banner__title", title));
    if (text) body.appendChild(el("div", "banner__text", text));
    wrap.appendChild(body);
    return wrap;
  }

  function fmtDate(iso) {
    var parts = String(iso).split("-");
    return parts.length === 3 ? parts[2] + "." + parts[1] : iso;
  }

  function fmtTime(iso) {
    return String(iso).slice(0, 5);
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  /* ---- rendering ---------------------------------------------------------- */

  function renderDiff(data) {
    result.innerHTML = "";

    var headline;
    if (data.added && data.updated) {
      headline = plural(data.added, "new lecture") + " · " + plural(data.updated, "update");
    } else if (data.added) {
      headline = plural(data.added, "new lecture") + " added";
    } else if (data.updated) {
      headline = plural(data.updated, "lecture") + " updated";
    } else {
      headline = "Nothing new — your ledger was already up to date";
    }
    result.appendChild(banner("success", "Report merged", headline));

    if (data.new_subjects && data.new_subjects.length) {
      result.appendChild(
        banner("info", plural(data.new_subjects.length, "new subject"),
               data.new_subjects.join(" · "))
      );
    }

    if (data.vanished && data.vanished.length) {
      result.appendChild(
        banner("", plural(data.vanished.length, "lecture") + " disappeared from the portal",
               "Kept in your history, left out of your totals.")
      );
    }

    var changes = data.changes || [];
    var moves = Object.keys(data.pct_moves || {});
    if (changes.length || moves.length) {
      var card = el("div", "card");
      var head = el("div", "row--between");
      head.style.marginBottom = "var(--sp-3)";
      head.appendChild(el("h3", null, "What changed"));
      if (data.resolved_pending) {
        head.appendChild(el("span", "badge badge--safe",
          plural(data.resolved_pending, "pending lecture") + " resolved"));
      }
      card.appendChild(head);

      changes.slice(0, 40).forEach(function (c) {
        var row = el("div", "diff-row");
        row.appendChild(el("span", null,
          c.subject_code + " · " + fmtDate(c.on_date) + " " + fmtTime(c.start_time)));
        var spacer = el("span");
        spacer.style.flex = "1";
        row.appendChild(spacer);
        row.appendChild(el("span", "text-" + (STATUS_TONE[c.from_status] || "muted"),
                           STATUS_WORD[c.from_status] || c.from_status));
        row.appendChild(el("span", "diff-arrow", "→"));
        row.appendChild(el("span", "text-" + (STATUS_TONE[c.to_status] || "muted"),
                           STATUS_WORD[c.to_status] || c.to_status));
        card.appendChild(row);
      });
      if (changes.length > 40) {
        card.appendChild(el("div", "text-muted", "…and " + (changes.length - 40) + " more"));
      }

      moves.forEach(function (code) {
        var move = data.pct_moves[code];
        var row = el("div", "diff-row");
        row.appendChild(el("span", null, "Worst-case " + code));
        var spacer = el("span");
        spacer.style.flex = "1";
        row.appendChild(spacer);
        row.appendChild(el("span", "mono", move[0] === null ? "—" : move[0].toFixed(1) + "%"));
        row.appendChild(el("span", "diff-arrow", "→"));
        var after = el("b", "mono " + (move[0] !== null && move[1] < move[0] ? "text-danger" : "text-safe"),
                       move[1] === null ? "—" : move[1].toFixed(1) + "%");
        row.appendChild(after);
        card.appendChild(row);
      });

      result.appendChild(card);
    }

    var actions = el("div", "row");
    var link = el("a", "btn btn--primary", "See my budget");
    link.href = "/";
    actions.appendChild(link);
    result.appendChild(actions);
  }

  function renderProposals(data) {
    result.innerHTML = "";
    result.appendChild(banner("info", "One quick question",
      "A course name changed. Tell Bunkr once and it will remember forever."));

    var card = el("div", "card");
    var answers = {};

    data.proposals.forEach(function (p) {
      var box = el("div", "proposal");
      var q = el("div", "proposal__q");
      q.textContent = "“" + p.raw_name + "” looks like " + p.match_code + ".";
      box.appendChild(q);
      box.appendChild(el("div", "text-muted",
        p.canonical_name + " (" + p.lecture_type + ") vs " + p.match_name));

      var row = el("div", "row");
      row.style.marginTop = "var(--sp-3)";

      var yes = el("button", "btn btn--primary btn--sm", "Yes, same subject");
      var no = el("button", "btn btn--sm", "No, it's new");

      function choose(value, chosen, other) {
        answers[p.raw_name] = value;
        chosen.classList.add("is-chosen");
        chosen.setAttribute("aria-pressed", "true");
        other.classList.remove("is-chosen");
        other.setAttribute("aria-pressed", "false");
        other.style.opacity = "0.5";
        chosen.style.opacity = "1";
      }
      yes.addEventListener("click", function () {
        choose("merge:" + p.match_subject_id, yes, no);
      });
      no.addEventListener("click", function () { choose("new", no, yes); });

      row.appendChild(yes);
      row.appendChild(no);
      box.appendChild(row);
      card.appendChild(box);
    });

    var confirm = el("button", "btn btn--accent", "Save and merge");
    confirm.addEventListener("click", function () {
      if (Object.keys(answers).length < data.proposals.length) {
        window.BunkrToast.error("Answer each question first.");
        return;
      }
      confirm.classList.add("is-loading");
      confirm.textContent = "Merging…";
      fetch("/api/reports/" + data.snapshot_id + "/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decisions: answers })
      })
        .then(window.BunkrApi.readJson)
        .then(function (res) {
          if (!res.ok) { showError(res.body.error || "Something went wrong."); return; }
          renderDiff(res.body);
        })
        .catch(function () { showError("Couldn't reach the server."); });
    });
    card.appendChild(confirm);
    result.appendChild(card);
  }

  function showError(message) {
    result.innerHTML = "";
    result.appendChild(banner("danger", "Upload failed", message));
  }

  /* ---- upload -------------------------------------------------------------- */

  function send(file) {
    if (!file) return;
    hint.textContent = file.name;
    result.innerHTML = "";
    result.appendChild(banner("info", "Reading your report…", file.name));

    var form = new FormData();
    form.append("report", file);

    fetch("/api/reports", { method: "POST", body: form })
      .then(window.BunkrApi.readJson)
      .then(function (res) {
        if (!res.ok) { showError(res.body.error || "Something went wrong."); return; }
        if (res.body.status === "duplicate") {
          result.innerHTML = "";
          result.appendChild(banner("info", "Nothing new",
            "You've already uploaded this exact file — your numbers are unchanged."));
          return;
        }
        if (res.body.status === "needs_confirmation") { renderProposals(res.body); return; }
        renderDiff(res.body);
      })
      .catch(function () { showError("Couldn't reach the server."); });
  }

  input.addEventListener("change", function () { send(input.files[0]); });

  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (e) {
      e.preventDefault();
      dropzone.classList.add("is-over");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (e) {
      e.preventDefault();
      dropzone.classList.remove("is-over");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    if (e.dataTransfer.files.length) send(e.dataTransfer.files[0]);
  });
})();
