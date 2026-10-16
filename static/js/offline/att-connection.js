/* Real connectivity checks (not just navigator.onLine) and the API client.
 *
 * "Online" here means: a request to OUR server succeeded and the server could
 * reach its database. Anything else (no network, captive portal, server down,
 * database down, timeout) counts as unreachable.
 */
(function (root) {
  'use strict';

  function timeoutFetch(url, opts, ms) {
    var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = null, timedOut = false;
    if (ctl) { opts.signal = ctl.signal; timer = setTimeout(function () { timedOut = true; ctl.abort(); }, ms); }
    return fetch(url, opts).then(function (r) { clearTimeout(timer); return r; }, function (e) {
      clearTimeout(timer);
      var err = new Error('network'); err.kind = timedOut ? 'timeout' : 'network'; throw err;
    });
  }
  function readJson(resp) { return resp.json().then(function (b) { return b; }, function () { return null; }); }

  /* ------------------------------------------------------------ API client */
  function ApiClient() { this.csrf = null; }
  ApiClient.prototype.ping = function () {
    var self = this;
    return timeoutFetch('/offline-api/ping', { method: 'GET', cache: 'no-store', credentials: 'same-origin' }, 6000)
      .then(function (r) {
        return readJson(r).then(function (b) {
          if (r.ok && b && b.ok) { if (b.csrf_token) self.csrf = b.csrf_token; return { reachable: true, body: b }; }
          return { reachable: false, status: r.status, body: b };   // server answered but is unhealthy
        });
      }, function (e) { return { reachable: false, error: e.kind }; });
  };
  ApiClient.prototype.bootstrap = function () {
    return timeoutFetch('/offline-api/bootstrap', { method: 'GET', cache: 'no-store', credentials: 'same-origin' }, 25000)
      .then(function (r) { return readJson(r).then(function (b) { return { status: r.status, body: b }; }); });
  };
  ApiClient.prototype.faceTemplates = function (classId) {
    return timeoutFetch('/offline-api/face-templates?class_id=' + encodeURIComponent(classId),
      { method: 'GET', cache: 'no-store', credentials: 'same-origin' }, 30000)
      .then(function (r) { return readJson(r).then(function (b) { return { status: r.status, body: b }; }); });
  };
  ApiClient.prototype.enrollmentBootstrap = function () {
    return timeoutFetch('/offline-api/enrollment-bootstrap', { method: 'GET', cache: 'no-store', credentials: 'same-origin' }, 30000)
      .then(function (r) { return readJson(r).then(function (b) { return { status: r.status, body: b }; }); });
  };
  ApiClient.prototype.syncSession = function (payload) { return this._post('/offline-api/sync', payload); };
  ApiClient.prototype.syncEnrollment = function (payload) { return this._post('/offline-api/enrollments', payload); };
  ApiClient.prototype._post = function (url, payload) {
    var self = this;
    function send() {
      return timeoutFetch(url, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf || '' },
        body: JSON.stringify(payload)
      }, 30000).then(function (r) { return readJson(r).then(function (b) { return { status: r.status, body: b }; }); });
    }
    var signedOut = { status: 401, body: { ok: false, code: 'auth_required' } };
    function ensureToken() {
      if (self.csrf) return Promise.resolve(true);
      return self.ping().then(function (p) { return !(p.reachable && p.body && !p.body.authenticated); });
    }
    return ensureToken().then(function (ok) {
      if (!ok) return signedOut;
      return send().then(function (res) {
        // HTML 400 = CSRF token rejected (e.g. rotated session). Refresh once; if the
        // session itself is gone report "sign in again" instead of a generic failure.
        if (res.status === 400 && !res.body) {
          return self.ping().then(function (p) {
            if (p.reachable && p.body && !p.body.authenticated) return signedOut;
            return send();
          });
        }
        return res;
      });
    });
  };

  /* --------------------------------------------------------------- monitor */
  function ConnectionMonitor(api, emit) {
    this.api = api; this.emit = emit || function () {};
    this.state = { browserOnline: typeof navigator === 'undefined' ? true : navigator.onLine !== false,
      reachable: null, authenticated: null, userId: null, role: null, offlineCapable: false, enrollCapable: false,
      failures: 0, lastProbeAt: 0, serverVersion: null, lastError: null, hidden: false };
    this._timer = null; this._inflight = null;
  }
  ConnectionMonitor.prototype.start = function () {
    var self = this;
    root.addEventListener('online', function () { self.state.browserOnline = true; self.probe('online-event', true); });
    root.addEventListener('offline', function () {
      self.state.browserOnline = false; self._set({ reachable: false, lastError: 'browser_offline' });
    });
    if (root.document) {
      root.document.addEventListener('visibilitychange', function () {
        self.state.hidden = root.document.hidden;
        if (!root.document.hidden) self.probe('foreground');
      });
      this.state.hidden = root.document.hidden;
    }
    return this.probe('start', true);
  };
  ConnectionMonitor.prototype._set = function (patch) {
    var prev = this.state.reachable, changed = false, k;
    for (k in patch) { if (this.state[k] !== patch[k]) { this.state[k] = patch[k]; changed = true; } }
    if (changed) this.emit('change', this.state);
    if (prev === false && this.state.reachable === true) this.emit('reconnected', this.state);
  };
  /* At most one probe in flight, and never more often than every 3 seconds
     unless forced (e.g. the browser just reported it is back online). */
  ConnectionMonitor.prototype.probe = function (reason, force) {
    var self = this, now = Date.now();
    if (this._inflight) return this._inflight;
    if (!force && now - this.state.lastProbeAt < 3000) return Promise.resolve(this.state);
    this.state.lastProbeAt = now;
    this._inflight = this.api.ping().then(function (r) {
      self._inflight = null;
      if (r.reachable) {
        var b = r.body;
        self.state.failures = 0;
        self._set({ reachable: true, authenticated: !!b.authenticated, userId: b.user_id || null, role: b.role || null,
          offlineCapable: !!b.offline_capable, enrollCapable: !!b.enroll_capable, serverVersion: b.app_version, lastError: null });
      } else {
        self.state.failures += 1;
        self._set({ reachable: false, lastError: r.error || ('http_' + r.status) });
      }
      self._schedule();
      return self.state;
    });
    return this._inflight;
  };
  ConnectionMonitor.prototype._schedule = function () {
    var self = this;
    clearTimeout(this._timer);
    var delay = root.AttCore.nextProbeDelay(this.state);
    this._timer = setTimeout(function () { self.probe('timer', true); }, delay + Math.round(Math.random() * 1500));
  };

  root.AttConnection = { ApiClient: ApiClient, ConnectionMonitor: ConnectionMonitor };
})(typeof self !== 'undefined' ? self : this);
