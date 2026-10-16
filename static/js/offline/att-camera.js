/* Camera access with explicit error codes and guaranteed release.
 * The stream is ALWAYS stopped by stop(); the UI also calls it when the page is hidden or closed,
 * so the camera light never stays on after a session ends. Frames stay in memory only. */
(function (root) {
  'use strict';
  var MESSAGES = {
    permission_denied: 'Camera permission was denied. Allow camera access for this site in the browser settings, then try again.',
    no_camera: 'No camera was found on this device.',
    camera_busy: 'The camera is in use by another app or could not be started. Close other apps using the camera and try again.',
    insecure: 'The camera needs a secure (HTTPS) connection.',
    unsupported: 'This browser does not support camera access.',
    camera_error: 'The camera could not be started. Try again, or use manual attendance.'
  };
  function mapError(e) {
    var n = e && e.name;
    if (n === 'NotAllowedError' || n === 'SecurityError' || n === 'PermissionDeniedError') return 'permission_denied';
    if (n === 'NotFoundError' || n === 'DevicesNotFoundError') return 'no_camera';
    if (n === 'NotReadableError' || n === 'TrackStartError' || n === 'AbortError') return 'camera_busy';
    return 'camera_error';
  }

  function Camera(o) {
    o = o || {};
    this.md = o.mediaDevices || (root.navigator && root.navigator.mediaDevices);
    this.doc = o.document || root.document;
    this.maxSide = o.maxSide || 480;
    this.stream = null; this.video = null; this.canvas = null; this.ctx = null; this.deviceId = null;
  }
  Camera.messages = MESSAGES;
  Camera.supported = function (md) {
    md = md || (root.navigator && root.navigator.mediaDevices);
    return !!(md && md.getUserMedia);
  };
  Camera.prototype.active = function () { return !!this.stream; };
  Camera.prototype.devices = function () {
    if (!this.md || !this.md.enumerateDevices) return Promise.resolve([]);
    return this.md.enumerateDevices().then(function (list) { return list.filter(function (d) { return d.kind === 'videoinput'; }); }, function () { return []; });
  };
  function fail(code) { var e = new Error(code); e.code = code; return e; }

  Camera.prototype.start = function (videoEl, o) {
    var self = this; o = o || {};
    this.stop();
    if (root.isSecureContext === false) return Promise.reject(fail('insecure'));
    if (!Camera.supported(this.md)) return Promise.reject(fail('unsupported'));
    var wanted = o.deviceId ? { deviceId: { exact: o.deviceId } } : { facingMode: 'user' };
    var primary = { audio: false, video: Object.assign({ width: { ideal: 640 }, height: { ideal: 480 } }, wanted) };
    return this.md.getUserMedia(primary).catch(function (e) {
      if (e && e.name === 'OverconstrainedError') return self.md.getUserMedia({ audio: false, video: true });   // device vanished: any camera
      throw e;
    }).then(function (stream) {
      self.stream = stream; self.video = videoEl;
      var track = stream.getVideoTracks && stream.getVideoTracks()[0];
      self.deviceId = track && track.getSettings ? (track.getSettings().deviceId || null) : null;
      videoEl.muted = true; videoEl.setAttribute('playsinline', ''); videoEl.srcObject = stream;
      return Promise.resolve(videoEl.play && videoEl.play()).then(function () { return true; });
    }).catch(function (e) { self.stop(); throw fail(e && e.code ? e.code : mapError(e)); });
  };

  Camera.prototype.stop = function () {
    if (this.stream) this.stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* already stopped */ } });
    if (this.video) { try { this.video.srcObject = null; } catch (e) { /* ignore */ } }
    this.stream = null; this.video = null;
  };

  /* One frame as { width, height, data(RGBA) }, scaled so the longest side <= maxSide. */
  Camera.prototype.grab = function () {
    var v = this.video;
    if (!this.stream || !v || !v.videoWidth) return null;
    var scale = Math.min(1, this.maxSide / Math.max(v.videoWidth, v.videoHeight));
    var w = Math.round(v.videoWidth * scale), h = Math.round(v.videoHeight * scale);
    if (!this.canvas) { this.canvas = this.doc.createElement('canvas'); this.ctx = this.canvas.getContext('2d', { willReadFrequently: true }); }
    this.canvas.width = w; this.canvas.height = h;
    this.ctx.drawImage(v, 0, 0, w, h);
    var img = this.ctx.getImageData(0, 0, w, h);
    return { width: w, height: h, data: img.data };
  };

  /* Release the camera whenever the page is hidden or closed. Returns an unsubscribe function. */
  Camera.autoRelease = function (cam, onRelease) {
    function hide() { if (cam.active()) { cam.stop(); if (onRelease) onRelease(); } }
    function vis() { if (root.document && root.document.hidden) hide(); }
    root.document.addEventListener('visibilitychange', vis);
    root.addEventListener('pagehide', hide);
    return function () { root.document.removeEventListener('visibilitychange', vis); root.removeEventListener('pagehide', hide); };
  };

  root.AttCamera = Camera;
})(typeof self !== 'undefined' ? self : this);
