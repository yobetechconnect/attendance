/* Offline face recognition - everything EXCEPT the neural network.
 *
 * Contract with the recognition engine (supplied with the model files, see OFFLINE_SYNC.md):
 *
 *   engine = {
 *     id, version, dim,                      // must equal what the server announces
 *     ready: true|false,
 *     async detect(frame) -> [{ box:{x,y,w,h}, score }]          // all faces in the frame
 *     async embed(frame, detection) -> Float32Array(dim)         // identity vector
 *     async liveness(frame, detection) -> {passed, score}        // OPTIONAL presentation-attack check
 *   }
 *   frame = { width, height, data: Uint8ClampedArray (RGBA) }     // e.g. canvas ImageData
 *
 * Detection, quality, embedding, matching and attendance recording are separate steps on purpose.
 * Nothing here ever stores or uploads a photograph: only numeric templates leave the camera pipeline.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./att-core.js'), globalThis.crypto);
  else root.AttFace = factory(root.AttCore, root.crypto);
})(typeof self !== 'undefined' ? self : this, function (Core, webcrypto) {
  'use strict';

  var MESSAGES = {
    engine_unavailable: 'The face-recognition model is not installed or could not be loaded on this device. Use manual attendance.',
    no_templates: 'No face data is downloaded for this class. Use manual attendance, or download face data while online.',
    no_face: 'No face detected. Ask the student to look at the camera.',
    multiple_faces: 'More than one face is visible. Scan one student at a time.',
    too_small: 'Face is too small. Move closer to the camera.',
    off_center: 'Please centre the face in the frame.',
    too_dark: 'Too dark. Move to better light.',
    too_bright: 'Too bright. Avoid direct light behind or on the face.',
    too_blurry: 'Image is blurry. Hold the phone steady.',
    liveness_failed: 'Could not confirm a live face. Use manual attendance for this student.',
    verifying: 'Hold still\u2026',
    not_recognized: 'Face not recognized. The student may not be enrolled. Use manual attendance.',
    low_confidence: 'The match confidence is too low. Try again, or mark this student manually.',
    ambiguous: 'The match is not certain enough. Try again, or mark this student manually.',
    marked: '{name} marked present.',
    already_marked: '{name} has already been marked.'
  };

  /* ------------------------------------------------------------------ codec */
  function floatsToB64(f32) {
    var bytes = new Uint8Array(f32.buffer, f32.byteOffset, f32.byteLength), s = '';
    for (var i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function b64ToFloats(b64) {
    var s = atob(b64), bytes = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i);
    if (bytes.length % 4) throw new Error('bad_template');
    return new Float32Array(bytes.buffer);
  }
  function bytesToB64(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function b64ToBytes(b64) {
    var s = atob(b64), b = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) b[i] = s.charCodeAt(i);
    return b;
  }

  /* ------------------------------------------------------------ vector math */
  function normalize(v) {
    var n = 0, i;
    for (i = 0; i < v.length; i++) n += v[i] * v[i];
    n = Math.sqrt(n);
    var out = new Float32Array(v.length);
    if (!n || !isFinite(n)) return out;
    for (i = 0; i < v.length; i++) out[i] = v[i] / n;
    return out;
  }
  function cosine(a, b) {
    if (a.length !== b.length) return -1;
    var d = 0, na = 0, nb = 0;
    for (var i = 0; i < a.length; i++) { d += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
    return na && nb ? d / Math.sqrt(na * nb) : -1;
  }

  /* ---------------------------------------------------------------- matching
     templates: [{studentId, templateUid, vector}]  (one per student)
     match        best >= threshold AND best beats the runner-up (a DIFFERENT student) by `margin`
     ambiguous    best >= threshold but a different student is almost as close
     low_confidence  within `lowBand` below the threshold (probably right, not sure enough)
     no_match     clearly below. An uncertain face is NEVER turned into attendance. */
  function matchTemplates(embedding, templates, o) {
    o = o || {};
    var threshold = o.threshold, margin = o.margin == null ? 0.05 : o.margin, lowBand = o.lowBand == null ? 0.12 : o.lowBand;
    if (!templates || !templates.length) return { code: 'no_templates' };
    var scored = templates.map(function (t) { return { studentId: t.studentId, templateUid: t.templateUid, score: cosine(embedding, t.vector) }; })
      .sort(function (a, b) { return b.score - a.score; });
    var best = scored[0], second = scored[1] || null;
    var res = { studentId: best.studentId, templateUid: best.templateUid, score: best.score, runnerUp: second ? second.score : null };
    if (best.score >= threshold) {
      if (second && best.score - second.score < margin) { res.code = 'ambiguous'; return res; }
      res.code = 'match'; return res;
    }
    res.code = best.score >= threshold - lowBand ? 'low_confidence' : 'no_match';
    return res;
  }

  /* ---------------------------------------------------------------- quality */
  /* Mean brightness (0-255) and sharpness (variance of a Laplacian) of the face region, from raw RGBA. */
  function frameMetrics(rgba, w, h, box) {
    var x0 = Math.max(0, Math.floor(box.x)), y0 = Math.max(0, Math.floor(box.y));
    var x1 = Math.min(w, Math.ceil(box.x + box.w)), y1 = Math.min(h, Math.ceil(box.y + box.h));
    var bw = x1 - x0, bh = y1 - y0;
    if (bw < 4 || bh < 4) return { brightness: 0, sharpness: 0 };
    var gx = Math.min(64, bw), gy = Math.min(64, bh), luma = new Float32Array(gx * gy), sum = 0, i, j;
    for (j = 0; j < gy; j++) for (i = 0; i < gx; i++) {
      var px = x0 + Math.floor(i * bw / gx), py = y0 + Math.floor(j * bh / gy), o = (py * w + px) * 4;
      var l = 0.299 * rgba[o] + 0.587 * rgba[o + 1] + 0.114 * rgba[o + 2];
      luma[j * gx + i] = l; sum += l;
    }
    var lap = [], lm = 0;
    for (j = 1; j < gy - 1; j++) for (i = 1; i < gx - 1; i++) {
      var v = 4 * luma[j * gx + i] - luma[j * gx + i - 1] - luma[j * gx + i + 1] - luma[(j - 1) * gx + i] - luma[(j + 1) * gx + i];
      lap.push(v); lm += v;
    }
    lm /= lap.length || 1;
    var varL = 0;
    lap.forEach(function (v) { varL += (v - lm) * (v - lm); });
    return { brightness: sum / luma.length, sharpness: varL / (lap.length || 1) };
  }

  var QUALITY_DEFAULTS = { minFaceRatio: 0.25, maxCenterOffset: 0.25, minBrightness: 60, maxBrightness: 200, minSharpness: 30 };
  /* The numbers above are STARTING POINTS. They must be calibrated on the real phones and lighting. */
  function checkQuality(m, o) {
    o = Object.assign({}, QUALITY_DEFAULTS, o || {});
    if (!m.faceCount) return { ok: false, code: 'no_face' };
    if (m.faceCount > 1) return { ok: false, code: 'multiple_faces' };
    var b = m.box;
    if (Math.min(b.w / m.frameW, b.h / m.frameH) < o.minFaceRatio) return { ok: false, code: 'too_small' };
    var dx = Math.abs(b.x + b.w / 2 - m.frameW / 2) / m.frameW, dy = Math.abs(b.y + b.h / 2 - m.frameH / 2) / m.frameH;
    if (dx > o.maxCenterOffset || dy > o.maxCenterOffset) return { ok: false, code: 'off_center' };
    if (m.brightness < o.minBrightness) return { ok: false, code: 'too_dark' };
    if (m.brightness > o.maxBrightness) return { ok: false, code: 'too_bright' };
    if (m.sharpness < o.minSharpness) return { ok: false, code: 'too_blurry' };
    return { ok: true, code: 'ok' };
  }

  /* ------------------------------------------------------ enrollment template */
  function buildTemplate(embeddings, o) {
    o = o || {};
    var minSamples = o.minSamples || 5, minConsistency = o.minConsistency == null ? 0.55 : o.minConsistency;
    if (!embeddings || embeddings.length < minSamples) return { ok: false, code: 'too_few' };
    var dim = embeddings[0].length;
    var normed = embeddings.map(function (e) { if (e.length !== dim) throw new Error('mixed_dimensions'); return normalize(e); });
    var mn = 1, total = 0, pairs = 0, i, j;
    for (i = 0; i < normed.length; i++) for (j = i + 1; j < normed.length; j++) {
      var c = cosine(normed[i], normed[j]); mn = Math.min(mn, c); total += c; pairs++;
    }
    var mean = pairs ? total / pairs : 1;
    var avg = new Float32Array(dim);
    normed.forEach(function (v) { for (var k = 0; k < dim; k++) avg[k] += v[k]; });
    var out = { ok: mn >= minConsistency, code: mn >= minConsistency ? 'ok' : 'inconsistent', vector: normalize(avg),
      sampleCount: normed.length, consistency: { min: mn, mean: mean } };
    return out;
  }

  /* ------------------------------------------------------------------ vault
     AES-GCM key derived from a PIN/passphrase with PBKDF2. The key lives only in memory.
     HONEST LIMIT: a short PIN can be brute-forced by someone who copies the phone's browser
     storage. It protects against casual access and lost/stolen-phone browsing, not a determined
     forensic attacker. Use 6+ characters; longer is better. */
  var PBKDF2_ITERATIONS = 310000;
  var WEAK = ['123456', '1234567', '12345678', '000000', '111111', '654321', '123123', 'password', 'qwerty'];
  function validatePin(pin) {
    if (typeof pin !== 'string' || pin.length < 6) return { ok: false, code: 'too_short' };
    if (WEAK.indexOf(pin.toLowerCase()) >= 0 || /^(.)\1+$/.test(pin)) return { ok: false, code: 'too_weak' };
    return { ok: true };
  }
  function Vault(key, subtle) { this.key = key; this.subtle = subtle; }
  Vault.prototype.encrypt = function (bytes) {
    var iv = webcrypto.getRandomValues(new Uint8Array(12));
    return this.subtle.encrypt({ name: 'AES-GCM', iv: iv }, this.key, bytes).then(function (ct) {
      return { iv: bytesToB64(iv), ct: bytesToB64(new Uint8Array(ct)) };
    });
  };
  Vault.prototype.decrypt = function (blob) {
    return this.subtle.decrypt({ name: 'AES-GCM', iv: b64ToBytes(blob.iv) }, this.key, b64ToBytes(blob.ct)).then(function (pt) { return new Uint8Array(pt); });
  };
  function deriveKey(pin, saltBytes, iterations, subtle) {
    return subtle.importKey('raw', new TextEncoder().encode(pin), 'PBKDF2', false, ['deriveKey']).then(function (base) {
      return subtle.deriveKey({ name: 'PBKDF2', salt: saltBytes, iterations: iterations, hash: 'SHA-256' }, base,
        { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);       // non-extractable
    });
  }
  var VERIFIER = 'att-vault-v1';
  Vault.create = function (pin, o) {
    var subtle = webcrypto.subtle, iterations = (o && o.iterations) || PBKDF2_ITERATIONS;
    var salt = webcrypto.getRandomValues(new Uint8Array(16));
    return deriveKey(pin, salt, iterations, subtle).then(function (key) {
      var v = new Vault(key, subtle);
      return v.encrypt(new TextEncoder().encode(VERIFIER)).then(function (verifier) {
        return { vault: v, record: { v: 1, salt: bytesToB64(salt), iterations: iterations, verifier: verifier } };
      });
    });
  };
  Vault.open = function (record, pin) {
    var subtle = webcrypto.subtle;
    return deriveKey(pin, b64ToBytes(record.salt), record.iterations, subtle).then(function (key) {
      var v = new Vault(key, subtle);
      return v.decrypt(record.verifier).then(function (pt) {
        if (new TextDecoder().decode(pt) !== VERIFIER) throw new Error('bad_pin');
        return v;
      }, function () { throw new Error('bad_pin'); });
    });
  };

  /* -------------------------------------------------------------- FaceStore */
  function FaceStore(store, o) {
    this.s = store; this.b = store.b; this.clock = store.clock; this.uuid = store.uuid;
    this.iterations = o && o.iterations; this.vault = null;
  }
  var FP = FaceStore.prototype;
  var UNSENT = ['pending', 'syncing', 'failed'];

  FP._need = function () { if (!this.vault) { var e = new Error('vault_locked'); e.code = 'vault_locked'; throw e; } return this.vault; };
  FP.vaultState = function (userId) {
    var self = this;
    return this.s.getMeta('vault:' + userId).then(function (r) { return !r ? 'none' : (self.vault ? 'unlocked' : 'locked'); });
  };
  FP.createVault = function (userId, pin) {
    var self = this, v = validatePin(pin);
    if (!v.ok) { var e = new Error(v.code); e.code = v.code; return Promise.reject(e); }
    return this.s.getMeta('vault:' + userId).then(function (existing) {
      if (existing) throw new Error('vault_exists');
      return Vault.create(pin, { iterations: self.iterations });
    }).then(function (r) { self.vault = r.vault; return self.s.setMeta('vault:' + userId, r.record); });
  };
  /* Wrong-PIN attempts are slowed down (5 free tries, then 30 s doubling up to 15 min). */
  FP.unlock = function (userId, pin) {
    var self = this, now = this.clock();
    return Promise.all([this.s.getMeta('vault:' + userId), this.s.getMeta('vaultFail:' + userId)]).then(function (r) {
      var rec = r[0], fail = r[1] || { count: 0, until: 0 };
      if (!rec) throw new Error('no_vault');
      if (now < fail.until) { var e = new Error('too_many_attempts'); e.retryAt = fail.until; throw e; }
      return Vault.open(rec, pin).then(function (v) {
        self.vault = v;
        return self.s.setMeta('vaultFail:' + userId, { count: 0, until: 0 });
      }, function () {
        var count = fail.count + 1;
        var until = count >= 5 ? now + Math.min(30000 * Math.pow(2, count - 5), 15 * 60 * 1000) : 0;
        return self.s.setMeta('vaultFail:' + userId, { count: count, until: until }).then(function () {
          var e = new Error('bad_pin'); e.attemptsLeft = Math.max(0, 5 - count); throw e;
        });
      });
    });
  };
  FP.lock = function () { this.vault = null; };
  /* Always use this (not Store.signOut directly) so the in-memory key is dropped too. */
  FP.signOut = function (userId) { var self = this; return this.s.signOut(userId).then(function (r) { self.lock(); return r; }); };

  /* Forgot the PIN: face data can be downloaded again, but UNSENT ENROLLMENTS cannot be recovered,
     so this is refused unless the user explicitly accepts losing them. */
  FP.resetVault = function (userId, o) {
    var self = this, force = !!(o && o.force);
    return this.listEnrollments(userId).then(function (enr) {
      var unsent = enr.filter(function (e) { return UNSENT.indexOf(e.syncStatus) >= 0; }).length;
      if (unsent && !force) { var e = new Error('pending_enrollments'); e.count = unsent; throw e; }
      return self.b.transaction(['faceTemplates', 'enrollments', 'meta'], 'rw', function (tx) {
        return Promise.all([tx.byIndex('faceTemplates', 'userId', userId), tx.byIndex('enrollments', 'userId', userId), tx.all('meta')]).then(function (r) {
          var d = [];
          r[0].forEach(function (t) { d.push(tx.del('faceTemplates', t.key)); });
          r[1].forEach(function (x) { d.push(tx.del('enrollments', x.clientUid)); });
          r[2].forEach(function (m) {
            if (m.key === 'vault:' + userId || m.key === 'vaultFail:' + userId || m.key.indexOf('faceDl:' + userId + ':') === 0) d.push(tx.del('meta', m.key));
          });
          return Promise.all(d);
        });
      }).then(function () { self.vault = null; });
    });
  };

  /* ---- templates downloaded for a class (encrypted at rest) */
  FP.saveClassTemplates = function (userId, classId, pkg) {
    var self = this, vault;
    try { vault = this._need(); } catch (e) { return Promise.reject(e); }
    var valid = [], invalid = 0;
    (pkg.templates || []).forEach(function (t) {
      try { if (b64ToFloats(t.template_b64).length === pkg.dim) valid.push(t); else invalid++; } catch (e) { invalid++; }
    });
    return Promise.all(valid.map(function (t) {
      return vault.encrypt(new TextEncoder().encode(t.template_b64)).then(function (blob) {
        return { key: userId + ':' + classId + ':' + t.student_id, userId: userId, userClass: userId + ':' + classId, classId: classId,
          studentId: t.student_id, templateUid: t.enrollment_uid, blob: blob };
      });
    })).then(function (rows) {
      var meta = { at: self.clock(), engine: pkg.engine, engineVersion: pkg.engine_version, dim: pkg.dim, threshold: pkg.min_similarity,
        margin: pkg.margin, count: rows.length, invalid: invalid, missing: pkg.missing_student_ids || [] };
      return self.s.checkStorage(rows.length * 4096 + 1024 * 1024).then(function (st) {
        if (!st.ok) { var e = new Error('storage_full'); e.code = 'storage_full'; throw e; }
        return self.b.transaction(['faceTemplates', 'meta'], 'rw', function (tx) {
          return tx.byIndex('faceTemplates', 'userClass', userId + ':' + classId).then(function (old) {
            return Promise.all(old.map(function (o) { return tx.del('faceTemplates', o.key); })
              .concat(rows.map(function (r) { return tx.put('faceTemplates', r); }))
              .concat([tx.put('meta', { key: 'faceDl:' + userId + ':' + classId, value: meta })]));
          });
        });
      }).then(function () { return meta; });
    });
  };
  FP.classStatus = function (userId, classId) { return this.s.getMeta('faceDl:' + userId + ':' + classId).then(function (m) { return m || null; }); };
  FP.loadClassTemplates = function (userId, classId) {
    var self = this, vault;
    try { vault = this._need(); } catch (e) { return Promise.reject(e); }
    return Promise.all([this.classStatus(userId, classId),
      this.b.transaction(['faceTemplates'], 'r', function (tx) { return tx.byIndex('faceTemplates', 'userClass', userId + ':' + classId); })])
      .then(function (r) {
        return Promise.all(r[1].map(function (row) {
          return vault.decrypt(row.blob).then(function (pt) {
            return { studentId: row.studentId, templateUid: row.templateUid, vector: normalize(b64ToFloats(new TextDecoder().decode(pt))) };
          });
        })).then(function (templates) { return { meta: r[0], templates: templates }; });
      });
  };

  /* ---- enrollments captured on this device */
  FP.saveEnrollRoster = function (userId, students) { return this.s.setMeta('enrollRoster:' + userId, { at: this.clock(), students: students }); };
  FP.getEnrollRoster = function (userId) { return this.s.getMeta('enrollRoster:' + userId); };

  FP.saveEnrollment = function (userId, e) {
    var self = this, vault;
    try { vault = this._need(); } catch (err) { return Promise.reject(err); }
    if (!e.consentConfirmed) { var c = new Error('consent_required'); c.code = 'consent_required'; return Promise.reject(c); }
    if (!e.vector || e.vector.length !== e.dim) return Promise.reject(new Error('wrong_dimension'));
    var rec = { clientUid: this.uuid(), userId: userId, studentId: e.studentId, studentName: e.studentName, admissionNumber: e.admissionNumber,
      engine: e.engine, engineVersion: e.engineVersion, dim: e.dim, sampleCount: e.sampleCount, quality: e.quality == null ? null : e.quality,
      consentNote: e.consentNote || null, replace: !!e.replace, capturedAt: e.capturedAt, syncStatus: 'pending', retryCount: 0,
      lastError: null, nextRetryAt: 0, createdAt: this.clock() };
    return Promise.all([vault.encrypt(new TextEncoder().encode(floatsToB64(normalize(e.vector)))), this.s.checkStorage()]).then(function (r) {
      if (!r[1].ok) { var er = new Error('storage_full'); er.code = 'storage_full'; throw er; }
      rec.blob = r[0];
      return self.b.transaction(['enrollments'], 'rw', function (tx) {
        return tx.byIndex('enrollments', 'userId', userId).then(function (rows) {
          // one unsent enrollment per student: no accidental duplicates from double taps or retries
          var dup = rows.filter(function (x) { return x.studentId === e.studentId && UNSENT.indexOf(x.syncStatus) >= 0; });
          if (dup.length && !e.replacePending) { var er2 = new Error('enrollment_exists'); er2.code = 'enrollment_exists'; throw er2; }
          return Promise.all(dup.map(function (x) { return tx.del('enrollments', x.clientUid); }).concat([tx.put('enrollments', rec)]));
        });
      });
    }).then(function () { var out = Object.assign({}, rec); delete out.blob; return out; });
  };
  FP.listEnrollments = function (userId) {
    return this.b.transaction(['enrollments'], 'r', function (tx) { return tx.byIndex('enrollments', 'userId', userId); }).then(function (rows) {
      return rows.map(function (r) { var o = Object.assign({}, r); delete o.blob; return o; }).sort(function (a, b) { return b.createdAt - a.createdAt; });
    });
  };
  FP.enrollmentCounts = function (userId) {
    return this.listEnrollments(userId).then(function (rows) {
      var c = { pending: 0, syncing: 0, failed: 0, rejected: 0, conflict: 0, synced: 0 };
      rows.forEach(function (r) { if (r.syncStatus in c) c[r.syncStatus]++; });
      c.unsent = c.pending + c.syncing + c.failed;
      return c;
    });
  };
  FP.readEnrollmentTemplate = function (clientUid) {
    var vault;
    try { vault = this._need(); } catch (e) { return Promise.reject(e); }
    return this.b.transaction(['enrollments'], 'r', function (tx) { return tx.get('enrollments', clientUid); }).then(function (rec) {
      return vault.decrypt(rec.blob).then(function (pt) { return new TextDecoder().decode(pt); });
    });
  };

  /* sync bookkeeping for enrollments (same rules as attendance sessions) */
  FP._update = function (clientUid, fn) {
    return this.b.transaction(['enrollments'], 'rw', function (tx) {
      return tx.get('enrollments', clientUid).then(function (r) { if (!r) return null; fn(r); return tx.put('enrollments', r).then(function () { return r; }); });
    });
  };
  FP.listSyncableEnrollments = function (userId, now, manual) {
    return this.listEnrollments(userId).then(function (rows) {
      return rows.filter(function (r) { return r.syncStatus === 'pending' || (r.syncStatus === 'failed' && (manual || (r.nextRetryAt || 0) <= now)); })
        .sort(function (a, b) { return a.createdAt - b.createdAt; });
    });
  };
  FP.recoverEnrollments = function (userId) {
    var self = this;
    return this.listEnrollments(userId).then(function (rows) {
      return Promise.all(rows.filter(function (r) { return r.syncStatus === 'syncing'; }).map(function (r) {
        return self._update(r.clientUid, function (x) { x.syncStatus = 'pending'; });
      }));
    });
  };

  /* -------------------------------------------------------- EnrollmentSync */
  function EnrollmentSync(o) {
    this.faces = o.faces; this.api = o.api; this.deviceUid = o.deviceUid; this.clock = o.clock || function () { return Date.now(); };
    this.emit = o.emit || function () {}; this.running = false;
  }
  EnrollmentSync.prototype.syncNow = function (o) {
    var self = this; o = o || {};
    if (this.running) return Promise.resolve({ skipped: 'busy' });
    if (!o.userId) return Promise.resolve({ skipped: 'no_user' });
    if (!this.faces.vault) return Promise.resolve({ skipped: 'locked' });     // encrypted: needs the PIN to be sent
    this.running = true;
    var summary = { synced: 0, failed: 0, rejected: 0, conflicts: 0, authRequired: false, stopped: null };
    return this.faces.recoverEnrollments(o.userId).then(function () {
      return self.faces.listSyncableEnrollments(o.userId, self.clock(), !!o.manual);
    }).then(function (due) {
      var chain = Promise.resolve();
      due.forEach(function (e) { chain = chain.then(function () { if (!summary.stopped) return self._one(e, summary); }); });
      return chain;
    }).then(function () { return self.faces.enrollmentCounts(o.userId); }).then(function (counts) {
      self.running = false;
      var res = { summary: summary, counts: counts, complete: !summary.stopped && counts.unsent === 0, anySynced: summary.synced > 0 };
      self.emit('enroll-sync-finish', res);
      return res;
    }, function (err) { self.running = false; throw err; });
  };
  EnrollmentSync.prototype._one = function (e, summary) {
    var self = this, faces = this.faces;
    return faces._update(e.clientUid, function (x) { x.syncStatus = 'syncing'; }).then(function () {
      return faces.readEnrollmentTemplate(e.clientUid);
    }).then(function (b64) {
      return self.api.syncEnrollment({ device_uid: self.deviceUid, enrollment: {
        client_uid: e.clientUid, student_id: e.studentId, engine: e.engine, engine_version: e.engineVersion, template_b64: b64,
        sample_count: e.sampleCount, quality_score: e.quality, consent_confirmed: true, consent_note: e.consentNote,
        replace: !!e.replace, captured_at: e.capturedAt } });
    }).then(function (resp) {
      var st = resp.status, body = resp.body || {};
      if (st === 200 && body.ok && body.enrollment) {
        var r = body.enrollment.result;
        return faces._update(e.clientUid, function (x) {
          x.retryCount = 0; x.lastError = null; x.serverId = body.enrollment.server_id || null;
          if (r === 'created' || r === 'replaced' || r === 'duplicate') { x.syncStatus = 'synced'; }
          else if (r === 'conflict') { x.syncStatus = 'conflict'; x.note = body.enrollment.reason; }
          else { x.syncStatus = 'rejected'; x.note = body.enrollment.reason || 'rejected'; }
        }).then(function (x) { if (x.syncStatus === 'synced') summary.synced++; else if (x.syncStatus === 'conflict') summary.conflicts++; else summary.rejected++; });
      }
      if (st === 401 || (st === 403 && body.code === 'password_change_required')) {
        summary.authRequired = true; summary.stopped = 'auth';
        return faces._update(e.clientUid, function (x) { x.syncStatus = 'pending'; });
      }
      if (st === 400 && body.code === 'invalid') {
        summary.rejected++;
        return faces._update(e.clientUid, function (x) { x.syncStatus = 'rejected'; x.note = 'invalid'; });
      }
      return self._fail(e, summary, st === 429 ? 'rate_limited' : (st === 403 ? 'forbidden' : 'server'), 'server');
    }, function (err) {
      if (err && err.code === 'vault_locked') { summary.stopped = 'locked'; return faces._update(e.clientUid, function (x) { x.syncStatus = 'pending'; }); }
      return self._fail(e, summary, err && err.kind === 'timeout' ? 'timeout' : 'network', 'network');
    });
  };
  EnrollmentSync.prototype._fail = function (e, summary, code, stop) {
    var now = this.clock();
    summary.failed++; summary.stopped = stop;
    return this.faces._update(e.clientUid, function (x) {
      x.syncStatus = 'failed'; x.retryCount = (x.retryCount || 0) + 1; x.lastError = Core.sanitizeError(code);
      x.nextRetryAt = now + Core.backoffMs(x.retryCount);
    });
  };

  /* ------------------------------------------------------------ FaceScanner */
  function format(msg, name) { return msg.replace('{name}', name || 'Student'); }
  function FaceScanner(o) {
    this.engine = o.engine; this.templates = o.templates || [];
    this.threshold = o.threshold; this.margin = o.margin; this.quality = o.quality; this.lowBand = o.lowBand;
    this.stableFrames = o.stableFrames || 2; this.cooldownMs = o.cooldownMs == null ? 4000 : o.cooldownMs;
    this.clock = o.clock || function () { return Date.now(); };
    this.isMarked = o.isMarked || function () { return false; };
    this.nameOf = o.nameOf || function () { return 'Student'; };
    this.candidate = null; this.lastShown = {}; this.stats = {}; this.log = [];
  }
  FaceScanner.prototype._out = function (code, extra) {
    var out = Object.assign({ code: code, mark: false }, extra || {});
    out.message = format(MESSAGES[code] || code, out.name);
    this.stats[code] = (this.stats[code] || 0) + 1;
    if (code !== 'verifying' && code !== 'cooldown') {
      this.log.push({ at: this.clock(), code: code, studentId: out.studentId || null, score: out.score == null ? null : Math.round(out.score * 100) / 100 });
      if (this.log.length > 50) this.log.shift();
    }
    if (['marked', 'verifying', 'already_marked', 'cooldown'].indexOf(code) < 0) this.candidate = null;
    return out;
  };
  /* One camera frame -> one outcome. Only 'marked' (mark:true) may be turned into attendance. */
  FaceScanner.prototype.processFrame = function (frame) {
    var self = this, eng = this.engine;
    if (!eng || !eng.ready) return Promise.resolve(this._out('engine_unavailable'));
    if (!this.templates.length) return Promise.resolve(this._out('no_templates'));
    return Promise.resolve(eng.detect(frame)).then(function (dets) {
      dets = dets || [];
      var m = { faceCount: dets.length, box: dets[0] && dets[0].box, frameW: frame.width, frameH: frame.height, brightness: 0, sharpness: 0 };
      if (dets.length === 1) { var fm = frameMetrics(frame.data, frame.width, frame.height, dets[0].box); m.brightness = fm.brightness; m.sharpness = fm.sharpness; }
      var q = checkQuality(m, self.quality);
      if (!q.ok) return self._out(q.code);
      var det = dets[0];
      var live = eng.liveness ? Promise.resolve(eng.liveness(frame, det)) : Promise.resolve({ passed: true, skipped: true });
      return live.then(function (lv) {
        if (!lv.passed) return self._out('liveness_failed');
        return Promise.resolve(eng.embed(frame, det)).then(function (emb) {
          var res = matchTemplates(normalize(emb), self.templates, { threshold: self.threshold, margin: self.margin, lowBand: self.lowBand });
          var base = { studentId: res.studentId, score: res.score, templateUid: res.templateUid };
          if (res.code === 'no_match') return self._out('not_recognized', { score: res.score });
          if (res.code === 'low_confidence') return self._out('low_confidence', base);
          if (res.code === 'ambiguous') return self._out('ambiguous', base);
          // A match must hold for several consecutive frames before it counts (reduces false accepts).
          if (self.candidate && self.candidate.studentId === res.studentId) self.candidate.count++;
          else self.candidate = { studentId: res.studentId, count: 1 };
          var name = self.nameOf(res.studentId);
          if (self.candidate.count < self.stableFrames) return self._out('verifying', Object.assign({ name: name }, base));
          var now = self.clock();
          if (self.lastShown[res.studentId] && now - self.lastShown[res.studentId] < self.cooldownMs) return self._out('cooldown', Object.assign({ name: name }, base));
          self.lastShown[res.studentId] = now;
          if (self.isMarked(res.studentId)) return self._out('already_marked', Object.assign({ name: name }, base));
          self.candidate = null;
          return self._out('marked', Object.assign({ name: name, mark: true }, base));
        });
      });
    }, function () { return self._out('engine_unavailable'); });
  };
  FaceScanner.prototype.summary = function () { return { stats: Object.assign({}, this.stats), recent: this.log.slice(-20) }; };

  return {
    MESSAGES: MESSAGES, floatsToB64: floatsToB64, b64ToFloats: b64ToFloats, normalize: normalize, cosine: cosine,
    matchTemplates: matchTemplates, frameMetrics: frameMetrics, checkQuality: checkQuality, QUALITY_DEFAULTS: QUALITY_DEFAULTS,
    buildTemplate: buildTemplate, validatePin: validatePin, Vault: Vault, FaceStore: FaceStore, EnrollmentSync: EnrollmentSync,
    FaceScanner: FaceScanner, PBKDF2_ITERATIONS: PBKDF2_ITERATIONS
  };
});
