/* The one place Bunkk talks to its JSON API.

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

  /* Every POST/PUT/DELETE carries the session's CSRF token, read from the
     <meta> tag base.html renders. A missing token is a 400, so a page left
     open across a deploy fails loudly rather than silently. */
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function headers(extra) {
    var out = { "X-CSRFToken": csrfToken() };
    Object.keys(extra || {}).forEach(function (k) { out[k] = extra[k]; });
    return out;
  }

  function request(method, url, payload) {
    var options = { method: method, headers: method === "GET" ? {} : headers() };
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

  window.BunkkApi = {
    get: function (url) { return request("GET", url); },
    post: function (url, payload) { return request("POST", url, payload); },
    put: function (url, payload) { return request("PUT", url, payload); },
    del: function (url, payload) { return request("DELETE", url, payload); },
    readJson: readJson,
    csrfToken: csrfToken,
    headers: headers
  };
})();
