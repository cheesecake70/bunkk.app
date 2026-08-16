/* Plan page: commit or drop a future absence, then reload so every number on
   the page comes from one recomputation rather than being patched in pieces. */
(function () {
  "use strict";

  function post(url, options) {
    return fetch(url, options)
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) { alert(res.body.error || "That didn't work."); return; }
        window.location.reload();
      })
      .catch(function () { alert("Couldn't reach the server."); });
  }

  document.addEventListener("click", function (event) {
    var commit = event.target.closest(".js-plan");
    if (commit) {
      commit.disabled = true;
      post("/api/absences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: commit.dataset.date })
      });
      return;
    }

    var drop = event.target.closest(".js-drop");
    if (drop) {
      drop.disabled = true;
      post("/api/absences/" + drop.dataset.id, { method: "DELETE" });
    }
  });
})();
