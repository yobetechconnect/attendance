/* Offline attendance screens (shell page only).
 * Views: home (classes + saved sheets) -> attendance sheet -> saved sheet detail.
 * Every piece of text from storage/server is inserted with textContent. */
(function (root) {
  'use strict';
  var doc = root.document, Core = root.AttCore;
  if (!doc.body.getAttribute('data-att-shell')) return;
  var host = doc.getElementById('attApp');
  var A = root.Att;
  var view = { name: 'home' };
  /* Extension points used by att-face-ui.js (face scanning / enrollment). */
  var UI = root.AttUI = { hooks: { home: [], sheet: [] }, h: null, clear: null, toast: null, fmt: null, host: host,
    setView: function (v) { view = v; }, getView: function () { return view; } };

  function h(tag, attrs) {
    var e = doc.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'class') e.className = attrs[k];
      else if (k === 'text') e.textContent = attrs[k];
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      e.appendChild(typeof c === 'string' ? doc.createTextNode(c) : c);
    }
    return e;
  }
  function clear() { host.textContent = ''; }
  UI.h = h; UI.clear = clear;
  function toast(msg, kind) {
    var t = h('div', { class: 'att-toast att-' + (kind || 'blue'), role: 'status', text: msg });
    doc.body.appendChild(t); setTimeout(function () { t.remove(); }, 4500);
  }
  function fmt(ms) { return new Date(ms).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); }
  UI.toast = toast; UI.fmt = fmt;

  var STATUS_LABEL = {
    pending: ['Saved on this device \u2013 waiting to sync', 'att-orange'],
    syncing: ['Synchronizing\u2026', 'att-orange'],
    synced: ['Synchronized', 'att-green'],
    partial: ['Synchronized \u2013 some records need review', 'att-orange'],
    failed: ['Sync failed \u2013 will retry', 'att-red'],
    rejected: ['Rejected by server \u2013 needs attention', 'att-red']
  };
  function chip(session) {
    if (session.status === 'draft') return h('span', { class: 'att-chip att-blue', text: 'In progress (not saved)' });
    var l = STATUS_LABEL[session.syncStatus] || ['Saved', 'att-blue'];
    return h('span', { class: 'att-chip ' + l[1], text: l[0] });
  }

  /* ----------------------------------------------------------------- home */
  function renderHome() {
    view = { name: 'home' };
    UI.renderHome = renderHome;
    return A.currentUserId().then(function (uid) {
      if (!A.store) return blocked('This browser cannot store attendance offline (local database unavailable).');
      if (!uid) return noProfile(null);
      var m = A.monitor.state;
      return A.store.offlineAccess(uid).then(function (acc) {
        // Online and signed in as this user -> always allowed (server is the authority).
        var online = m.reachable && m.authenticated && m.userId === uid;
        if (!acc.allowed && !online) return accessDenied(acc, uid);
        if (m.reachable && m.authenticated === false) return signInNeeded(uid, acc);
        return Promise.all([A.store.getAssignments(uid), A.store.listSessions(uid, 30), A.store.getProfile(uid)])
          .then(function (r) { draw(uid, acc, r[0], r[1], r[2]); });
      });
    });
  }
  function blocked(msg) { clear(); host.appendChild(h('div', { class: 'att-card att-red-soft' }, h('p', { text: msg }))); }
  function noProfile() {
    clear();
    var m = A.monitor.state;
    host.appendChild(h('div', { class: 'att-card' },
      h('h2', { text: 'This device is not ready for offline use' }),
      h('p', { text: 'Sign in while online, then open this page once so your classes can be downloaded to this device.' }),
      m.reachable === false ? h('p', { class: 'att-muted', text: "You're offline right now, so nothing can be downloaded." })
        : h('a', { class: 'att-btn', href: '/auth/login?next=/offline-app/', text: 'Sign in' })));
  }
  function accessDenied(acc, uid) {
    clear();
    var why = acc.reason === 'expired'
      ? 'Offline access for this device has expired. Connect to the internet and sign in to renew it. Your unsynced records are safe and will sync afterwards.'
      : acc.reason === 'signed_out'
        ? 'You signed out on this device. Sign in again to continue and to sync any records still waiting.'
        : 'This device has no offline data for your account yet.';
    host.appendChild(h('div', { class: 'att-card' }, h('h2', { text: 'Offline access is locked' }), h('p', { text: why }),
      h('a', { class: 'att-btn', href: '/auth/login?next=/offline-app/', text: 'Sign in' })));
  }
  function signInNeeded(uid) {
    clear();
    host.appendChild(h('div', { class: 'att-card' }, h('h2', { text: 'Sign in to continue' }),
      h('p', { text: 'Your session has ended. Records saved on this device are safe and will synchronize after you sign in.' }),
      h('a', { class: 'att-btn', href: '/auth/login?next=/offline-app/', text: 'Sign in' })));
  }

  function draw(uid, acc, assignments, sessions, profile) {
    clear();
    var m = A.monitor.state;
    var expiry = acc.expiresAt ? 'Offline access valid until ' + fmt(acc.expiresAt) : '';
    host.appendChild(h('div', { class: 'att-card' },
      h('div', { class: 'att-between' },
        h('div', null, h('strong', { text: profile ? profile.name : 'Teacher' }),
          h('div', { class: 'att-muted', text: profile ? ('Class data downloaded ' + fmt(profile.preparedAt)) : '' }),
          h('div', { class: 'att-muted', text: expiry })),
        h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Refresh classes',
          onclick: function () {
            A.prepare(true).then(function (r) {
              toast(r.ok ? 'Classes updated.' : (r.reason === 'offline' ? "You're offline. Connect to refresh." : 'Could not update classes.'), r.ok ? 'green' : 'red');
              renderHome();
            });
          } }))));

    var teaching = !profile || profile.hasTeacherProfile !== false || assignments.length > 0;
    if (teaching) host.appendChild(h('h3', { text: 'Take attendance' }));
    if (teaching && !assignments.length) {
      host.appendChild(h('div', { class: 'att-card' }, h('p', { class: 'att-muted',
        text: m.reachable ? 'No classes are downloaded. Make sure you are assigned to a class and subject, then tap "Refresh classes".'
          : 'No classes are downloaded on this device. Connect to the internet once to download them.' })));
    }
    assignments.forEach(function (a) {
      host.appendChild(h('div', { class: 'att-card att-between' },
        h('div', null, h('strong', { text: a.className }), h('div', { text: a.subjectName }),
          h('div', { class: 'att-muted', text: a.studentCount + ' students \u00b7 ready offline' })),
        h('button', { class: 'att-btn', type: 'button', text: 'Open', onclick: function () { openSheet(uid, a); } })));
    });

    UI.hooks.home.forEach(function (fn) {
      var slot = h('div'); host.appendChild(slot);
      Promise.resolve(fn(uid, assignments, profile)).then(function (node) { if (node) slot.appendChild(node); }, function () { /* a failing extension must not break the page */ });
    });
    host.appendChild(h('h3', { text: 'Saved on this device' }));
    if (!sessions.length) host.appendChild(h('p', { class: 'att-muted', text: 'Nothing saved yet.' }));
    sessions.forEach(function (s) {
      host.appendChild(h('div', { class: 'att-card att-between att-click', onclick: function () { showSession(uid, s.clientUid); } },
        h('div', null, h('strong', { text: s.className + ' \u2013 ' + s.subjectName }),
          h('div', { class: 'att-muted', text: s.sessionDate + (s.serverSessionCode ? ' \u00b7 ' + s.serverSessionCode : '') })),
        chip(s)));
    });
    host.appendChild(h('div', { class: 'att-card att-danger-zone' },
      h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Sign out & clear this device', onclick: function () { signOut(uid); } }),
      h('p', { class: 'att-muted', text: 'Unsynced attendance is never deleted by signing out. It stays locked on this device until you sign in and it syncs.' })));
  }

  function signOut(uid) {
    A.store.unsyncedFor(uid).then(function (n) {
      if (n > 0 && !root.confirm(n + ' sheet(s) have not synchronized. They will stay on this device (locked). Sign out anyway?')) return;
      return (A.faces ? A.faces.signOut(uid) : A.store.signOut(uid)).then(function () { root.location.href = '/auth/logout'; });
    });
  }

  /* --------------------------------------------------------- the sheet */
  function openSheet(uid, a) {
    A.store.getProfile(uid).then(function (profile) {
      var school = Core.schoolNow(profile.settings.timezone);
      return A.store.openSheet(uid, a.assignmentId, school, A.deviceUid).then(function (r) {
        if (!r.created && r.session.status === 'saved') { toast('Today\u2019s sheet for this class is already saved.', 'blue'); return showSession(uid, r.session.clientUid); }
        return renderSheet(uid, r.session.clientUid);
      });
    }).catch(function (e) { toast(e.message === 'assignment_not_downloaded' ? 'This class is not downloaded. Refresh classes while online.' : 'Could not open the sheet.', 'red'); });
  }

  function renderSheet(uid, sessionUid) {
    view = { name: 'sheet', uid: uid, sessionUid: sessionUid };
    return Promise.all([A.store.getSession(sessionUid), A.store.listRecords(sessionUid), A.store.getProfile(uid)]).then(function (r) {
      var session = r[0], records = r[1], profile = r[2];
      return A.store.getRoster(uid, session.classId).then(function (roster) {
        clear();
        var byStudent = {};
        records.forEach(function (x) { byStudent[x.studentId] = x; });
        var marked = records.length;
        host.appendChild(h('div', { class: 'att-card' },
          h('div', { class: 'att-between' },
            h('div', null, h('strong', { text: session.className + ' \u2013 ' + session.subjectName }), h('div', { class: 'att-muted', text: session.sessionDate })),
            h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Back', onclick: renderHome })),
          h('div', { class: 'att-progress', text: marked + ' of ' + roster.length + ' marked' }),
          h('div', { class: 'att-row-actions' },
            h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: 'Mark all unmarked present', onclick: function () { markAll(uid, session, roster, profile, byStudent); } }))));

        UI.hooks.sheet.forEach(function (fn) {
          var slot = h('div'); host.appendChild(slot);
          Promise.resolve(fn(uid, session, roster, profile, records)).then(function (node) { if (node) slot.appendChild(node); }, function () {});
        });
        roster.forEach(function (st) {
          var cur = byStudent[st.studentId];
          function set(status) {
            var now = Core.schoolNow(profile.settings.timezone).datetime;
            var eff = status === 'present' ? Core.effectivePresentStatus(session.scheduledStart, now, profile.settings.late_after_minutes) : status;
            var extra = null;
            if (cur && cur.method === 'face') {             // a manual tap must not silently replace a face-recognition record
              if (!root.confirm('This student was recognised by face (' + Math.round((cur.confidence || 0) * 100) + '%).\n\nChange the mark manually?')) return;
              extra = { override: true };
            }
            A.store.setMark(uid, sessionUid, st, eff, now, extra).then(function () { renderSheet(uid, sessionUid); },
              function () { toast('Could not save this mark on the device.', 'red'); });
          }
          function btn(label, status, cls) {
            var on = cur && (cur.status === status || (status === 'present' && cur.status === 'late'));
            return h('button', { class: 'att-mark ' + cls + (on ? ' att-on' : ''), type: 'button', 'aria-pressed': String(!!on), text: label, onclick: function () { set(status); } });
          }
          var extra = h('select', { class: 'att-more', 'aria-label': 'More statuses for ' + st.firstName + ' ' + st.surname,
            onchange: function (e) { if (e.target.value) set(e.target.value); } },
            h('option', { value: '', text: cur && cur.status === 'late' ? 'Late' : (cur && cur.status === 'excused' ? 'Excused' : '\u22ef') }),
            h('option', { value: 'late', text: 'Late' }), h('option', { value: 'excused', text: 'Excused' }));
          host.appendChild(h('div', { class: 'att-student' },
            h('div', { class: 'att-name' }, h('strong', { text: st.surname + ', ' + st.firstName }), h('div', { class: 'att-muted', text: st.admissionNumber }),
              cur && cur.method === 'face' ? h('span', { class: 'att-chip att-green', text: 'Face ' + Math.round((cur.confidence || 0) * 100) + '%' }) : null),
            h('div', { class: 'att-marks' }, btn('Present', 'present', 'att-p'), btn('Absent', 'absent', 'att-a'), extra)));
        });

        host.appendChild(h('div', { class: 'att-card att-sticky' },
          h('button', { class: 'att-btn att-btn-big', type: 'button', text: 'Save attendance', onclick: function () { save(uid, session, roster, profile, records); } }),
          h('p', { class: 'att-muted', text: 'Marks are saved on this device as you tap. Saving locks the sheet and queues it to sync.' })));
      });
    });
  }

  function markAll(uid, session, roster, profile, byStudent) {
    var now = Core.schoolNow(profile.settings.timezone).datetime;
    var status = Core.effectivePresentStatus(session.scheduledStart, now, profile.settings.late_after_minutes);
    var chain = Promise.resolve();
    roster.forEach(function (st) { if (!byStudent[st.studentId]) chain = chain.then(function () { return A.store.setMark(uid, session.clientUid, st, status, now); }); });
    chain.then(function () { renderSheet(uid, session.clientUid); }, function () { toast('Could not save on the device.', 'red'); });
  }

  function save(uid, session, roster, profile, records) {
    var unmarked = roster.length - records.length;
    var msg = unmarked > 0 ? unmarked + ' student(s) are not marked and will be recorded as ABSENT.\n\nSave attendance?' : 'Save attendance?';
    if (!root.confirm(msg)) return;
    var closedAt = Core.schoolNow(profile.settings.timezone).datetime;
    A.store.finalizeSession(uid, session.clientUid, roster, closedAt).then(function (res) {
      // This message is shown only after the local transaction has committed.
      toast('Saved on this device (' + res.recordCount + ' students). It will sync automatically.', 'green');
      A.requestBackgroundSync();
      A.refresh();
      showSession(uid, session.clientUid);
      A.syncNow(false);
    }, function (e) {
      toast(e && e.code === 'storage_full' ? 'This device is almost out of storage. Nothing was saved. Free some space and try again.'
        : 'Saving failed. Nothing was recorded as saved. Please try again.', 'red');
    });
  }

  /* --------------------------------------------------------- detail view */
  function showSession(uid, sessionUid) {
    view = { name: 'session', uid: uid, sessionUid: sessionUid };
    return Promise.all([A.store.getSession(sessionUid), A.store.listRecords(sessionUid)]).then(function (r) {
      var s = r[0], recs = r[1];
      if (!s) return renderHome();
      clear();
      var present = recs.filter(function (x) { return x.status === 'present' || x.status === 'late'; }).length;
      host.appendChild(h('div', { class: 'att-card' },
        h('div', { class: 'att-between' }, h('div', null, h('strong', { text: s.className + ' \u2013 ' + s.subjectName }), h('div', { class: 'att-muted', text: s.sessionDate })),
          h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Back', onclick: renderHome })),
        chip(s),
        h('p', { text: present + ' present / late, ' + (recs.length - present) + ' absent or excused' }),
        s.syncStatus === 'failed' ? h('p', { class: 'att-muted', text: 'Next automatic retry: ' + fmt(s.nextRetryAt) + ' (reason: ' + s.lastError + ')' }) : null,
        s.syncStatus === 'rejected' ? h('p', { class: 'att-bad', text: 'The server did not accept this sheet. It is kept here for review; contact an administrator.' }) : null,
        s.status === 'draft' ? h('button', { class: 'att-btn', type: 'button', text: 'Continue marking', onclick: function () { renderSheet(uid, sessionUid); } }) : null,
        (s.syncStatus === 'failed' || s.syncStatus === 'pending') ? h('button', { class: 'att-btn', type: 'button', text: 'Sync now', onclick: function () { A.syncNow(true).then(function () { showSession(uid, sessionUid); }); } }) : null));
      recs.sort(function (a, b) { return a.studentName.localeCompare(b.studentName); }).forEach(function (x) {
        host.appendChild(h('div', { class: 'att-student' }, h('div', { class: 'att-name' }, h('strong', { text: x.studentName }), h('div', { class: 'att-muted', text: x.admissionNumber }), x.method === 'face' ? h('span', { class: 'att-chip att-green', text: 'Face ' + Math.round((x.confidence || 0) * 100) + '%' }) : null),
          h('div', { class: 'att-marks' }, h('span', { class: 'att-chip ' + (x.status === 'absent' ? 'att-red' : 'att-green'), text: x.status.charAt(0).toUpperCase() + x.status.slice(1) }),
            x.syncStatus === 'conflict' ? h('span', { class: 'att-chip att-orange', text: 'Conflict \u2013 under review' }) : null,
            x.syncStatus === 'rejected' ? h('span', { class: 'att-chip att-red', text: 'Rejected' }) : null)));
      });
    });
  }

  UI.renderSheet = renderSheet; UI.showSession = showSession;

  /* Re-render the home list when sync state changes (not while marking). */
  A.on('refreshed', function () { if (view.name === 'home') renderHome(); else if (view.name === 'session') showSession(view.uid, view.sessionUid); });
  A.ready.then(function () {
    // First screen after the connection check finishes. Keep it quick; the status bar updates itself.
    renderHome();
  });
})(typeof self !== 'undefined' ? self : this);
