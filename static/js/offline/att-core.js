/* Offline attendance - core logic (no DOM, no direct IndexedDB access).
 *
 * Everything here talks to storage through a small "backend" interface so the
 * very same code runs on IndexedDB in the browser (att-idb.js) and on an
 * in-memory backend in the automated tests:
 *
 *   backend.transaction(storeNames, 'r' | 'rw', async (tx) => result)
 *     tx.get(store, key) / tx.put(store, obj) / tx.del(store, key)
 *     tx.all(store) / tx.byIndex(store, indexName, value)
 *   The promise resolves only AFTER the transaction has committed.
 *   Inside a transaction callback, await ONLY tx.* calls (IndexedDB closes a
 *   transaction that is waiting on anything else).
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.AttCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ---------------------------------------------------------- messages */
  var MESSAGES = {
    online: "You're online. Your connection to the server is working.",
    offline: "You're offline. You can continue working. Your records will synchronize when your connection returns.",
    syncing: 'Synchronizing your records. Please keep the application open until synchronization is complete.',
    synced: 'Synchronization completed successfully. All pending records are saved online.',
    error: 'Synchronization failed. Your local records are safe. We will retry automatically.',
    auth_required: 'Sign-in required. Your records are safe on this device. Sign in again to synchronize them.',
    checking: 'Checking your connection to the server\u2026'
  };

  var SCHEMA = {
    meta: { keyPath: 'key', indexes: {} },
    profiles: { keyPath: 'userId', indexes: {} },
    assignments: { keyPath: 'key', indexes: { userId: 'userId' } },
    students: { keyPath: 'key', indexes: { userClass: 'userClass', userId: 'userId' } },
    sessions: { keyPath: 'clientUid', indexes: { userId: 'userId', natural: 'natural' } },
    records: { keyPath: 'clientUid', indexes: { sessionUid: 'sessionUid', userId: 'userId' } },
    /* Stage 2/3 (added in DB version 2; existing stores are never touched) */
    faceTemplates: { keyPath: 'key', indexes: { userClass: 'userClass', userId: 'userId' } },
    enrollments: { keyPath: 'clientUid', indexes: { userId: 'userId' } }
  };

  /* ------------------------------------------------------------ helpers */
  function uuid() {
    var c = (typeof crypto !== 'undefined') ? crypto : null;
    if (c && c.randomUUID) return c.randomUUID();
    var b = new Uint8Array(16);
    if (c && c.getRandomValues) c.getRandomValues(b);
    else for (var i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }

  /* Exponential backoff with jitter: 5s, 10s, 20s ... capped at 15 minutes. */
  function backoffMs(retryCount, rnd) {
    var base = Math.min(5000 * Math.pow(2, Math.max(0, retryCount - 1)), 15 * 60 * 1000);
    var r = (rnd || Math.random)();
    return Math.round(base * (0.8 + 0.4 * r));
  }

  /* How long to wait before the next health probe (keeps traffic low). */
  function nextProbeDelay(s) {
    if (s.reachable === null) return 1500;
    if (s.reachable) return s.hidden ? 120000 : 45000;
    return Math.min(60000, 5000 * Math.pow(2, Math.min(s.failures || 0, 4)));
  }

  /* Parse "YYYY-MM-DDTHH:MM:SS" as plain wall-clock components (no timezone
     shifting), so school-time strings compare correctly on any device. */
  function parseNaive(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/.exec(s || '');
    if (!m) return NaN;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }

  /* Current school-local date/time from the SCHOOL timezone (not the device's),
     so two phones with different settings still agree on the school day. */
  function schoolNow(tz, when) {
    var d = when ? new Date(when) : new Date();
    var parts;
    try {
      parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: tz || 'Africa/Lagos', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
      }).formatToParts(d);
    } catch (e) {
      parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
      }).formatToParts(d);
    }
    var p = {};
    parts.forEach(function (x) { p[x.type] = x.value; });
    var date = p.year + '-' + p.month + '-' + p.day;
    return { date: date, datetime: date + 'T' + p.hour + ':' + p.minute + ':' + p.second };
  }

  /* Same Present/Late rule as the server's manual attendance. */
  function effectivePresentStatus(sessionStart, markedAt, lateAfterMinutes) {
    var minutes = (parseNaive(markedAt) - parseNaive(sessionStart)) / 60000;
    return minutes > (lateAfterMinutes || 15) ? 'late' : 'present';
  }

  function sanitizeError(code) {
    // Never keep server messages or stack traces in local storage.
    var allowed = ['network', 'timeout', 'server', 'rate_limited', 'forbidden', 'invalid', 'incomplete_response', 'csrf'];
    return allowed.indexOf(code) >= 0 ? code : 'server';
  }

  /* --------------------------------------- connection state (pure logic) */
  /* input: {reachable: true|false|null, authenticated, syncing, pendingRecords,
             failedSessions, rejectedSessions, recentlySynced, authRequiredPending} */
  function computeConnectionState(i) {
    var out;
    if (i.reachable === null || i.reachable === undefined) out = ['checking', 'grey'];
    else if (i.reachable === false) out = ['offline', 'blue'];
    else if (i.syncing) out = ['syncing', 'orange'];
    else if (i.authRequiredPending) out = ['auth_required', 'red'];
    else if (i.failedSessions > 0) out = ['error', 'red'];
    else if (i.recentlySynced && !i.pendingRecords && !i.failedSessions) out = ['synced', 'green'];
    else out = ['online', 'green'];
    return { code: out[0], color: out[1], message: MESSAGES[out[0]] };
  }

  /* ------------------------------------------------------ memory backend */
  function MemoryBackend() {
    this.data = {};
    for (var k in SCHEMA) this.data[k] = new Map();
    this.failNextCommit = false;      // test hook: simulate quota/commit failure
  }
  MemoryBackend.prototype.available = function () { return Promise.resolve(true); };
  MemoryBackend.prototype.transaction = function (names, mode, fn) {
    var self = this;
    var snapshot = null;
    if (mode === 'rw') {
      snapshot = {};
      for (var k in self.data) snapshot[k] = new Map(self.data[k]);
    }
    var tx = {
      get: function (s, key) { var v = self.data[s].get(key); return Promise.resolve(v === undefined ? undefined : JSON.parse(JSON.stringify(v))); },
      put: function (s, obj) { self.data[s].set(obj[SCHEMA[s].keyPath], JSON.parse(JSON.stringify(obj))); return Promise.resolve(); },
      del: function (s, key) { self.data[s].delete(key); return Promise.resolve(); },
      all: function (s) { return Promise.resolve(Array.from(self.data[s].values()).map(function (v) { return JSON.parse(JSON.stringify(v)); })); },
      byIndex: function (s, idx, value) {
        var field = SCHEMA[s].indexes[idx];
        return Promise.resolve(Array.from(self.data[s].values()).filter(function (v) { return v[field] === value; })
          .map(function (v) { return JSON.parse(JSON.stringify(v)); }));
      }
    };
    return Promise.resolve().then(function () { return fn(tx); }).then(function (res) {
      if (self.failNextCommit && mode === 'rw') { self.failNextCommit = false; throw new Error('QuotaExceededError'); }
      return res;
    }).catch(function (err) {
      if (snapshot) self.data = snapshot;      // roll back, like a real aborted transaction
      throw err;
    });
  };

  /* ---------------------------------------------------------------- Store */
  function Store(backend, opts) {
    opts = opts || {};
    this.b = backend;
    this.clock = opts.clock || function () { return Date.now(); };
    this.uuid = opts.uuid || uuid;
    this.estimator = opts.estimator || null;   // () => Promise<{usage, quota}>
  }
  var SP = Store.prototype;

  SP.available = function () { return this.b.available(); };

  SP.getMeta = function (key) {
    return this.b.transaction(['meta'], 'r', function (tx) { return tx.get('meta', key); })
      .then(function (r) { return r ? r.value : undefined; });
  };
  SP.setMeta = function (key, value) {
    return this.b.transaction(['meta'], 'rw', function (tx) { return tx.put('meta', { key: key, value: value }); });
  };
  SP.getDeviceUid = function () {
    var self = this;
    return this.getMeta('deviceUid').then(function (v) {
      if (v) return v;
      var fresh = self.uuid();
      return self.setMeta('deviceUid', fresh).then(function () { return fresh; });
    });
  };

  /* Replace the downloaded class data for ONE user. Saved attendance
     (sessions/records) is never touched by a refresh. */
  SP.saveBootstrap = function (b) {
    var self = this, uid = b.user.id, now = this.clock();
    return this.b.transaction(['profiles', 'assignments', 'students', 'meta'], 'rw', function (tx) {
      return tx.byIndex('assignments', 'userId', uid).then(function (old) {
        return Promise.all(old.map(function (a) { return tx.del('assignments', a.key); }));
      }).then(function () { return tx.byIndex('students', 'userId', uid); })
        .then(function (old) { return Promise.all(old.map(function (s) { return tx.del('students', s.key); })); })
        .then(function () {
          var puts = [];
          b.assignments.forEach(function (a) {
            puts.push(tx.put('assignments', {
              key: uid + ':' + a.id, userId: uid, assignmentId: a.id, classId: a.class_id, className: a.class_name,
              subjectId: a.subject_id, subjectName: a.subject_name, subjectCode: a.subject_code,
              termId: a.term_id, studentCount: a.student_count
            }));
          });
          b.classes.forEach(function (c) {
            c.students.forEach(function (s) {
              puts.push(tx.put('students', {
                key: uid + ':' + c.id + ':' + s.id, userId: uid, userClass: uid + ':' + c.id, classId: c.id,
                studentId: s.id, admissionNumber: s.admission_number, firstName: s.first_name,
                surname: s.surname, faceLbphEnrolled: !!s.face_lbph_enrolled, faceOfflineEnrolled: !!s.face_offline_enrolled
                /* Browser face templates live in their own encrypted store, never in this row. */
              }));
            });
          });
          return Promise.all(puts);
        })
        .then(function () {
          var graceMs = ((b.offline_policy && b.offline_policy.grace_hours) || 72) * 3600 * 1000;
          return tx.put('profiles', {
            userId: uid, name: b.user.name, role: b.user.role, hasTeacherProfile: !!b.has_teacher_profile,
            preparedAt: now, lastVerifiedAt: now, graceMs: graceMs, offlineExpiresAt: now + graceMs,
            locked: false, settings: b.settings, packageId: b.package_id, appVersion: b.app_version,
            faceOfflineSupported: !!(b.face && b.face.offline_supported), face: b.face || { offline_supported: false }
          });
        })
        .then(function () { return tx.put('meta', { key: 'lastUserId', value: uid }); });
    });
  };

  /* Accounts that only enrol faces (Super Administrator) have no classes: store just the offline profile. */
  SP.saveProfileOnly = function (b) {
    var now = this.clock(), uid = b.user.id, graceMs = ((b.offline_policy && b.offline_policy.grace_hours) || 72) * 3600 * 1000;
    return this.b.transaction(['profiles', 'meta'], 'rw', function (tx) {
      return tx.get('profiles', uid).then(function (old) {
        var p = Object.assign(old || {}, { userId: uid, name: b.user.name, role: b.user.role, hasTeacherProfile: !!(old && old.hasTeacherProfile),
          preparedAt: now, lastVerifiedAt: now, graceMs: graceMs, offlineExpiresAt: now + graceMs, locked: false,
          settings: Object.assign({}, old && old.settings, b.settings), appVersion: b.app_version, face: b.face || { offline_supported: false } });
        return tx.put('profiles', p);
      }).then(function () { return tx.put('meta', { key: 'lastUserId', value: uid }); });
    });
  };

  SP.getProfile = function (userId) {
    return this.b.transaction(['profiles'], 'r', function (tx) { return tx.get('profiles', userId); });
  };
  SP.getLastUserId = function () { return this.getMeta('lastUserId'); };

  /* Called whenever the server confirms the session is valid. */
  SP.verifyOnline = function (userId) {
    var now = this.clock();
    return this.b.transaction(['profiles'], 'rw', function (tx) {
      return tx.get('profiles', userId).then(function (p) {
        if (!p) return null;
        p.lastVerifiedAt = now; p.offlineExpiresAt = now + p.graceMs; p.locked = false;
        return tx.put('profiles', p).then(function () { return p; });
      });
    });
  };

  /* Documented offline authorisation policy (see OFFLINE_SYNC.md). */
  SP.offlineAccess = function (userId) {
    var now = this.clock();
    return this.getProfile(userId).then(function (p) {
      if (!p) return { allowed: false, reason: 'no_profile' };
      if (p.locked) return { allowed: false, reason: 'signed_out', profile: p };
      if (now > p.offlineExpiresAt) return { allowed: false, reason: 'expired', profile: p };
      return { allowed: true, reason: 'ok', profile: p, expiresAt: p.offlineExpiresAt };
    });
  };

  SP.getAssignments = function (userId) {
    return this.b.transaction(['assignments'], 'r', function (tx) { return tx.byIndex('assignments', 'userId', userId); })
      .then(function (rows) {
        return rows.sort(function (a, b) {
          return (a.className + a.subjectName).localeCompare(b.className + b.subjectName);
        });
      });
  };
  SP.getRoster = function (userId, classId) {
    return this.b.transaction(['students'], 'r', function (tx) { return tx.byIndex('students', 'userClass', userId + ':' + classId); })
      .then(function (rows) {
        return rows.sort(function (a, b) { return (a.surname + a.firstName).localeCompare(b.surname + b.firstName); });
      });
  };

  /* One attendance sheet per user + class/subject + school day. Reopening the
     same class that day resumes it instead of creating a duplicate. */
  SP.openSheet = function (userId, assignmentId, school, deviceUid) {
    var self = this, natural = userId + ':' + assignmentId + ':' + school.date;
    return this.b.transaction(['sessions', 'assignments'], 'rw', function (tx) {
      return tx.byIndex('sessions', 'natural', natural).then(function (existing) {
        if (existing.length) return { session: existing[0], created: false };
        return tx.get('assignments', userId + ':' + assignmentId).then(function (a) {
          if (!a) throw new Error('assignment_not_downloaded');
          var s = {
            clientUid: self.uuid(), userId: userId, assignmentId: assignmentId, classId: a.classId,
            className: a.className, subjectName: a.subjectName, subjectCode: a.subjectCode,
            sessionDate: school.date, scheduledStart: school.datetime, natural: natural,
            status: 'draft', syncStatus: null, retryCount: 0, lastError: null, nextRetryAt: 0,
            deviceUid: deviceUid, createdAt: self.clock(), updatedAt: self.clock()
          };
          return tx.put('sessions', s).then(function () { return { session: s, created: true }; });
        });
      });
    });
  };

  SP.getSession = function (uid) {
    return this.b.transaction(['sessions'], 'r', function (tx) { return tx.get('sessions', uid); });
  };
  SP.listRecords = function (sessionUid) {
    return this.b.transaction(['records'], 'r', function (tx) { return tx.byIndex('records', 'sessionUid', sessionUid); });
  };

  /* Auto-saved as the teacher taps, so a reload or crash loses nothing. */
  SP.setMark = function (userId, sessionUid, student, status, markedAt, extra) {
    var self = this;
    return this.b.transaction(['sessions', 'records'], 'rw', function (tx) {
      return tx.get('sessions', sessionUid).then(function (s) {
        if (!s || s.userId !== userId) throw new Error('not_allowed');
        if (s.status !== 'draft') throw new Error('session_already_saved');
        return tx.byIndex('records', 'sessionUid', sessionUid).then(function (rows) {
          var existing = rows.filter(function (r) { return r.studentId === student.studentId; })[0];
          var viaFace = !!(extra && extra.method === 'face');
          // A manual tap must never silently replace a face-recognition record; it needs an explicit override.
          if (existing && existing.method === 'face' && !viaFace && !(extra && extra.override)) throw new Error('face_record_protected');
          var rec = existing || { clientUid: self.uuid(), sessionUid: sessionUid, userId: userId, studentId: student.studentId };
          rec.studentName = student.firstName + ' ' + student.surname;
          rec.admissionNumber = student.admissionNumber;
          rec.status = status; rec.recordedAt = markedAt; rec.syncStatus = 'draft';
          if (viaFace) {
            rec.method = 'face'; rec.confidence = extra.confidence; rec.engine = extra.engine;
            rec.engineVersion = extra.engineVersion; rec.templateUid = extra.templateUid;
          } else {
            rec.method = 'manual'; rec.confidence = null; rec.engine = null; rec.engineVersion = null; rec.templateUid = null;
          }
          return tx.put('records', rec);
        });
      });
    });
  };
  SP.clearMark = function (userId, sessionUid, studentId) {
    return this.b.transaction(['sessions', 'records'], 'rw', function (tx) {
      return tx.get('sessions', sessionUid).then(function (s) {
        if (!s || s.userId !== userId || s.status !== 'draft') throw new Error('not_allowed');
        return tx.byIndex('records', 'sessionUid', sessionUid).then(function (rows) {
          return Promise.all(rows.filter(function (r) { return r.studentId === studentId; })
            .map(function (r) { return tx.del('records', r.clientUid); }));
        });
      });
    });
  };

  /* Quota guard: refuse to claim "saved" when the device is nearly full. */
  SP.checkStorage = function (neededBytes) {
    if (!this.estimator) return Promise.resolve({ ok: true, unknown: true });
    return this.estimator().then(function (e) {
      if (!e || !e.quota) return { ok: true, unknown: true };
      var free = e.quota - e.usage;
      return { ok: free > (neededBytes || 2 * 1024 * 1024), free: free };
    }, function () { return { ok: true, unknown: true }; });
  };

  /* Save = ONE transaction. Resolves only after the data is durably stored,
     so the "saved on this device" message can never be shown early. */
  SP.finalizeSession = function (userId, sessionUid, roster, closedAt) {
    var self = this;
    return this.checkStorage().then(function (st) {
      if (!st.ok) { var e = new Error('storage_full'); e.code = 'storage_full'; throw e; }
      return self.b.transaction(['sessions', 'records'], 'rw', function (tx) {
        return tx.get('sessions', sessionUid).then(function (s) {
          if (!s || s.userId !== userId) throw new Error('not_allowed');
          if (s.status !== 'draft') throw new Error('session_already_saved');
          return tx.byIndex('records', 'sessionUid', sessionUid).then(function (rows) {
            var have = {};
            rows.forEach(function (r) { have[r.studentId] = r; });
            var writes = [];
            roster.forEach(function (st2) {
              if (!have[st2.studentId]) {
                var rec = { clientUid: self.uuid(), sessionUid: sessionUid, userId: userId, studentId: st2.studentId,
                  studentName: st2.firstName + ' ' + st2.surname, admissionNumber: st2.admissionNumber,
                  status: 'absent', method: 'manual', recordedAt: closedAt, syncStatus: 'pending' };
                have[st2.studentId] = rec; writes.push(tx.put('records', rec));
              }
            });
            Object.keys(have).forEach(function (k) {
              var r = have[k];
              if (r.syncStatus === 'draft') { r.syncStatus = 'pending'; writes.push(tx.put('records', r)); }
            });
            s.status = 'saved'; s.syncStatus = 'pending'; s.closedAt = closedAt; s.savedAt = self.clock();
            s.updatedAt = self.clock();
            writes.push(tx.put('sessions', s));
            return Promise.all(writes).then(function () { return { session: s, recordCount: Object.keys(have).length }; });
          });
        });
      });
    });
  };

  SP.listSessions = function (userId, limit) {
    return this.b.transaction(['sessions'], 'r', function (tx) { return tx.byIndex('sessions', 'userId', userId); })
      .then(function (rows) {
        rows.sort(function (a, b) { return b.createdAt - a.createdAt; });
        return limit ? rows.slice(0, limit) : rows;
      });
  };

  SP.counts = function (userId) {
    var self = this;
    return this.b.transaction(['sessions', 'records', 'meta'], 'r', function (tx) {
      return Promise.all([tx.byIndex('sessions', 'userId', userId), tx.byIndex('records', 'userId', userId), tx.get('meta', 'lastSyncAt:' + userId)]);
    }).then(function (r) {
      var sessions = r[0], records = r[1];
      var c = { drafts: 0, pendingSessions: 0, syncingSessions: 0, failedSessions: 0, rejectedSessions: 0,
        syncedSessions: 0, pendingRecords: 0, conflictRecords: 0, rejectedRecords: 0,
        lastSyncAt: r[2] ? r[2].value : null };
      sessions.forEach(function (s) {
        if (s.status === 'draft') c.drafts++;
        else if (s.syncStatus === 'pending') c.pendingSessions++;
        else if (s.syncStatus === 'syncing') c.syncingSessions++;
        else if (s.syncStatus === 'failed') c.failedSessions++;
        else if (s.syncStatus === 'rejected') c.rejectedSessions++;
        else if (s.syncStatus === 'synced' || s.syncStatus === 'partial') c.syncedSessions++;
      });
      records.forEach(function (x) {
        if (x.syncStatus === 'pending' || x.syncStatus === 'syncing' || x.syncStatus === 'failed') c.pendingRecords++;
        else if (x.syncStatus === 'conflict') c.conflictRecords++;
        else if (x.syncStatus === 'rejected') c.rejectedRecords++;
      });
      // Records of sessions that were rejected as a whole are not "pending"
      return c;
    });
  };

  /* ----- sync bookkeeping ------------------------------------------------ */
  SP.listSyncable = function (userId, now, manual) {
    return this.listSessions(userId).then(function (rows) {
      return rows.filter(function (s) {
        if (s.status !== 'saved') return false;
        if (s.syncStatus === 'pending') return true;
        return s.syncStatus === 'failed' && (manual || (s.nextRetryAt || 0) <= now);
      }).sort(function (a, b) { return a.createdAt - b.createdAt; });
    });
  };
  SP.earliestRetryAt = function (userId) {
    return this.listSessions(userId).then(function (rows) {
      var t = rows.filter(function (s) { return s.syncStatus === 'failed'; }).map(function (s) { return s.nextRetryAt || 0; });
      return t.length ? Math.min.apply(null, t) : null;
    });
  };

  SP.markSyncing = function (sessionUid) {
    var self = this;
    return this.b.transaction(['sessions', 'records'], 'rw', function (tx) {
      return tx.get('sessions', sessionUid).then(function (s) {
        s.syncStatus = 'syncing'; s.updatedAt = self.clock();
        return tx.put('sessions', s).then(function () { return tx.byIndex('records', 'sessionUid', sessionUid); })
          .then(function (rows) {
            return Promise.all(rows.filter(function (r) { return r.syncStatus === 'pending' || r.syncStatus === 'failed'; })
              .map(function (r) { r.syncStatus = 'syncing'; return tx.put('records', r); }));
          });
      });
    });
  };

  /* Put 'syncing' back to 'pending' WITHOUT counting a retry (used for
     "sign in again" and for recovery after the app was closed mid-request). */
  SP.revertSyncing = function (sessionUid) {
    var self = this;
    return this.b.transaction(['sessions', 'records'], 'rw', function (tx) {
      return tx.get('sessions', sessionUid).then(function (s) {
        if (!s) return null;
        if (s.syncStatus === 'syncing') { s.syncStatus = 'pending'; s.updatedAt = self.clock(); }
        return tx.put('sessions', s).then(function () { return tx.byIndex('records', 'sessionUid', sessionUid); })
          .then(function (rows) {
            return Promise.all(rows.filter(function (r) { return r.syncStatus === 'syncing'; })
              .map(function (r) { r.syncStatus = 'pending'; return tx.put('records', r); }));
          });
      });
    });
  };
  SP.recoverInterrupted = function (userId) {
    var self = this;
    return this.listSessions(userId).then(function (rows) {
      return Promise.all(rows.filter(function (s) { return s.syncStatus === 'syncing'; })
        .map(function (s) { return self.revertSyncing(s.clientUid); }));
    });
  };

  SP.markFailed = function (sessionUid, code, now, delayMs) {
    var self = this;
    return this.b.transaction(['sessions', 'records'], 'rw', function (tx) {
      return tx.get('sessions', sessionUid).then(function (s) {
        s.syncStatus = 'failed'; s.retryCount = (s.retryCount || 0) + 1; s.lastError = sanitizeError(code);
        s.nextRetryAt = now + delayMs; s.updatedAt = self.clock();
        return tx.put('sessions', s).then(function () { return tx.byIndex('records', 'sessionUid', sessionUid); })
          .then(function (rows) {
            return Promise.all(rows.filter(function (r) { return r.syncStatus === 'syncing'; })
              .map(function (r) { r.syncStatus = 'pending'; return tx.put('records', r); }));
          });
      });
    });
  };

  SP.markRejected = function (sessionUid, code) {
    var self = this;
    return this.b.transaction(['sessions', 'records'], 'rw', function (tx) {
      return tx.get('sessions', sessionUid).then(function (s) {
        s.syncStatus = 'rejected'; s.lastError = sanitizeError(code); s.updatedAt = self.clock();
        return tx.put('sessions', s).then(function () { return tx.byIndex('records', 'sessionUid', sessionUid); })
          .then(function (rows) {
            // Kept on the device for review, but no longer "pending": retrying cannot succeed.
            return Promise.all(rows.filter(function (r) { return ['syncing', 'pending', 'failed'].indexOf(r.syncStatus) >= 0; })
              .map(function (r) { r.syncStatus = 'rejected'; r.note = 'invalid'; return tx.put('records', r); }));
          });
      });
    });
  };

  /* Apply the server's per-record decisions. Records become "synced" ONLY
     for results the server confirmed (created / duplicate). */
  SP.applyResult = function (sessionUid, body, now) {
    var self = this;
    return this.b.transaction(['sessions', 'records', 'meta'], 'rw', function (tx) {
      return Promise.all([tx.get('sessions', sessionUid), tx.byIndex('records', 'sessionUid', sessionUid)]).then(function (r) {
        var s = r[0], rows = r[1], byUid = {};
        rows.forEach(function (x) { byUid[x.clientUid] = x; });
        var decided = 0, synced = 0, conflict = 0, rejected = 0, writes = [];
        (body.records || []).forEach(function (res) {
          var rec = byUid[res.client_uid];
          if (!rec) return;
          if (res.result === 'created' || res.result === 'duplicate') { rec.syncStatus = 'synced'; synced++; }
          else if (res.result === 'conflict') { rec.syncStatus = 'conflict'; rec.note = res.reason; conflict++; }
          else if (res.result === 'rejected') { rec.syncStatus = 'rejected'; rec.note = res.reason; rejected++; }
          else return;
          rec.serverRecordId = res.server_record_id || null; decided++;
          writes.push(tx.put('records', rec));
        });
        // The confirmed records above were already updated in place, so whatever is still
        // pending/syncing/failed here is a record the server did NOT answer for.
        var undecided = rows.filter(function (x) {
          return x.syncStatus === 'pending' || x.syncStatus === 'syncing' || x.syncStatus === 'failed';
        }).length;
        s.serverSessionId = (body.session && body.session.server_session_id) || null;
        s.serverSessionCode = (body.session && body.session.session_code) || null;
        s.conflictCount = (s.conflictCount || 0) + conflict;
        s.updatedAt = self.clock();
        if (undecided > 0) {
          s.syncStatus = 'failed'; s.lastError = 'incomplete_response'; s.retryCount = (s.retryCount || 0) + 1;
          s.nextRetryAt = now + backoffMs(s.retryCount);
          rows.forEach(function (x) { if (x.syncStatus === 'syncing') { x.syncStatus = 'pending'; writes.push(tx.put('records', x)); } });
        } else {
          s.syncStatus = (rejected > 0 && synced + conflict === 0) ? 'rejected' : (rejected > 0 ? 'partial' : 'synced');
          s.retryCount = 0; s.lastError = null; s.nextRetryAt = 0;
          if (s.syncStatus !== 'rejected') writes.push(tx.put('meta', { key: 'lastSyncAt:' + s.userId, value: now }));
        }
        writes.push(tx.put('sessions', s));
        return Promise.all(writes).then(function () { return s; });
      });
    });
  };

  /* Unsynced work that belongs to OTHER accounts on this device. */
  SP.unsyncedFor = function (userId) {
    var self = this;
    return this.counts(userId).then(function (c) {
      return self.b.transaction(['enrollments'], 'r', function (tx) { return tx.byIndex('enrollments', 'userId', userId); }).then(function (enr) {
        var e = enr.filter(function (x) { return ['pending', 'syncing', 'failed'].indexOf(x.syncStatus) >= 0; }).length;
        return c.pendingSessions + c.syncingSessions + c.failedSessions + c.drafts + e;
      });
    });
  };

  /* Sign-out policy: erase everything if nothing is unsynced; otherwise keep ONLY
     the unsynced attendance (locked) and drop the downloaded roster/classes. */
  SP.signOut = function (userId) {
    var self = this;
    return this.unsyncedFor(userId).then(function (n) {
      return self.b.transaction(['profiles', 'assignments', 'students', 'sessions', 'records', 'meta', 'faceTemplates', 'enrollments'], 'rw', function (tx) {
        var dels = [];
        return Promise.all([tx.byIndex('assignments', 'userId', userId), tx.byIndex('students', 'userId', userId),
          tx.byIndex('sessions', 'userId', userId), tx.byIndex('records', 'userId', userId), tx.get('profiles', userId),
          tx.byIndex('faceTemplates', 'userId', userId), tx.byIndex('enrollments', 'userId', userId), tx.all('meta')])
          .then(function (r) {
            r[0].forEach(function (a) { dels.push(tx.del('assignments', a.key)); });
            r[1].forEach(function (s) { dels.push(tx.del('students', s.key)); });
            // Biometric templates and the student list used for enrollment never survive a sign-out.
            r[5].forEach(function (t) { dels.push(tx.del('faceTemplates', t.key)); });
            var unsentEnr = 0;
            r[6].forEach(function (e) {
              if (['pending', 'syncing', 'failed'].indexOf(e.syncStatus) >= 0) unsentEnr++; else dels.push(tx.del('enrollments', e.clientUid));
            });
            r[7].forEach(function (m) {
              if (m.key.indexOf('faceDl:' + userId + ':') === 0 || m.key === 'enrollRoster:' + userId ||
                  (m.key === 'vault:' + userId && unsentEnr === 0)) dels.push(tx.del('meta', m.key));
            });
            if (n === 0) {
              r[2].forEach(function (s) { dels.push(tx.del('sessions', s.clientUid)); });
              r[3].forEach(function (x) { dels.push(tx.del('records', x.clientUid)); });
              dels.push(tx.del('profiles', userId));
            } else {
              var keepSessions = {};
              r[2].forEach(function (s) {
                var unsynced = s.status === 'draft' || ['pending', 'syncing', 'failed'].indexOf(s.syncStatus) >= 0;
                if (unsynced) keepSessions[s.clientUid] = true; else dels.push(tx.del('sessions', s.clientUid));
              });
              r[3].forEach(function (x) { if (!keepSessions[x.sessionUid]) dels.push(tx.del('records', x.clientUid)); });
              if (r[4]) { r[4].locked = true; dels.push(tx.put('profiles', r[4])); }
            }
            return Promise.all(dels).then(function () { return { erased: n === 0, keptUnsynced: n }; });
          });
      });
    });
  };

  /* Explicit, confirmed discard of everything (including unsynced work). */
  SP.eraseAll = function (userId) {
    return this.b.transaction(['profiles', 'assignments', 'students', 'sessions', 'records', 'faceTemplates', 'enrollments', 'meta'], 'rw', function (tx) {
      return Promise.all([tx.byIndex('assignments', 'userId', userId), tx.byIndex('students', 'userId', userId),
        tx.byIndex('sessions', 'userId', userId), tx.byIndex('records', 'userId', userId),
        tx.byIndex('faceTemplates', 'userId', userId), tx.byIndex('enrollments', 'userId', userId), tx.all('meta')]).then(function (r) {
        var d = [];
        r[4].forEach(function (t) { d.push(tx.del('faceTemplates', t.key)); });
        r[5].forEach(function (e) { d.push(tx.del('enrollments', e.clientUid)); });
        r[6].forEach(function (m) {
          if (m.key.indexOf('faceDl:' + userId + ':') === 0 || m.key === 'enrollRoster:' + userId || m.key === 'vault:' + userId) d.push(tx.del('meta', m.key));
        });
        r[0].forEach(function (a) { d.push(tx.del('assignments', a.key)); });
        r[1].forEach(function (s) { d.push(tx.del('students', s.key)); });
        r[2].forEach(function (s) { d.push(tx.del('sessions', s.clientUid)); });
        r[3].forEach(function (x) { d.push(tx.del('records', x.clientUid)); });
        d.push(tx.del('profiles', userId));
        return Promise.all(d);
      });
    });
  };

  /* ------------------------------------------------------------ SyncEngine */
  function buildPayload(session, records) {
    return {
      client_uid: session.clientUid, subject_assignment_id: session.assignmentId,
      session_date: session.sessionDate, scheduled_start: session.scheduledStart,
      closed_at: session.closedAt || session.scheduledStart,
      records: records.map(function (r) {
        var o = { client_uid: r.clientUid, student_id: r.studentId, status: r.status, method: r.method || 'manual', recorded_at: r.recordedAt };
        if (o.method === 'face') {
          o.confidence = r.confidence; o.engine = r.engine; o.engine_version = r.engineVersion; o.template_uid = r.templateUid;
        }
        return o;
      })
    };
  }

  /* opts: {store, api, deviceUid, clock, emit}
     api.syncSession(payload) -> Promise<{status, body}>, rejects with {kind:'network'|'timeout'} */
  function SyncEngine(opts) {
    this.store = opts.store; this.api = opts.api; this.deviceUid = opts.deviceUid;
    this.clock = opts.clock || function () { return Date.now(); };
    this.emit = opts.emit || function () {};
    this.running = false;
  }
  SyncEngine.prototype.syncNow = function (o) {
    var self = this;
    o = o || {};
    if (this.running) return Promise.resolve({ skipped: 'busy' });
    if (!o.userId) return Promise.resolve({ skipped: 'no_user' });
    this.running = true;
    this.emit('sync-start', {});
    var summary = { synced: 0, failed: 0, rejected: 0, authRequired: false, stopped: null };
    return this.store.recoverInterrupted(o.userId).then(function () {
      return self.store.listSyncable(o.userId, self.clock(), !!o.manual);
    }).then(function (due) {
      var chain = Promise.resolve();
      due.forEach(function (s) {
        chain = chain.then(function () {
          if (summary.stopped) return;
          return self._one(s, summary);
        });
      });
      return chain;
    }).then(function () {
      return self.store.counts(o.userId);
    }).then(function (counts) {
      self.running = false;
      var complete = !summary.authRequired && !summary.stopped && summary.failed === 0 &&
        counts.pendingRecords === 0 && counts.failedSessions === 0;
      var result = { summary: summary, counts: counts, complete: complete, anySynced: summary.synced > 0 };
      self.emit('sync-finish', result);
      return result;
    }, function (err) {
      self.running = false;
      self.emit('sync-finish', { error: String(err && err.message || err), summary: summary, complete: false });
      throw err;
    });
  };

  SyncEngine.prototype._one = function (s, summary) {
    var self = this;
    return this.store.listRecords(s.clientUid).then(function (records) {
      var toSend = records.filter(function (r) { return ['pending', 'failed', 'syncing'].indexOf(r.syncStatus) >= 0; });
      if (!toSend.length) return self.store.applyResult(s.clientUid, { records: [], session: {} }, self.clock()).then(function () { summary.synced++; });
      return self.store.markSyncing(s.clientUid).then(function () {
        self.emit('sync-progress', { sessionUid: s.clientUid });
        return self.api.syncSession({ device_uid: self.deviceUid, session: buildPayload(s, toSend) });
      }).then(function (resp) {
        var st = resp.status, body = resp.body || {};
        if (st === 200 && body.ok) {
          return self.store.applyResult(s.clientUid, body, self.clock()).then(function (after) {
            if (after.syncStatus === 'failed') { summary.failed++; summary.stopped = 'incomplete'; }
            else if (after.syncStatus === 'rejected') summary.rejected++;
            else summary.synced++;
          });
        }
        if (st === 401 || (st === 403 && body.code === 'password_change_required')) {
          summary.authRequired = true; summary.stopped = 'auth';
          return self.store.revertSyncing(s.clientUid);
        }
        if (st === 400 && body.code === 'invalid') {
          summary.rejected++; return self.store.markRejected(s.clientUid, 'invalid');
        }
        var code = st === 429 ? 'rate_limited' : (st === 403 ? 'forbidden' : (st === 400 ? 'csrf' : 'server'));
        summary.failed++; summary.stopped = 'server';
        return self.store.markFailed(s.clientUid, code, self.clock(), backoffMs((s.retryCount || 0) + 1));
      }, function (err) {
        // Network failure or timeout: keep everything, back off, stop the run.
        summary.failed++; summary.stopped = 'network';
        var code = err && err.kind === 'timeout' ? 'timeout' : 'network';
        return self.store.markFailed(s.clientUid, code, self.clock(), backoffMs((s.retryCount || 0) + 1));
      });
    });
  };

  return {
    MESSAGES: MESSAGES, SCHEMA: SCHEMA, uuid: uuid, backoffMs: backoffMs, nextProbeDelay: nextProbeDelay,
    parseNaive: parseNaive, schoolNow: schoolNow, effectivePresentStatus: effectivePresentStatus,
    sanitizeError: sanitizeError, computeConnectionState: computeConnectionState,
    MemoryBackend: MemoryBackend, Store: Store, SyncEngine: SyncEngine, buildPayload: buildPayload
  };
});
