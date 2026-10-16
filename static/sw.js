/* Service worker - AL-ISTI'ANAH attendance (offline shell).
 *
 * What it caches: ONLY the data-free application shell and its static files
 * (listed below), plus the immutable face-model files under /static/models/face/<version>/. It never caches pages for signed-in users, never caches
 * /offline-api/ or any other API response, and never touches IndexedDB, so a
 * new version can never delete saved attendance.
 *
 * Version management: __APP_VERSION__ is replaced by the server. A new version
 * installs in the background, waits, and the page offers "Reload now" - it is
 * not forced on a teacher in the middle of marking attendance.
 */
'use strict';
var VERSION = '__APP_VERSION__';
var CACHE = 'att-shell-v' + VERSION;
var SHELL = '/offline-app/';
var V = '?v=' + VERSION;
var MODEL_CACHE = 'att-models-v1';          // face-model files: kept across app updates (folder name carries the model version)
var PRECACHE = [
  SHELL,
  '/static/css/style.css' + V,
  '/static/css/offline.css' + V,
  '/static/js/offline/att-core.js' + V,
  '/static/js/offline/att-idb.js' + V,
  '/static/js/offline/att-connection.js' + V,
  '/static/js/offline/att-statusbar.js' + V,
  '/static/js/offline/att-boot.js' + V,
  '/static/js/offline/att-app.js' + V,
  '/static/js/offline/att-face.js' + V,
  '/static/js/offline/att-camera.js' + V,
  '/static/js/offline/att-face-engine.js' + V,
  '/static/js/offline/att-face-ui.js' + V,
  '/static/icons/icon-192.png',
  '/static/icons/apple-touch-icon.png',
  '/manifest.webmanifest'
];
/* /static/css/style.css and /static/js/app.js are also requested WITHOUT ?v= by
   the normal pages; those are served by the network (and the browser cache). */

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) {
    return Promise.all(PRECACHE.map(function (url) {
      return fetch(new Request(url, { cache: 'reload', credentials: 'omit' })).then(function (resp) {
        // Refuse redirects / error pages so a login page can never be stored as the shell.
        if (!resp.ok || resp.redirected) throw new Error('precache failed: ' + url);
        return cache.put(url, resp);
      });
    }));
  }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('att-shell-') === 0 && k !== CACHE; })  // model cache is NOT touched
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('message', function (event) {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
  if (event.data && event.data.type === 'GET_VERSION' && event.ports[0]) event.ports[0].postMessage(VERSION);
});

function networkFirst(request, cacheKey, timeoutMs) {
  return new Promise(function (resolve) {
    var done = false;
    var timer = setTimeout(function () { fromCache(); }, timeoutMs);
    function fromCache() {
      if (done) return;
      caches.open(CACHE).then(function (c) { return c.match(cacheKey); }).then(function (hit) {
        if (hit && !done) { done = true; clearTimeout(timer); resolve(hit); }
      });
    }
    fetch(request).then(function (resp) {
      if (done) return;
      done = true; clearTimeout(timer);
      if (resp.ok) { var copy = resp.clone(); caches.open(CACHE).then(function (c) { c.put(cacheKey, copy); }); }
      resolve(resp);
    }, function () {
      clearTimeout(timer);
      if (!done) caches.open(CACHE).then(function (c) { return c.match(cacheKey); }).then(function (hit) {
        done = true; resolve(hit || Response.error());
      });
    });
  });
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // The offline shell itself: network first (fresh when online), cache when offline/slow.
  if (url.pathname === SHELL) {
    event.respondWith(networkFirst(req, SHELL, 4000));
    return;
  }
  // Static shell files: cache first (they are versioned by ?v=).
  if (PRECACHE.indexOf(url.pathname + url.search) >= 0) {
    event.respondWith(caches.open(CACHE).then(function (c) {
      return c.match(url.pathname + url.search).then(function (hit) { return hit || fetch(req); });
    }));
    return;
  }
  // Face-model files are immutable per model version (the version is in the path): cache first, store on first use.
  if (url.pathname.indexOf('/static/models/face/') === 0) {
    event.respondWith(caches.open(MODEL_CACHE).then(function (c) {
      return c.match(req).then(function (hit) {
        if (hit) return hit;
        return fetch(req).then(function (resp) { if (resp.ok) c.put(req, resp.clone()); return resp; });
      });
    }));
    return;
  }
  // Any other page navigation: normal network; if the network is down, show the offline shell
  // (which holds no private data itself) so the teacher can still reach their saved classes.
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(function () {
      return caches.open(CACHE).then(function (c) { return c.match(SHELL); }).then(function (hit) { return hit || Response.error(); });
    }));
  }
  // Everything else (API calls, authenticated data, other assets) is NOT intercepted or cached.
});

/* Best-effort hint only: wake any open page so it runs its own sync. The page-side
   triggers (open, online, foreground, retry timer, button) do not depend on this. */
self.addEventListener('sync', function (event) {
  if (event.tag !== 'att-sync') return;
  event.waitUntil(self.clients.matchAll({ includeUncontrolled: true }).then(function (clients) {
    clients.forEach(function (c) { c.postMessage({ type: 'bg-sync' }); });
  }));
});
