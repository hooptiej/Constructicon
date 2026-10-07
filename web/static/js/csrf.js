/* #467 step 1: per-session CSRF token for signed-in pages.
 *
 * base.html loads this (before every other script) only when the page is signed in, with the
 * token in <meta name="csrf-token">. It wraps window.fetch and XMLHttpRequest so every
 * state-changing (non GET/HEAD/OPTIONS/TRACE) request to this same origin carries the
 * X-CSRF-Token header the server checks (web/auth.py). Requests to other origins never get it.
 * The app has no plain HTML POST forms (every form is submitted by JS through fetch/XHR), so
 * no hidden form fields are needed; a new plain POST form would have to submit through fetch.
 */
(function () {
  'use strict';
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta && meta.getAttribute('content');
  if (!token) return;
  var SAFE = /^(GET|HEAD|OPTIONS|TRACE)$/i;
  var HEADER = 'X-CSRF-Token';

  function sameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  var origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (input, init) {
      var isRequest = typeof Request !== 'undefined' && input instanceof Request;
      var method = (init && init.method) || (isRequest ? input.method : 'GET');
      var url = isRequest ? input.url : String(input);
      if (!SAFE.test(method) && sameOrigin(url)) {
        var headers = new Headers((init && init.headers) || (isRequest ? input.headers : undefined));
        if (!headers.has(HEADER)) headers.set(HEADER, token);
        init = Object.assign({}, init || {}, { headers: headers });
      }
      return origFetch.call(this, input, init);
    };
  }

  if (typeof XMLHttpRequest !== 'undefined') {
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__cxMethod = method;
      this.__cxUrl = url;
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      if (this.__cxMethod && !SAFE.test(this.__cxMethod) && sameOrigin(this.__cxUrl)) {
        this.setRequestHeader(HEADER, token);
      }
      return origSend.apply(this, arguments);
    };
  }
})();
