/* Orchestration shared by every page: connection monitor, status bar,
 * service worker, and (for teachers) the local store + automatic sync.
 *
 * Sync is triggered by: app open, the browser coming back online, a probe that
 * flips unreachable -> reachable, the app returning to the foreground, a
 * saved session, the retry timer, and the "Sync now" button. Background Sync
 * is only an extra hint - it is never the only mechanism.
 */
(function (root) {
  'use strict';
  var doc = root.document, body = doc.body, Core = root.AttCore, Conn = root.AttConnection;
  var shell = body.getAttribute('data-att-shell') === '1';
  var role = body.getAttribute('data-att-role');
  var pageUser = body.getAttribute('data-att-user') ? parseInt(body.getAttribute('data-att-user'), 10) : null;
  var version = body.getAttribute('data-att-version') || '0';
  var teacherPage = role === 'teacher' || role === 'attendance_operator';
  var wantsStore = shell || teacherPage;
  var REFRESH_AFTER_MS = 6 * 3600 * 1000;

  var listeners = {};
  var att = {
    version: version, shell: shell, api: new Conn.ApiClient(), store: null, engine: null, deviceUid: null,
    dbOk: null, updateReady: false, recentlySyncedUntil: 0, onlineBannerUntil: 0, syncing: false,
    authRequiredPending: false, otherUserPending: false, lastCounts: null, lastSyncAt: null, prepareError: null,
    on: function (n, fn) { (listeners[n] = listeners[n] || []).push(fn); },
    emit: function (n, d) { (listeners[n] || []).forEach(function (f) { try { f(d); } catch (e) { /* listener errors must not break sync */ } }); }
  };
  var readyResolve;
  att.ready = new Promise(function (r) { readyResolve = r; });   // exists before start() so other scripts can wait on it
  att.monitor = new Conn.ConnectionMonitor(att.api, function (n, d) { att.emit('conn-' + n, d); onConn(n); });

  /* ---------------------------------------------------------- identity */
  att.currentUserId = function () {
    var m = att.monitor.state;
    if (m.authenticated && m.userId) return Promise.resolve(m.userId);
    if (pageUser) return Promise.resolve(pageUser);
    if (att.store) return att.store.getLastUserId().then(function (v) { return v || null; });
    return Promise.resolve(null);
  };

  /* ------------------------------------------------------------ status */
  att.snapshot = function () {
    var m = att.monitor.state, c = att.lastCounts || {};
    var now = Date.now();
    var conn = Core.computeConnectionState({
      reachable: m.reachable, syncing: att.syncing, pendingRecords: c.pendingRecords || 0,
      failedSessions: c.failedSessions || 0, rejectedSessions: c.rejectedSessions || 0,
      recentlySynced: now < att.recentlySyncedUntil,
      authRequiredPending: !!(m.reachable && m.authenticated === false && (c.pendingRecords || 0) > 0)
    });
    return {
      conn: conn, reachable: m.reachable, browserOnline: m.browserOnline, counts: c, hasStore: !!att.store,
      dbOk: att.dbOk, lastSyncAt: c.lastSyncAt || null, version: version, serverVersion: m.serverVersion,
      updateReady: att.updateReady, outdated: !!(m.serverVersion && m.serverVersion !== version && !att.updateReady),
      showOnlineBanner: now < att.onlineBannerUntil, otherUserPending: att.otherUserPending,
      faceEngine: att.engineState ? att.engineState.message : null, faceEngineOk: !!(att.engineState && att.engineState.status === 'ready'),
      needsUnlock: !!(att.faces && att.lastEnrollCounts && att.lastEnrollCounts.unsent > 0 && !att.faces.vault)
    };
  };
  att.refresh = function () {
    var p = att.store ? att.currentUserId().then(function (uid) {
      if (!uid) return null;
      return att.store.counts(uid).then(function (c) {
        if (!att.faces) { att.lastCounts = c; return; }
        return att.faces.enrollmentCounts(uid).then(function (ec) {
          att.lastEnrollCounts = ec;
          // Unsent enrollments count as pending records, and a failed one counts as a failed upload.
          c.pendingEnrollments = ec.unsent; c.pendingRecords += ec.unsent; c.failedSessions += ec.failed; c.conflictRecords += ec.conflict; c.rejectedRecords += ec.rejected;
          att.lastCounts = c;
        });
      });
    }).catch(function () { att.dbOk = false; }) : Promise.resolve();
    return p.then(function () { if (att.bar) att.bar.render(att.snapshot()); att.emit('refreshed', att.snapshot()); });
  };

  /* -------------------------------------------------------------- sync */
  att.syncNow = function (manual) {
    var m = att.monitor.state;
    if (!att.store || !att.engine) return Promise.resolve({ skipped: 'no_store' });
    if (!m.reachable) return Promise.resolve({ skipped: 'offline' });
    if (!m.authenticated) { return att.refresh().then(function () { return { skipped: 'auth_required' }; }); }
    if (!m.offlineCapable && !m.enrollCapable) return Promise.resolve({ skipped: 'not_capable' });
    att.syncing = true; att.refresh();
    var sessions = m.offlineCapable ? att.engine.syncNow({ userId: m.userId, manual: !!manual }) : Promise.resolve(null);
    return sessions.then(function (res) {
      if (!(m.enrollCapable && att.faceSync)) return res;
      return att.faceSync.syncNow({ userId: m.userId, manual: !!manual }).then(function (er) {
        if (!er || er.skipped) return res;                       // vault locked / busy: nothing changed
        var merged = Object.assign({}, res || {});
        merged.complete = (res ? !!res.complete : true) && !!er.complete;   // "complete" only if BOTH kinds are fully synced
        merged.anySynced = !!((res && res.anySynced) || er.anySynced);
        return merged;
      });
    }).then(function (res) {
      att.syncing = false;
      if (res && res.complete && res.anySynced) {
        att.recentlySyncedUntil = Date.now() + 8000;
        setTimeout(att.refresh, 8100);
      }
      return att.refresh().then(function () { scheduleRetry(m.userId); return res; });
    }, function (err) {
      att.syncing = false; att.refresh(); throw err;
    });
  };
  var retryTimer = null;
  function scheduleRetry(userId) {
    clearTimeout(retryTimer);
    Promise.all([att.store.earliestRetryAt(userId), att.faces ? att.faces.listEnrollments(userId) : Promise.resolve([])]).then(function (r) {
      var times = r[1].filter(function (e) { return e.syncStatus === 'failed'; }).map(function (e) { return e.nextRetryAt || 0; });
      if (r[0]) times.push(r[0]);
      var t = times.length ? Math.min.apply(null, times) : null;
      if (t) retryTimer = setTimeout(function () { att.syncNow(false); }, Math.max(1000, t - Date.now()) + 250);
    });
  }

  /* ----------------------------------------------------------- prepare */
  att.prepare = function (force) {
    var m = att.monitor.state;
    if (!att.store) return Promise.resolve({ ok: false, reason: 'no_store' });
    if (!m.reachable) return Promise.resolve({ ok: false, reason: 'offline' });
    if (!m.authenticated) return Promise.resolve({ ok: false, reason: 'auth_required' });
    if (!m.offlineCapable) return Promise.resolve({ ok: false, reason: 'not_capable' });
    return att.store.getProfile(m.userId).then(function (p) {
      if (!force && p && !p.locked && Date.now() - p.preparedAt < REFRESH_AFTER_MS) {
        return att.store.verifyOnline(m.userId).then(function () { return { ok: true, reason: 'fresh' }; });
      }
      return att.api.bootstrap().then(function (r) {
        if (r.status === 200 && r.body && r.body.ok) {
          return att.store.saveBootstrap(r.body).then(function () {
            if (root.navigator.storage && root.navigator.storage.persist) root.navigator.storage.persist();
            att.prepareError = null; att.emit('prepared', r.body);
            return { ok: true, reason: 'downloaded' };
          });
        }
        att.prepareError = 'http_' + r.status;
        return { ok: false, reason: 'server', status: r.status };
      }, function () { att.prepareError = 'network'; return { ok: false, reason: 'network' }; });
    }).then(function (res) { return att.refresh().then(function () { return res; }); });
  };

  /* Super Administrator: download the student list used for offline enrollment (names/ids only). */
  att.prepareEnroll = function (force) {
    var m = att.monitor.state;
    if (!att.store || !att.faces) return Promise.resolve({ ok: false, reason: 'no_store' });
    if (!m.reachable) return Promise.resolve({ ok: false, reason: 'offline' });
    if (!m.authenticated) return Promise.resolve({ ok: false, reason: 'auth_required' });
    if (!m.enrollCapable) return Promise.resolve({ ok: false, reason: 'not_capable' });
    return att.faces.getEnrollRoster(m.userId).then(function (r) {
      if (!force && r && Date.now() - r.at < REFRESH_AFTER_MS) return { ok: true, reason: 'fresh' };
      return att.api.enrollmentBootstrap().then(function (resp) {
        if (resp.status === 409) return { ok: false, reason: 'face_not_configured' };
        if (resp.status !== 200 || !resp.body || !resp.body.ok) return { ok: false, reason: 'server', status: resp.status };
        return att.store.saveProfileOnly(resp.body).then(function () { return att.faces.saveEnrollRoster(m.userId, resp.body.students); })
          .then(function () { return { ok: true, reason: 'downloaded' }; });
      }, function () { return { ok: false, reason: 'network' }; });
    }).then(function (res) { return att.refresh().then(function () { return res; }); });
  };

  /* ---------------------------------------------------------- triggers */
  var lastHandledReach = null;
  function onConn(name) {
    var m = att.monitor.state;
    if (name === 'reconnected') att.onlineBannerUntil = Date.now() + 6000;
    if (m.reachable && m.authenticated && (m.offlineCapable || m.enrollCapable) && att.store) {
      att.store.verifyOnline(m.userId).then(function () {
        return m.offlineCapable ? att.prepare(false) : att.prepareEnroll(false);
      }).then(function () {
        if (lastHandledReach !== true || name === 'reconnected') return att.syncNow(false);
      }).catch(function () { /* surfaced through status */ }).then(function () {
        lastHandledReach = true; checkOtherUsers(m.userId);
      });
    } else {
      lastHandledReach = m.reachable === false ? false : lastHandledReach;
      att.refresh();
    }
  }
  function checkOtherUsers(currentId) {
    // Unsynced work from a DIFFERENT account on this shared device: cannot be sent from this session.
    att.store.getLastUserId().then(function (last) {
      if (last && last !== currentId) {
        att.store.counts(last).then(function (c) {
          att.otherUserPending = (c.pendingSessions + c.failedSessions + c.syncingSessions) > 0;
          att.refresh();
        });
      } else if (att.otherUserPending) { att.otherUserPending = false; att.refresh(); }
    });
  }

  /* ----------------------------------------------------- service worker */
  function registerSW() {
    if (!('serviceWorker' in root.navigator)) return;
    root.navigator.serviceWorker.register('/sw.js').then(function (reg) {
      att.swReg = reg;
      function watch(w) {
        w.addEventListener('statechange', function () {
          if (w.state === 'installed' && root.navigator.serviceWorker.controller) { att.updateReady = true; att.refresh(); }
        });
      }
      if (reg.waiting && root.navigator.serviceWorker.controller) { att.updateReady = true; }
      reg.addEventListener('updatefound', function () { if (reg.installing) watch(reg.installing); });
    }).catch(function () { /* the app still works online without a service worker */ });
    root.navigator.serviceWorker.addEventListener('message', function (e) {
      if (e.data && e.data.type === 'bg-sync') att.syncNow(false);
    });
  }
  att.reload = function () {
    var reg = att.swReg;
    if (reg && reg.waiting) {
      root.navigator.serviceWorker.addEventListener('controllerchange', function () { root.location.reload(); });
      reg.waiting.postMessage({ type: 'SKIP_WAITING' });
    } else root.location.reload();
  };
  att.requestBackgroundSync = function () {
    if (att.swReg && att.swReg.sync) att.swReg.sync.register('att-sync').catch(function () {});
  };

  /* ------------------------------------------------------------ logout */
  function hookLogout() {
    doc.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href$="/auth/logout"]');
      if (!a || !att.store || !teacherPage) return;
      e.preventDefault();
      var go = function () { root.location.href = a.href; };
      att.currentUserId().then(function (uid) {
        if (!uid) return go();
        return att.store.unsyncedFor(uid).then(function (n) {
          if (n > 0 && !root.confirm('You have ' + n + ' attendance sheet(s) that have not synchronized yet.\n\n' +
            'They will stay safely on this device (locked) and sync after you sign in again. Face data and the student list are removed from this device. Sign out anyway?')) return;
          return (att.faces ? att.faces.signOut(uid) : att.store.signOut(uid)).then(go, go);
        });
      }).catch(go);
    });
  }

  /* -------------------------------------------------------------- start */
  function start() {
    att.bar = new root.AttStatusBar({
      reload: att.reload,
      check: function () { att.monitor.probe('manual', true); },
      sync: function () { att.syncNow(true); },
      unlock: function () { if (att.promptUnlock) att.promptUnlock(); }
    });
    att.bar.render(att.snapshot());
    registerSW();
    doc.addEventListener('visibilitychange', function () {
      if (!doc.hidden && att.monitor.state.reachable) att.syncNow(false);
    });
    var ready = Promise.resolve();
    if (wantsStore && root.AttIdb) {
      var backend = new root.AttIdb.IdbBackend();
      att.store = new Core.Store(backend, {
        estimator: root.navigator.storage && root.navigator.storage.estimate ? function () { return root.navigator.storage.estimate(); } : null
      });
      ready = backend.available().then(function (ok) {
        att.dbOk = ok;
        if (!ok) { att.store = null; return; }
        return att.store.getDeviceUid().then(function (id) {
          att.deviceUid = id;
          att.engine = new Core.SyncEngine({ store: att.store, api: att.api, deviceUid: id, emit: function (n, d) { att.emit(n, d); if (n !== 'sync-progress') att.refresh(); } });
          if (root.AttFace) {
            att.faces = new root.AttFace.FaceStore(att.store);
            att.faceSync = new root.AttFace.EnrollmentSync({ faces: att.faces, api: att.api, deviceUid: id,
              emit: function (n, d) { att.emit(n, d); att.refresh(); } });
          }
          return att.currentUserId().then(function (uid) { return uid ? att.store.recoverInterrupted(uid) : null; });
        });
      }).catch(function () { att.dbOk = false; att.store = null; });
    }
    ready.then(function () {
      hookLogout();
      att.monitor.start();
      return att.refresh();
    }).then(readyResolve, readyResolve);
    att.emit('booted', att);
  }

  root.Att = att;
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start); else start();
})(typeof self !== 'undefined' ? self : this);
