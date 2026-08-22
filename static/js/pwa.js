/* PWA glue: register the service worker, and manage the push subscription from
   Settings. Everything degrades quietly — an unsupported browser or an
   unconfigured server just leaves the controls disabled with a reason. */
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function (err) {
        console.warn("Service worker registration failed:", err);
      });
    });
  }

  var toggle = document.getElementById("push-toggle");
  var status = document.getElementById("push-status");
  var testBtn = document.getElementById("push-test");
  if (!toggle || !status) return;

  var supported = "serviceWorker" in navigator && "PushManager" in window;

  function say(message) { status.textContent = message; }

  function urlBase64ToUint8Array(base64String) {
    var padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(base64);
    var output = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; ++i) output[i] = raw.charCodeAt(i);
    return output;
  }

  if (!supported) {
    toggle.disabled = true;
    say("This browser can't do push notifications. On iPhone, add Bunkr to your home screen first.");
    return;
  }
  if (toggle.dataset.configured !== "1") {
    toggle.disabled = true;
    say("Notifications aren't configured on this server yet (no VAPID keys).");
    return;
  }

  function subscribe() {
    return Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") {
        toggle.checked = false;
        say("Notifications are blocked in your browser settings.");
        return;
      }
      return navigator.serviceWorker.ready.then(function (registration) {
        return registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(toggle.dataset.key)
        });
      }).then(function (subscription) {
        var json = subscription.toJSON();
        return fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            endpoint: subscription.endpoint,
            keys: json.keys,
            user_agent: navigator.userAgent
          })
        });
      }).then(function () {
        say("On — you'll get tomorrow's verdict each morning.");
        if (testBtn) testBtn.hidden = false;
      });
    });
  }

  function unsubscribe() {
    return navigator.serviceWorker.ready
      .then(function (registration) { return registration.pushManager.getSubscription(); })
      .then(function (subscription) {
        if (!subscription) return;
        var endpoint = subscription.endpoint;
        return subscription.unsubscribe().then(function () {
          return fetch("/api/push/unsubscribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ endpoint: endpoint })
          });
        });
      })
      .then(function () {
        say("Off — nothing will be sent.");
        if (testBtn) testBtn.hidden = true;
      });
  }

  toggle.addEventListener("change", function () {
    toggle.disabled = true;
    say("Working…");
    var action = toggle.checked ? subscribe() : unsubscribe();
    action.catch(function (err) {
      console.warn(err);
      toggle.checked = !toggle.checked;
      say("That didn't work — try again.");
    }).then(function () { toggle.disabled = false; });
  });

  if (testBtn) {
    testBtn.addEventListener("click", function () {
      testBtn.disabled = true;
      fetch("/api/push/test", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          say(body.error ? body.error : "Sent — check your notifications.");
        })
        .catch(function () { say("Couldn't reach the server."); })
        .then(function () { testBtn.disabled = false; });
    });
  }
})();
