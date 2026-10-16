/* IndexedDB backend for AttCore.Store.
 *
 * Thin on purpose: all the logic lives in att-core.js (and is unit-tested).
 * Adding Stage 2/3 data later = bump DB_VERSION and add stores in
 * onupgradeneeded. Existing stores are never dropped, so saved attendance
 * survives every app update.
 */
(function (root) {
  'use strict';
  var DB_NAME = 'att-offline';
  var DB_VERSION = 2;   // v2 adds faceTemplates + enrollments; existing stores and data are kept

  function open() {
    return new Promise(function (resolve, reject) {
      if (!root.indexedDB) { reject(new Error('IndexedDB not supported')); return; }
      var req = root.indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result, schema = root.AttCore.SCHEMA;
        Object.keys(schema).forEach(function (name) {
          if (db.objectStoreNames.contains(name)) return;       // never recreate/drop existing data
          var os = db.createObjectStore(name, { keyPath: schema[name].keyPath });
          Object.keys(schema[name].indexes).forEach(function (idx) {
            os.createIndex(idx, schema[name].indexes[idx], { unique: false });
          });
        });
      };
      req.onsuccess = function () {
        var db = req.result;
        db.onversionchange = function () { db.close(); };
        resolve(db);
      };
      req.onerror = function () { reject(req.error || new Error('IndexedDB open failed')); };
      req.onblocked = function () { reject(new Error('IndexedDB blocked by another tab')); };
    });
  }

  function wrap(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function IdbBackend() { this._db = null; }
  IdbBackend.prototype._conn = function () {
    var self = this;
    if (!this._db) {
      this._db = open().then(function (db) {
        db.onclose = function () { self._db = null; };
        return db;
      }, function (e) { self._db = null; throw e; });
    }
    return this._db;
  };
  IdbBackend.prototype.available = function () {
    return this._conn().then(function () { return true; }, function () { return false; });
  };

  IdbBackend.prototype.transaction = function (names, mode, fn) {
    return this._conn().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx;
        try {
          // 'strict' durability: complete only after the data reached disk (where supported)
          tx = db.transaction(names, mode === 'rw' ? 'readwrite' : 'readonly',
            mode === 'rw' ? { durability: 'strict' } : undefined);
        } catch (e) { reject(e); return; }
        var result, failed = null;
        var api = {
          get: function (s, k) { return wrap(tx.objectStore(s).get(k)); },
          put: function (s, o) { return wrap(tx.objectStore(s).put(o)); },
          del: function (s, k) { return wrap(tx.objectStore(s).delete(k)); },
          all: function (s) { return wrap(tx.objectStore(s).getAll()); },
          byIndex: function (s, idx, v) { return wrap(tx.objectStore(s).index(idx).getAll(IDBKeyRange.only(v))); }
        };
        tx.oncomplete = function () { if (failed) reject(failed); else resolve(result); };
        tx.onabort = function () { reject(failed || tx.error || new Error('Transaction aborted')); };
        tx.onerror = function () { failed = failed || tx.error; };
        Promise.resolve().then(function () { return fn(api); }).then(function (r) { result = r; }, function (e) {
          failed = e;
          try { tx.abort(); } catch (x) { /* already finished */ }
        });
      });
    });
  };

  root.AttIdb = { IdbBackend: IdbBackend, DB_NAME: DB_NAME, DB_VERSION: DB_VERSION };
})(typeof self !== 'undefined' ? self : this);
