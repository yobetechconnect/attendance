/* Visible connection / sync status: a compact pill, a message banner and a
 * details panel with the separate indicators. Pure rendering - all state
 * comes from att-boot.js. All text is set with textContent (no innerHTML),
 * so student names or server text can never inject markup. */
(function (root) {
  'use strict';
  var doc = root.document;
  var SHORT = { online: 'Online', offline: 'Offline', syncing: 'Syncing\u2026', synced: 'Synced', error: 'Sync error',
    auth_required: 'Sign in', checking: 'Checking\u2026' };

  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function fmtTime(ms) {
    if (!ms) return 'Never';
    var d = new Date(ms);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function StatusBar(handlers) {
    this.h = handlers || {};
    this.pillHost = doc.getElementById('attStatus') || (function () {
      var top = doc.querySelector('.topbar'); if (!top) return null;
      var h = el('div', 'att-pill-host'); top.appendChild(h); return h;
    })();
    this.bannerHost = doc.getElementById('attBanner') || (function () {
      var c = doc.querySelector('.content'); if (!c) return null;
      var b = el('div', 'att-banner-zone'); b.setAttribute('aria-live', 'polite'); c.insertBefore(b, c.firstChild); return b;
    })();
    this.open = false;
    this.last = null;
  }

  StatusBar.prototype.render = function (s) {
    this.last = s;
    var c = s.conn;
    if (this.pillHost) {
      this.pillHost.textContent = '';
      var btn = el('button', 'att-pill att-' + c.color);
      btn.type = 'button'; btn.setAttribute('aria-expanded', String(this.open));
      btn.title = c.message;
      btn.appendChild(el('span', 'att-dot'));
      btn.appendChild(el('span', 'att-pill-label', SHORT[c.code] || c.code));
      if (s.counts && s.counts.pendingRecords > 0) btn.appendChild(el('span', 'att-count', String(s.counts.pendingRecords)));
      var self = this;
      btn.addEventListener('click', function () { self.open = !self.open; self.render(self.last); });
      this.pillHost.appendChild(btn);
      if (this.open) this.pillHost.appendChild(this._panel(s));
    }
    if (this.bannerHost) {
      this.bannerHost.textContent = '';
      var show = c.code !== 'checking' && (c.code !== 'online' || s.showOnlineBanner);
      if (show) {
        var b = el('div', 'att-banner att-' + c.color);
        b.setAttribute('role', 'status');
        b.appendChild(el('span', null, c.message));
        if (c.code === 'auth_required') {
          var a = el('a', 'att-link', 'Sign in'); a.href = '/auth/login'; b.appendChild(doc.createTextNode(' ')); b.appendChild(a);
        }
        this.bannerHost.appendChild(b);
      }
      if (s.needsUnlock) {
        var ub = el('div', 'att-banner att-blue');
        ub.appendChild(el('span', null, 'Face enrollments are waiting to upload. Unlock face data with your PIN so they can sync. '));
        var ubtn = el('button', 'att-btn att-btn-small', 'Unlock'); ubtn.type = 'button';
        ubtn.addEventListener('click', function () { if (self.h.unlock) self.h.unlock(); });
        ub.appendChild(ubtn); this.bannerHost.appendChild(ub);
      }
      if (s.otherUserPending) {
        this.bannerHost.appendChild(el('div', 'att-banner att-red',
          'This device holds unsynced attendance from another account. Sign in as that teacher to synchronize it.'));
      }
      if (s.updateReady) {
        var u = el('div', 'att-banner att-blue');
        u.appendChild(el('span', null, 'A newer version of the app is ready. Your saved attendance is safe. '));
        var rb = el('button', 'att-btn att-btn-small', 'Reload now');
        rb.type = 'button';
        rb.addEventListener('click', function () { if (self.h.reload) self.h.reload(); });
        u.appendChild(rb); this.bannerHost.appendChild(u);
      } else if (s.outdated) {
        this.bannerHost.appendChild(el('div', 'att-banner att-blue',
          'You are using an older version of the app. It will update when you next open it online.'));
      }
    }
  };

  StatusBar.prototype._panel = function (s) {
    var self = this, p = el('div', 'att-panel'), c = s.counts || {};
    function row(k, v, cls) {
      var r = el('div', 'att-row'); r.appendChild(el('span', 'att-k', k)); r.appendChild(el('span', 'att-v' + (cls ? ' ' + cls : ''), v)); p.appendChild(r);
    }
    row('Network', s.browserOnline ? 'Connected' : 'No network', s.browserOnline ? 'att-ok' : 'att-bad');
    row('Server', s.reachable === null ? 'Checking\u2026' : (s.reachable ? 'Reachable' : 'Not reachable'),
      s.reachable ? 'att-ok' : (s.reachable === false ? 'att-bad' : ''));
    if (s.hasStore) {
      row('Local database', s.dbOk ? 'Ready' : 'Unavailable', s.dbOk ? 'att-ok' : 'att-bad');
      row('Pending records', String(c.pendingRecords || 0), c.pendingRecords ? 'att-warn' : 'att-ok');
      if (c.conflictRecords || c.rejectedRecords || c.rejectedSessions) {
        row('Needs attention', (c.conflictRecords || 0) + ' conflict(s), ' + ((c.rejectedRecords || 0)) + ' rejected', 'att-warn');
      }
      row('Last successful sync', fmtTime(s.lastSyncAt));
      row('Face recognition', s.faceEngine || 'Not enabled', s.faceEngineOk ? 'att-ok' : '');
    }
    row('App version', s.version + (s.serverVersion && s.serverVersion !== s.version ? ' (server has ' + s.serverVersion + ')' : ''));
    var acts = el('div', 'att-panel-actions');
    var check = el('button', 'att-btn att-btn-small', 'Check connection'); check.type = 'button';
    check.addEventListener('click', function () { if (self.h.check) self.h.check(); });
    acts.appendChild(check);
    if (s.hasStore) {
      var retry = el('button', 'att-btn att-btn-small', 'Sync now'); retry.type = 'button';
      retry.addEventListener('click', function () { if (self.h.sync) self.h.sync(); });
      acts.appendChild(retry);
    }
    p.appendChild(acts);
    return p;
  };

  root.AttStatusBar = StatusBar;
})(typeof self !== 'undefined' ? self : this);
