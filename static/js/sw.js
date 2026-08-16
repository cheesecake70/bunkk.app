/* Bunkmate service worker — served from / so its scope covers the whole app.
 *
 * Caching policy is deliberately narrow: static assets only. Pages are never
 * cached, because every page in this app contains one student's attendance and
 * a shared or reinstalled device shouldn't be able to read it out of a cache.
 * Offline therefore means "the shell loads and explains itself", not "your
 * numbers are available" — and stale numbers are exactly what this app must
 * never show.
 */
const VERSION = "bunkmate-v1";
const SHELL = [
  "/offline",
  "/static/css/tokens.css",
  "/static/css/components.css",
  "/static/css/app.css",
  "/static/icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;          // always live

  if (request.mode === "navigate") {
    // Network-first, and the response is never stored.
    event.respondWith(
      fetch(request).catch(() => caches.match("/offline"))
    );
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request)
          .then((response) => {
            if (response && response.ok) {
              const copy = response.clone();
              caches.open(VERSION).then((cache) => cache.put(request, copy));
            }
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});

/* ---- Push ---------------------------------------------------------------- */

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { title: "Bunkmate", body: event.data ? event.data.text() : "" };
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || "Bunkmate", {
      body: payload.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      tag: payload.tag || "bunkmate",
      renotify: true,
      data: { url: payload.url || "/" }
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((windows) => {
        for (const client of windows) {
          if (client.url.includes(target) && "focus" in client) return client.focus();
        }
        return self.clients.openWindow ? self.clients.openWindow(target) : null;
      })
  );
});
