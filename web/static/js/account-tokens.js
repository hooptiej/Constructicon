// #604 step 5: "My MCP tokens" on the account page (templates/change_password.html). List, make,
// rotate and revoke your own tokens. A new secret is shown once, in a read-only box, with the client
// config line to paste. Every failure is shown with the server's reason; nothing fails quietly.
(function () {
  'use strict';
  var list = document.getElementById('mcp-token-list');
  if (!list) return;
  var form = document.getElementById('mcp-token-form');
  var errBox = document.getElementById('mcp-token-error');
  var newBox = document.getElementById('mcp-token-new');
  var valueBox = document.getElementById('mcp-token-value');
  var configBox = document.getElementById('mcp-token-config');

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n[k] = attrs[k]; });
    if (text != null) n.textContent = text;
    return n;
  }
  function fail(msg) { errBox.textContent = msg; errBox.hidden = false; }
  function clearError() { errBox.hidden = true; errBox.textContent = ''; }
  function when(t) {
    if (!t) return 'never';
    return window.MountainTime && MountainTime.formatDateTime ? MountainTime.formatDateTime(t) : new Date(t * 1000).toLocaleString();
  }

  function call(method, url, body) {
    var opts = { method: method, credentials: 'same-origin', headers: {} };
    if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (b) {
        if (!r.ok || !b || b.ok === false) {
          throw new Error((b && ((b.error && b.error.message) || b.detail)) ||
                          ('The server answered ' + r.status + ' with no reason; reload the page to see what changed.'));
        }
        return b;
      });
    }, function (e) { throw new Error('Could not reach the server (' + e.message + '). Nothing was changed.'); });
  }

  function showSecret(token) {
    valueBox.value = token;
    var host = window.location.hostname || '<host>';
    configBox.textContent = '{"type":"http","url":"http://' + host + ':8100/mcp","headers":{"Authorization":"Bearer ' +
      token + '"}}';
    newBox.hidden = false;
    valueBox.focus();
    valueBox.select();
  }

  function render(tokens) {
    list.textContent = '';
    if (!tokens.length) { list.append(el('li', { className: 'dp-muted' }, 'No tokens yet.')); return; }
    tokens.forEach(function (t) {
      var li = el('li');
      li.append(el('strong', {}, t.name), ' ',
        el('span', { className: 'dp-muted' }, 'made ' + when(t.rotated_at || t.created_at) + ', last used ' + when(t.last_used_at)), ' ');
      var rot = el('button', { type: 'button', className: 'btn' }, 'Rotate');
      rot.setAttribute('aria-label', 'Rotate the token ' + t.name);
      rot.addEventListener('click', function () {
        if (!window.confirm('Make a new secret for "' + t.name + '"? The old one stops working now, so update that client.')) return;
        clearError();
        call('POST', '/api/account/mcp-tokens/' + t.id + '/rotate').then(function (b) { showSecret(b.token); load(); })
          .catch(function (e) { fail(e.message); });
      });
      var rev = el('button', { type: 'button', className: 'btn' }, 'Revoke');
      rev.setAttribute('aria-label', 'Revoke the token ' + t.name);
      rev.addEventListener('click', function () {
        if (!window.confirm('Revoke "' + t.name + '"? A client using it gets "not authorized" from its next call.')) return;
        clearError();
        call('POST', '/api/account/mcp-tokens/' + t.id + '/revoke').then(function () { newBox.hidden = true; load(); })
          .catch(function (e) { fail(e.message); });
      });
      // #778: renaming keeps the secret; work credited to this key ("<you>'s <name>") shows the new name.
      var ren = el('button', { type: 'button', className: 'btn' }, 'Rename');
      ren.setAttribute('aria-label', 'Rename the token ' + t.name);
      ren.addEventListener('click', function () {
        var name = window.prompt('New name for "' + t.name + '" (work made with it is credited to this name):', t.name);
        if (name == null) return;
        clearError();
        call('POST', '/api/account/mcp-tokens/' + t.id + '/rename', { name: name.trim() }).then(function () { load(); })
          .catch(function (e) { fail(e.message); });
      });
      li.append(ren, ' ', rot, ' ', rev);
      list.append(li);
    });
  }

  function load() {
    return call('GET', '/api/account/mcp-tokens').then(function (b) { render(b.tokens); })
      .catch(function (e) { list.textContent = ''; fail('Could not load your MCP tokens: ' + e.message); });
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    clearError();
    var name = form.name.value.trim();
    if (!name) { fail('Give the token a name first, e.g. the machine that will use it.'); return; }
    call('POST', '/api/account/mcp-tokens', { name: name }).then(function (b) {
      form.reset();
      showSecret(b.token);
      load();
    }).catch(function (e) { fail(e.message); });
  });
  load();
})();
