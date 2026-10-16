/* Loads the face-recognition model package that the school installs under
 *   /static/models/face/<engine_version>/manifest.json   and   .../engine.js
 * The folder name contains the version, so files are immutable and safe to cache for offline use.
 *
 * manifest.json: { "id": "<engine id>", "version": "<engine version>", "dim": <embedding size>,
 *                  "entry": "engine.js", "files": ["engine.js", "detector.onnx", ...] }
 * engine.js must call:  AttFaceEngines.register({ id, version, dim, init(baseUrl, manifest) -> Promise<engine> })
 * where engine implements detect/embed(/liveness) exactly as documented at the top of att-face.js.
 *
 * Nothing is faked here: with no model installed the state is 'not_installed' and face scanning stays off.
 * The loader refuses a model whose id/version/dim differ from what the SERVER announced, so templates
 * from different models are never compared. */
(function (root) {
  'use strict';
  var registry = {};
  root.AttFaceEngines = { register: function (factory) { if (factory && factory.id) registry[factory.id + '@' + factory.version] = factory; }, _registry: registry };

  var MESSAGES = {
    not_configured: 'Offline face recognition is not enabled on the server.',
    not_installed: 'The face-recognition model is not installed on the server.',
    not_downloaded: 'The face-recognition model has not been downloaded to this device yet. Connect to the internet once to download it.',
    mismatch: 'The model on this device does not match the version the server uses. Reload the app when online to update it.',
    error: 'The face-recognition model could not be started on this device.',
    ready: 'Face recognition is ready.',
    loading: 'Loading the face-recognition model\u2026'
  };

  function loadScript(url, doc) {
    return new Promise(function (resolve, reject) {
      var s = doc.createElement('script');
      s.src = url; s.async = true;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error('script_failed')); };
      doc.head.appendChild(s);
    });
  }

  /* cfg = profile.face from the server: {offline_supported, engine, engine_version, dim}.
     o.online: allowed to fetch (and so warm the offline cache); o.fetch/o.doc injectable for tests. */
  function load(cfg, o) {
    o = o || {};
    var f = o.fetch || root.fetch, doc = o.doc || root.document;
    if (!cfg || !cfg.offline_supported) return Promise.resolve({ status: 'not_configured', message: MESSAGES.not_configured });
    var base = '/static/models/face/' + encodeURIComponent(cfg.engine_version) + '/';
    return f(base + 'manifest.json', { cache: 'default' }).then(function (r) {
      if (!r.ok) return { status: o.online === false ? 'not_downloaded' : 'not_installed', message: o.online === false ? MESSAGES.not_downloaded : MESSAGES.not_installed };
      return r.json().then(function (m) {
        if (m.id !== cfg.engine || String(m.version) !== String(cfg.engine_version) || Number(m.dim) !== Number(cfg.dim)) {
          return { status: 'mismatch', message: MESSAGES.mismatch };
        }
        var load = (o.loadScript || loadScript)(base + (m.entry || 'engine.js'), doc);
        return load.then(function () {
          var factory = registry[m.id + '@' + m.version];
          if (!factory) return { status: 'error', message: MESSAGES.error };
          return Promise.resolve(factory.init(base, m)).then(function (engine) {
            engine.ready = true; engine.id = m.id; engine.version = String(m.version); engine.dim = Number(m.dim);
            return { status: 'ready', engine: engine, manifest: m, base: base, message: MESSAGES.ready };
          });
        });
      });
    }, function () { return { status: 'not_downloaded', message: MESSAGES.not_downloaded }; })
      .catch(function () { return { status: 'error', message: MESSAGES.error }; });
  }

  /* While online, fetch every model file once so the service worker keeps it for offline use. */
  function prefetch(state, onProgress, o) {
    o = o || {};
    var f = o.fetch || root.fetch, files = (state.manifest && state.manifest.files) || [], done = 0;
    return files.reduce(function (p, file) {
      return p.then(function () {
        return f(state.base + file, { cache: 'reload' }).then(function (r) {
          if (!r.ok) throw new Error('model_file_failed');
          return r.arrayBuffer ? r.arrayBuffer() : null;
        }).then(function () { done++; if (onProgress) onProgress(done, files.length); });
      });
    }, Promise.resolve()).then(function () { return { ok: true, files: files.length }; }, function () { return { ok: false, done: done, files: files.length }; });
  }

  root.AttFaceEngine = { load: load, prefetch: prefetch, MESSAGES: MESSAGES };
})(typeof self !== 'undefined' ? self : this);
