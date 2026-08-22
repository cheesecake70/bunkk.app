/* The one place Bunkr talks to its JSON API.

   Every caller wants the same three things: send JSON, get JSON back, and be
   told plainly when that didn't work. Before this, three files each grew their
   own copy of that and only one of them handled the case that actually bites —
   a crashed Flask answering with an HTML error page, which JSON.parse turns
   into a "network error" and sends you looking in the wrong place. */
(function () {
  "use strict";

  /* A non-JSON body means the server broke before it could format a reply.
     Say so, rather than blaming the network. */
  function readJson(response) {
    return response.text().then(function (text) {
      try {
        return { ok: response.ok, status: response.status, body: JSON.parse(text) };
      } catch (e) {
        return {
          ok: false,
          status: response.status,
          body: { error: "The server hit an unexpected error (" + response.status + ")." }
        };
      }
    });
  }

  function request(method, url, payload) {
    var options = { method: method, headers: {} };
    if (payload !== undefined && payload !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    return fetch(url, options)
      .then(readJson)
      .catch(function () {
        return { ok: false, status: 0, body: { error: "Couldn't reach the server." } };
      });
  }

  window.BunkrApi = {
    get: function (url) { return request("GET", url); },
    post: function (url, payload) { return request("POST", url, payload); },
    put: function (url, payload) { return request("PUT", url, payload); },
    del: function (url, payload) { return request("DELETE", url, payload); },
    readJson: readJson
  };
})();
