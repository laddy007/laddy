/* laddy phone service worker: cache-first for the static shell ONLY.
   /api/ is NEVER cached or intercepted - control traffic must be live. */
"use strict";

var CACHE = "laddy-phone-v1";
var ASSETS = ["/", "/index.html", "/app.js", "/app.css", "/manifest.json"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(ASSETS); })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (key) {
        return key !== CACHE;
      }).map(function (key) { return caches.delete(key); }));
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.indexOf("/api/") === 0) {
    return; /* pass through untouched */
  }
  event.respondWith(
    caches.match(event.request).then(function (cached) {
      return cached || fetch(event.request);
    })
  );
});
