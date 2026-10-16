/* Screens for offline face recognition (Stage 2) and offline face enrollment (Stage 3).
 * Plugs into att-app.js through AttUI hooks. All text from storage/server goes through textContent. */
(function (root) {
  'use strict';
  var UI = root.AttUI, A = root.Att, F = root.AttFace, Cam = root.AttCamera, E = root.AttFaceEngine, Core = root.AttCore, doc = root.document;
  if (!UI || !A || !F || !Cam || !E) return;
  var h = UI.h, toast = UI.toast, fmt = UI.fmt;

  /* ------------------------------------------------------------- helpers */
  function modal(title, nodes, buttons) {
    var overlay = h('div', { class: 'att-overlay', role: 'dialog', 'aria-modal': 'true', 'aria-label': title });
    var box = h('div', { class: 'att-modal' }, h('h3', { text: title }));
    nodes.forEach(function (n) { box.appendChild(n); });
    var row = h('div', { class: 'att-row-actions' });
    var api = { el: box, close: function () { overlay.remove(); } };
    buttons.forEach(function (b) {
      row.appendChild(h('button', { class: 'att-btn' + (b.outline ? ' att-btn-outline' : ''), type: 'button', text: b.label, onclick: function () { b.onclick(api); } }));
    });
    box.appendChild(row); overlay.appendChild(box); doc.body.appendChild(overlay);
    return api;
  }
  function field(label, type, attrs) {
    var input = h('input', Object.assign({ type: type, class: 'att-input', 'aria-label': label, autocomplete: 'off' }, attrs || {}));
    return { node: h('label', { class: 'att-field' }, h('span', { text: label }), input), input: input };
  }
  var PIN_MSG = { too_short: 'Use at least 6 characters.', too_weak: 'That PIN is too easy to guess. Choose another.' };

  /* ------------------------------------------------------------ the vault */
  function askPin(uid, mode) {
    return new Promise(function (resolve) {
      var err = h('p', { class: 'att-bad', role: 'alert' });
      var pin = field(mode === 'create' ? 'Choose a PIN or passphrase (6+ characters)' : 'Face data PIN', 'password');
      var confirmField = mode === 'create' ? field('Repeat it', 'password') : null;
      var nodes = [];
      if (mode === 'create') {
        nodes.push(h('p', { text: 'This PIN locks the face data stored on this phone. It is never sent to the server and cannot be recovered. ' +
          'If you forget it, the face data on this phone must be erased and downloaded again, and any face enrollments that have not uploaded yet would be lost.' }));
      } else nodes.push(h('p', { text: 'Enter your PIN to unlock face data on this phone.' }));
      nodes.push(pin.node); if (confirmField) nodes.push(confirmField.node); nodes.push(err);
      var buttons = [{ label: mode === 'create' ? 'Save PIN' : 'Unlock', onclick: submit }];
      if (mode === 'unlock') buttons.push({ label: 'Forgot PIN', outline: true, onclick: function (m) { m.close(); forgot(uid).then(function (r) { resolve(r); }); } });
      buttons.push({ label: 'Cancel', outline: true, onclick: function (m) { m.close(); resolve(false); } });
      var m = modal(mode === 'create' ? 'Protect face data' : 'Unlock face data', nodes, buttons);
      function submit() {
        err.textContent = '';
        if (mode === 'create') {
          if (pin.input.value !== confirmField.input.value) { err.textContent = 'The two entries do not match.'; return; }
          A.faces.createVault(uid, pin.input.value).then(function () { m.close(); resolve(true); },
            function (e) { err.textContent = PIN_MSG[e.code] || 'Could not create the PIN.'; });
        } else {
          A.faces.unlock(uid, pin.input.value).then(function () { m.close(); resolve(true); }, function (e) {
            if (e.message === 'too_many_attempts') err.textContent = 'Too many wrong attempts. Try again at ' + new Date(e.retryAt).toLocaleTimeString() + '.';
            else err.textContent = 'Wrong PIN.' + (typeof e.attemptsLeft === 'number' ? ' ' + e.attemptsLeft + ' quick tries left.' : '');
          });
        }
      }
    });
  }
  function forgot(uid) {
    return A.faces.resetVault(uid).then(function () {
      return root.confirm('Reset face data on this phone?\n\nThis erases the downloaded face data. You will need to download it again while online.') ? A.faces.resetVault(uid, { force: true }).then(function () { toast('Face data erased. Set a new PIN to continue.', 'blue'); return ensureUnlocked(uid); }) : false;
    }, function (e) {
      if (e.message !== 'pending_enrollments') throw e;
      if (!root.confirm(e.count + ' face enrollment(s) on this phone have NOT uploaded yet. Without the PIN they cannot be read or uploaded.\n\n' +
        'Cancel and try your PIN again if you can. Erase them anyway?')) return false;
      return A.faces.resetVault(uid, { force: true }).then(function () { return ensureUnlocked(uid); });
    });
  }
  function ensureUnlocked(uid) {
    return A.faces.vaultState(uid).then(function (s) {
      if (s === 'unlocked') return true;
      return askPin(uid, s === 'none' ? 'create' : 'unlock');
    });
  }
  A.promptUnlock = function () {
    return A.currentUserId().then(function (uid) { return uid ? ensureUnlocked(uid) : false; }).then(function (ok) { if (ok) return A.syncNow(false); });
  };

  /* ----------------------------------------------------------- the engine */
  /* The outcome (including "not installed") is cached for a minute and shared by concurrent callers, so a
     screen refresh can never trigger a new model check (which would refresh the screen again...). A new
     check happens when connectivity changes, after a minute, or when the user taps "Check again". */
  var engineP = null, engineAt = 0, engineKey = '';
  function ensureEngine(face, force) {
    var key = face.engine + '@' + face.engine_version + ':' + !!A.monitor.state.reachable, st = A.engineState;
    if (!force) {
      if (engineP && engineKey === key) return engineP;
      if (st && st.status === 'ready' && st.engine.version === String(face.engine_version)) return Promise.resolve(st);
      if (st && st.status !== 'loading' && engineKey === key && Date.now() - engineAt < 60000) return Promise.resolve(st);
    }
    engineKey = key; engineAt = Date.now();
    A.engineState = { status: 'loading', message: E.MESSAGES.loading };
    engineP = E.load(face, { online: A.monitor.state.reachable }).then(function (s) {
      A.engineState = s; engineP = null; engineAt = Date.now(); A.refresh(); return s;
    });
    return engineP;
  }

  /* ------------------------------------------------- face data (teachers) */
  function downloadFaceData(uid, classId) {
    var m = A.monitor.state;
    if (!m.reachable) { toast("You're offline. Connect to download face data.", 'red'); return Promise.resolve(false); }
    return ensureUnlocked(uid).then(function (ok) {
      if (!ok) return false;
      return A.api.faceTemplates(classId).then(function (r) {
        if (r.status === 200 && r.body && r.body.ok) {
          return A.faces.saveClassTemplates(uid, classId, r.body).then(function (meta) {
            toast(meta.count + ' face template(s) saved on this phone' + (meta.missing.length ? '; ' + meta.missing.length + ' student(s) have no face data' : '') + '.', 'green');
            return true;
          }, function (e) { toast(e.code === 'storage_full' ? 'This phone is almost out of storage. Face data was not saved.' : 'Could not save face data.', 'red'); return false; });
        }
        toast(r.status === 409 ? 'Offline face recognition is not enabled on the server.' : (r.status === 403 ? 'You are not assigned to this class.' : 'Could not download face data.'), 'red');
        return false;
      }, function () { toast('Could not reach the server. Try again.', 'red'); return false; });
    });
  }

  UI.hooks.home.push(function (uid, assignments, profile) {
    var face = profile && profile.face;
    var teaching = assignments.length > 0;
    if (!face || !face.offline_supported) {
      return teaching ? h('p', { class: 'att-muted', text: 'Offline face recognition is not enabled on the server. Manual attendance works offline.' }) : null;
    }
    var wrap = h('div');
    return ensureEngine(face).then(function (eng) {
      if (teaching) {
        var card = h('div', { class: 'att-card' }, h('h3', { text: 'Face recognition' }),
          h('p', { class: 'att-muted', text: eng.message }));
        if (eng.status !== 'ready') {
          card.appendChild(h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: 'Check again',
            onclick: function () { ensureEngine(face, true).then(renderHomeSoon); } }));
        }
        if (eng.status === 'ready' && A.monitor.state.reachable) {
          card.appendChild(h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: 'Download model for offline use',
            onclick: function (e) {
              e.target.disabled = true; e.target.textContent = 'Downloading\u2026';
              E.prefetch(eng, function (d, n) { e.target.textContent = 'Downloading ' + d + '/' + n; }).then(function (r) {
                toast(r.ok ? 'Face model is stored on this phone for offline use.' : 'Could not download the whole model. Try again.', r.ok ? 'green' : 'red'); renderHomeSoon();
              });
            } }));
        }
        var seen = {};
        var rows = assignments.filter(function (a) { if (seen[a.classId]) return false; seen[a.classId] = true; return true; });
        return Promise.all(rows.map(function (a) { return A.faces.classStatus(uid, a.classId).then(function (s) { return { a: a, s: s }; }); })).then(function (list) {
          list.forEach(function (x) {
            var line = x.s ? ('Face data for ' + x.s.count + ' student(s)' + (x.s.missing.length ? ', ' + x.s.missing.length + ' without' : '') + ' \u00b7 downloaded ' + fmt(x.s.at))
              : 'Face data not downloaded';
            card.appendChild(h('div', { class: 'att-between att-sub' }, h('div', null, h('strong', { text: x.a.className }), h('div', { class: 'att-muted', text: line })),
              h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: x.s ? 'Refresh' : 'Download',
                onclick: function () { downloadFaceData(uid, x.a.classId).then(renderHomeSoon); } })));
          });
          wrap.appendChild(card);
        });
      }
    }).then(function () {
      return enrollCard(uid, profile, face).then(function (n) { if (n) wrap.appendChild(n); return wrap; });
    });
  });
  function renderHomeSoon() { if (UI.renderHome) UI.renderHome(); }

  /* ------------------------------------------------------- sheet: scan faces */
  UI.hooks.sheet.push(function (uid, session, roster, profile, records) {
    var face = profile && profile.face;
    if (!face || !face.offline_supported) return null;
    return Promise.all([A.faces.classStatus(uid, session.classId), ensureEngine(face)]).then(function (r) {
      var status = r[0], eng = r[1];
      var card = h('div', { class: 'att-card' });
      if (!status) {
        card.appendChild(h('p', { class: 'att-muted', text: 'Face data is not downloaded for this class. Use manual attendance, or download it while online.' }));
        if (A.monitor.state.reachable) card.appendChild(h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: 'Download face data',
          onclick: function () { downloadFaceData(uid, session.classId).then(function (ok) { if (ok) UI.renderSheet(uid, session.clientUid); }); } }));
        return card;
      }
      if (eng.status !== 'ready') { card.appendChild(h('p', { class: 'att-muted', text: eng.message + ' Use manual attendance.' })); return card; }
      card.appendChild(h('button', { class: 'att-btn', type: 'button', text: 'Scan faces',
        onclick: function () { startScan(uid, session, roster, profile, eng); } }));
      var without = roster.filter(function (s) { return status.missing.indexOf(s.studentId) >= 0; });
      if (without.length) card.appendChild(h('p', { class: 'att-muted', text: without.length + ' student(s) have no face data and must be marked manually: ' +
        without.slice(0, 6).map(function (s) { return s.firstName + ' ' + s.surname; }).join(', ') + (without.length > 6 ? '\u2026' : '') }));
      card.appendChild(h('p', { class: 'att-muted', text: 'Manual attendance is always available as an alternative.' }));
      return card;
    });
  });

  function startScan(uid, session, roster, profile, eng) {
    ensureUnlocked(uid).then(function (ok) {
      if (!ok) return;
      return A.faces.loadClassTemplates(uid, session.classId).then(function (pkg) { renderScan(uid, session, roster, profile, eng, pkg); },
        function () { toast('Could not read face data.', 'red'); });
    });
  }

  var OUTCOME_STYLE = { marked: 'att-green', verifying: 'att-blue', cooldown: 'att-blue', already_marked: 'att-blue' };
  function renderScan(uid, session, roster, profile, eng, pkg) {
    UI.setView({ name: 'scan' }); UI.clear();
    var host = UI.host, cam = new Cam(), running = true, busy = false, timer = null;
    var byId = {}; roster.forEach(function (s) { byId[s.studentId] = s; });
    var marked = {};
    var video = h('video', { class: 'att-video', autoplay: '', playsinline: '', muted: '' });
    var status = h('div', { class: 'att-scan-status att-blue', role: 'status', 'aria-live': 'polite', text: 'Starting camera\u2026' });
    var counter = h('div', { class: 'att-progress' });
    var review = h('div', { class: 'att-card' });
    var scanner = new F.FaceScanner({ engine: eng.engine, templates: pkg.templates, threshold: pkg.meta.threshold, margin: pkg.meta.margin,
      isMarked: function (id) { return !!marked[id]; }, nameOf: function (id) { return byId[id] ? byId[id].firstName + ' ' + byId[id].surname : 'Student'; } });

    function stop() { running = false; clearTimeout(timer); cam.stop(); unsub(); }
    var unsub = Cam.autoRelease(cam, function () { running = false; clearTimeout(timer); showStatus('Camera stopped because the page was hidden.', 'att-orange'); });
    function showStatus(msg, cls) { status.className = 'att-scan-status ' + (cls || 'att-orange'); status.textContent = msg; }
    function updateCounter() { counter.textContent = Object.keys(marked).length + ' of ' + roster.length + ' marked'; }
    function updateReview() {
      var st = scanner.summary().stats, parts = [];
      [['not_recognized', 'not recognized'], ['low_confidence', 'low confidence'], ['ambiguous', 'not certain'], ['liveness_failed', 'live face not confirmed']].forEach(function (p) {
        if (st[p[0]]) parts.push(st[p[0]] + ' ' + p[1]);
      });
      review.textContent = '';
      review.appendChild(h('strong', { text: 'Needs manual attention' }));
      review.appendChild(h('p', { class: 'att-muted', text: parts.length ? 'Face attempts that were not accepted: ' + parts.join(', ') + '. These students were NOT marked. Use "Mark manually" for them.' : 'No unmatched attempts so far.' }));
      var missing = roster.filter(function (s) { return pkg.meta.missing.indexOf(s.studentId) >= 0 && !marked[s.studentId]; });
      if (missing.length) review.appendChild(h('p', { class: 'att-muted', text: 'No face data (mark manually): ' + missing.map(function (s) { return s.firstName + ' ' + s.surname; }).join(', ') }));
    }
    function recordMark(out) {
      var st = byId[out.studentId];
      var now = Core.schoolNow(profile.settings.timezone).datetime;
      var eff = Core.effectivePresentStatus(session.scheduledStart, now, profile.settings.late_after_minutes);
      return A.store.setMark(uid, session.clientUid, st, eff, now, { method: 'face', confidence: Math.round(out.score * 1000) / 1000,
        engine: pkg.meta.engine, engineVersion: pkg.meta.engineVersion, templateUid: out.templateUid }).then(function () {
        marked[out.studentId] = true; updateCounter(); updateReview();
      }, function (e) { if (e.message !== 'face_record_protected') toast('Could not save this mark on the device.', 'red'); else marked[out.studentId] = true; });
    }
    function tick() {
      if (!running) return;
      var frame = cam.grab();
      if (busy || !frame) { timer = setTimeout(tick, 250); return; }
      busy = true;
      scanner.processFrame(frame).then(function (out) {
        busy = false;
        var msg = out.message;
        if (out.code === 'marked') msg = out.message.replace('marked present.', 'marked present (' + Math.round(out.score * 100) + '%).');
        if (out.code !== 'cooldown') showStatus(msg, OUTCOME_STYLE[out.code] || 'att-orange');
        updateReview();
        return out.mark ? recordMark(out) : null;
      }).then(function () { if (running) timer = setTimeout(tick, 250); }, function () { busy = false; if (running) timer = setTimeout(tick, 500); });
    }
    function begin(deviceId) {
      cam.start(video, { deviceId: deviceId }).then(function () { showStatus('Ask each student to look at the camera, one at a time.', 'att-blue'); running = true; tick(); },
        function (e) { showStatus(Cam.messages[e.code] || Cam.messages.camera_error, 'att-red'); });
    }
    A.store.listRecords(session.clientUid).then(function (recs) {
      recs.forEach(function (r) { marked[r.studentId] = true; });
      updateCounter(); updateReview();
    });
    host.appendChild(h('div', { class: 'att-card' },
      h('div', { class: 'att-between' }, h('div', null, h('strong', { text: session.className + ' \u2013 ' + session.subjectName }), h('div', { class: 'att-muted', text: 'Face attendance' })),
        h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Stop & mark manually', onclick: function () { stop(); UI.renderSheet(uid, session.clientUid); } })),
      counter));
    host.appendChild(h('div', { class: 'att-video-wrap' }, video));
    host.appendChild(status);
    host.appendChild(h('div', { class: 'att-row-actions' },
      h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: 'Switch camera', onclick: function () {
        cam.devices().then(function (list) {
          if (list.length < 2) { toast('No other camera found.', 'blue'); return; }
          var i = list.findIndex(function (d) { return d.deviceId === cam.deviceId; });
          clearTimeout(timer); begin(list[(i + 1) % list.length].deviceId);
        });
      } }),
      h('button', { class: 'att-btn att-btn-outline att-btn-small', type: 'button', text: 'Retry camera', onclick: function () { clearTimeout(timer); begin(); } })));
    host.appendChild(review);
    host.appendChild(h('p', { class: 'att-muted', text: 'Face matching is not perfect. A student is only marked when the match is clearly confident; anything uncertain is left for you to mark manually. ' +
      'Face data stays on this phone and no photographs are saved.' }));
    begin();
  }

  /* ------------------------------------------------------------ enrollment */
  function enrollCard(uid, profile, face) {
    if (!A.monitor.state.enrollCapable && (!profile || profile.role !== 'super_admin')) return Promise.resolve(null);
    return Promise.all([A.faces.getEnrollRoster(uid), A.faces.listEnrollments(uid), A.faces.vaultState(uid)]).then(function (r) {
      var roster = r[0], list = r[1];
      var card = h('div', { class: 'att-card' }, h('h3', { text: 'Face enrollment' }),
        h('p', { class: 'att-muted', text: roster ? roster.students.length + ' students available \u00b7 list downloaded ' + fmt(roster.at)
          : 'The student list is not downloaded yet. Connect to the internet once to download it.' }),
        h('div', { class: 'att-row-actions' },
          roster ? h('button', { class: 'att-btn', type: 'button', text: 'Enroll a student', onclick: function () { renderPick(uid, profile, face); } }) : null,
          h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Refresh list', onclick: function () {
            A.prepareEnroll(true).then(function (x) { toast(x.ok ? 'Student list updated.' : (x.reason === 'offline' ? "You're offline." : 'Could not update the list.'), x.ok ? 'green' : 'red'); renderHomeSoon(); });
          } }),
          r[2] !== 'unlocked' && r[2] !== 'none' ? h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Unlock face data', onclick: function () { ensureUnlocked(uid).then(function (ok) { if (ok) { A.syncNow(false); renderHomeSoon(); } }); } }) : null));
      var LABEL = { pending: ['Waiting to upload', 'att-orange'], syncing: ['Uploading\u2026', 'att-orange'], failed: ['Upload failed \u2013 will retry', 'att-red'],
        synced: ['Uploaded', 'att-green'], conflict: ['Not applied \u2013 student already enrolled (admin review)', 'att-orange'], rejected: ['Rejected by server', 'att-red'] };
      list.slice(0, 15).forEach(function (e) {
        var l = LABEL[e.syncStatus] || [e.syncStatus, 'att-blue'];
        card.appendChild(h('div', { class: 'att-between att-sub' }, h('div', null, h('strong', { text: e.studentName }), h('div', { class: 'att-muted', text: e.admissionNumber + ' \u00b7 ' + fmt(e.createdAt) })),
          h('span', { class: 'att-chip ' + l[1], text: l[0] })));
      });
      return card;
    });
  }

  function renderPick(uid, profile, face) {
    UI.setView({ name: 'enroll-pick' }); UI.clear();
    Promise.all([A.faces.getEnrollRoster(uid), A.faces.listEnrollments(uid)]).then(function (r) {
      var students = r[0].students, local = {};
      r[1].forEach(function (e) { if (['pending', 'syncing', 'failed'].indexOf(e.syncStatus) >= 0) local[e.studentId] = true; });
      var host = UI.host, listEl = h('div');
      var q = h('input', { class: 'att-input', type: 'search', placeholder: 'Search name or admission number', 'aria-label': 'Search students' });
      function draw() {
        var t = q.value.trim().toLowerCase();
        var hits = students.filter(function (s) { return !t || (s.first_name + ' ' + s.surname + ' ' + s.admission_number).toLowerCase().indexOf(t) >= 0; }).slice(0, 30);
        listEl.textContent = '';
        if (!hits.length) listEl.appendChild(h('p', { class: 'att-muted', text: 'No student found.' }));
        hits.forEach(function (s) {
          var badges = [];
          if (local[s.id]) badges.push(h('span', { class: 'att-chip att-orange', text: 'Waiting to upload' }));
          if (s.face_offline_enrolled) badges.push(h('span', { class: 'att-chip att-green', text: 'Enrolled' }));
          else if (s.face_lbph_enrolled) badges.push(h('span', { class: 'att-chip att-blue', text: 'Server enrollment only' }));
          listEl.appendChild(h('div', { class: 'att-card att-between att-click', onclick: function () { renderCapture(uid, profile, face, s, !!local[s.id]); } },
            h('div', null, h('strong', { text: s.surname + ', ' + s.first_name }), h('div', { class: 'att-muted', text: s.admission_number + ' \u00b7 ' + (s.class_name || 'No class') })),
            h('div', null, badges)));
        });
      }
      q.addEventListener('input', draw);
      host.appendChild(h('div', { class: 'att-card att-between' }, h('strong', { text: 'Choose a student' }), h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Back', onclick: renderHomeSoon })));
      host.appendChild(q); host.appendChild(listEl); draw();
    });
  }

  function renderCapture(uid, profile, face, student, hasPending) {
    UI.setView({ name: 'enroll-capture' }); UI.clear();
    var host = UI.host, cam = new Cam(), running = false, busy = false, timer = null, samples = [], qualities = [], lastAt = 0;
    var needed = Math.max(face.min_samples || 5, 5);
    var consent = h('input', { type: 'checkbox', id: 'attConsent' });
    var note = field('Who gave consent (for example: guardian name)', 'text', { maxlength: '200' });
    var replace = h('input', { type: 'checkbox', id: 'attReplace' });
    var msg = h('p', { class: 'att-bad', role: 'alert' });
    var video = h('video', { class: 'att-video', autoplay: '', playsinline: '', muted: '' });
    var hint = h('div', { class: 'att-scan-status att-blue', role: 'status', 'aria-live': 'polite', text: '' });
    var dots = h('div', { class: 'att-dots' });
    var unsub = null;

    function stop() { running = false; clearTimeout(timer); cam.stop(); if (unsub) { unsub(); unsub = null; } }
    function drawDots() { dots.textContent = ''; for (var i = 0; i < needed; i++) dots.appendChild(h('span', { class: 'att-dot-s' + (i < samples.length ? ' att-on' : '') })); }
    function back() { stop(); renderHomeSoon(); }

    host.appendChild(h('div', { class: 'att-card' },
      h('div', { class: 'att-between' }, h('div', null, h('strong', { text: student.surname + ', ' + student.first_name }), h('div', { class: 'att-muted', text: student.admission_number })),
        h('button', { class: 'att-btn att-btn-outline', type: 'button', text: 'Cancel', onclick: back })),
      h('label', { class: 'att-check', for: 'attConsent' }, consent, h('span', { text: 'I confirm the student\u2019s guardian has consented to face enrollment for attendance, and that manual attendance remains available as an alternative.' })),
      note.node,
      student.face_offline_enrolled ? h('label', { class: 'att-check', for: 'attReplace' }, replace, h('span', { text: 'Replace this student\u2019s existing face enrollment' })) : null,
      hasPending ? h('p', { class: 'att-muted', text: 'An enrollment for this student is already waiting to upload. Saving a new one replaces it.' }) : null,
      msg,
      h('button', { class: 'att-btn att-btn-big', type: 'button', text: 'Start camera', onclick: start })));
    host.appendChild(h('div', { class: 'att-video-wrap' }, video)); host.appendChild(hint); host.appendChild(dots); drawDots();

    function start() {
      msg.textContent = '';
      if (!consent.checked) { msg.textContent = 'Consent must be confirmed before enrolling a face.'; return; }
      if (!note.input.value.trim()) { msg.textContent = 'Enter who gave consent.'; return; }
      ensureUnlocked(uid).then(function (ok) {
        if (!ok) return;
        return ensureEngine(face).then(function (eng) {
          if (eng.status !== 'ready') { msg.textContent = eng.message; return; }
          samples = []; qualities = []; drawDots();
          cam.stop();
          return cam.start(video).then(function () {
            unsub = Cam.autoRelease(cam, function () { running = false; hint.textContent = 'Camera stopped because the page was hidden.'; hint.className = 'att-scan-status att-orange'; });
            running = true; hint.textContent = 'Look at the camera. Turn your head slightly between captures.'; hint.className = 'att-scan-status att-blue';
            loop(eng.engine);
          }, function (e) { msg.textContent = Cam.messages[e.code] || Cam.messages.camera_error; });
        });
      });
    }

    function loop(engine) {
      if (!running) return;
      var frame = cam.grab();
      if (busy || !frame) { timer = setTimeout(function () { loop(engine); }, 250); return; }
      busy = true;
      Promise.resolve(engine.detect(frame)).then(function (dets) {
        dets = dets || [];
        var m = { faceCount: dets.length, box: dets[0] && dets[0].box, frameW: frame.width, frameH: frame.height, brightness: 0, sharpness: 0 };
        if (dets.length === 1) { var fm = F.frameMetrics(frame.data, frame.width, frame.height, dets[0].box); m.brightness = fm.brightness; m.sharpness = fm.sharpness; }
        var q = F.checkQuality(m);
        if (!q.ok) { hint.textContent = F.MESSAGES[q.code]; hint.className = 'att-scan-status att-orange'; return null; }
        if (Date.now() - lastAt < 700) return null;
        return Promise.resolve(engine.embed(frame, dets[0])).then(function (emb) {
          var n = F.normalize(emb);
          if (samples.length && F.cosine(samples[0], n) < 0.3) { hint.textContent = 'A different face appeared. Only the student being enrolled should be in view.'; hint.className = 'att-scan-status att-orange'; return; }
          samples.push(n); lastAt = Date.now(); drawDots();
          hint.textContent = 'Captured ' + samples.length + ' of ' + needed + '.'; hint.className = 'att-scan-status att-green';
          if (samples.length >= needed) { return finish(); }
        });
      }).then(function () { busy = false; if (running) timer = setTimeout(function () { loop(engine); }, 250); },
        function () { busy = false; hint.textContent = 'The face model had a problem. Try again.'; hint.className = 'att-scan-status att-red'; running = false; cam.stop(); });
    }

    function finish() {
      running = false; cam.stop();                                    // camera off as soon as capture is complete
      var t = F.buildTemplate(samples, { minSamples: needed });
      if (!t.ok) {
        hint.textContent = t.code === 'inconsistent' ? 'The captures did not look like the same face. Please retry with good light, facing the camera.' : 'Not enough good captures. Please retry.';
        hint.className = 'att-scan-status att-red'; samples = []; drawDots(); return;
      }
      return save(t, false);
    }
    function save(t, replacePending) {
      return A.faces.saveEnrollment(uid, { studentId: student.id, studentName: student.first_name + ' ' + student.surname, admissionNumber: student.admission_number,
        engine: face.engine, engineVersion: face.engine_version, dim: face.dim, vector: t.vector, sampleCount: t.sampleCount, quality: Math.round(t.consistency.mean * 100) / 100,
        consentConfirmed: consent.checked, consentNote: note.input.value.trim(), replace: !!replace.checked, replacePending: replacePending,
        capturedAt: Core.schoolNow(profile.settings.timezone).datetime }).then(function () {
        // Shown only after the encrypted record was committed to the device.
        toast('Enrollment saved on this device. It will upload automatically when the server is reachable.', 'green');
        A.refresh(); A.syncNow(false); renderHomeSoon();
      }, function (e) {
        if (e.code === 'enrollment_exists') { if (root.confirm('An enrollment for this student is already waiting to upload. Replace it?')) return save(t, true); return; }
        if (e.code === 'storage_full') { hint.textContent = 'This phone is almost out of storage. Nothing was saved.'; hint.className = 'att-scan-status att-red'; return; }
        if (e.code === 'vault_locked') { return ensureUnlocked(uid).then(function (ok) { if (ok) return save(t, replacePending); }); }
        hint.textContent = 'Saving failed. Nothing was recorded as saved. Please try again.'; hint.className = 'att-scan-status att-red';
      });
    }
  }
})(typeof self !== 'undefined' ? self : this);
