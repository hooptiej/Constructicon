// #604 step 3: the Private checkbox and member list on a card or hobby page (templates/_sharing.html).
// Ticking Private reveals the member list right there; the owner (or an admin) adds people as viewer
// (looks only) or editor (can change it), changes a role, or removes someone. Every change is one
// undoable write: the answer's batch_id feeds the Undo bar. A refusal is shown as the server said it
// (what was tried, what happened, what to do) and the checkbox goes back to what it was.
(function () {
  'use strict';

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n[k] = attrs[k]; });
    if (text != null) n.textContent = text;
    return n;
  }

  function messageOf(body, status) {
    var m = body && ((body.error && body.error.message) || body.detail);
    return m || ('The server answered ' + status + ' and gave no reason. Nothing may have changed: reload the page to check.');
  }

  function post(url, fields) {
    var data = new FormData();
    Object.keys(fields).forEach(function (k) { data.append(k, fields[k]); });
    return fetch(url, { method: 'POST', body: data, credentials: 'same-origin' }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (body) {
        if (!r.ok || !body || body.ok === false) throw new Error(messageOf(body, r.status));
        return body;
      });
    }, function (err) {
      throw new Error('Could not reach the server (' + err.message + '). Nothing was changed; try again.');
    });
  }

  function mount(root) {
    var kind = root.dataset.kind, target = root.dataset.target, noun = root.dataset.noun || kind;
    var canManage = root.dataset.canManage === '1';
    var base = '/api/sharing/' + encodeURIComponent(kind) + '/' + encodeURIComponent(target);
    var box = root.querySelector('.cx-sharing-private');
    var note = root.querySelector('.cx-sharing-note');
    var panel = root.querySelector('.cx-sharing-members');
    var list = root.querySelector('.cx-sharing-list');
    var errorBox = root.querySelector('.cx-sharing-error');
    var userSel = root.querySelector('.cx-sharing-user');
    var roleSel = root.querySelector('.cx-sharing-newrole');
    var addBtn = root.querySelector('.cx-sharing-addbtn');
    var state = null, people = null;

    function showError(msg) { errorBox.textContent = msg || ''; }

    function done(body, what) {
      showError('');
      if (body.warnings && body.warnings.length) showError(body.warnings.join(' '));
      if (body.sharing) render(body.sharing);
      if (window.showUndoBar && body.batch_id) window.showUndoBar(what, body.batch_id, function (how) {
        if (how === 'undone') window.location.reload();
      });
    }

    function render(s) {
      state = s;
      box.checked = !!s.private;
      panel.hidden = !s.private;
      note.textContent = '';
      if (s.inherited_from && s.inherited_from.length) {
        note.append('Private · inherited from ');
        s.inherited_from.forEach(function (i, n) {
          var href = i.kind === 'hobby' ? (window.HobbyLabel ? HobbyLabel.url(i.slug) : '#') : '/project/' + encodeURIComponent(i.slug);
          note.append(el('a', { href: href }, i.title));
          if (n < s.inherited_from.length - 1) note.append(', ');
        });
        note.append(". Unticking it here can't make it more open than that.");
      } else if (s.private) {
        note.textContent = 'Only admins, the owner and the members below can see this ' + noun + ' and everything in it.';
      } else {
        note.textContent = 'Everyone signed in can see this ' + noun + '. Tick Private to choose who can.';
      }
      list.textContent = '';
      if (!s.members.length) list.append(el('li', { className: 'cx-sharing-empty dp-muted' }, 'No members yet.'));
      s.members.forEach(function (m) {
        var li = el('li');
        li.dataset.user = m.user_id;
        li.append(el('span', { className: 'cx-sharing-name' }, m.name), ' ');
        if (canManage) {
          var sel = el('select', { className: 'cx-sharing-rolesel' });
          sel.setAttribute('aria-label', 'Role of ' + m.name);
          [['viewer', 'viewer (looks only)'], ['editor', 'editor (can change it)']].forEach(function (o) {
            var opt = el('option', { value: o[0] }, o[1]);
            if (o[0] === m.role) opt.selected = true;
            sel.append(opt);
          });
          sel.addEventListener('change', function () {
            post(base + '/members', { user: m.user_id, role: sel.value })
              .then(function (b) { done(b, m.name + ' is now a ' + sel.value + '.'); })
              .catch(function (e) { sel.value = m.role; showError(e.message); });
          });
          var rm = el('button', { type: 'button', className: 'dp-btn cx-sharing-remove' }, 'Remove');
          rm.setAttribute('aria-label', 'Remove ' + m.name);
          rm.addEventListener('click', function () {
            post(base + '/members/remove', { user: m.user_id })
              .then(function (b) { done(b, m.name + ' was removed.'); })
              .catch(function (e) { showError(e.message); });
          });
          li.append(sel, ' ', rm);
        } else {
          li.append(el('span', { className: 'cx-sharing-role' }, m.role));
        }
        if (m.capped) li.append(el('span', { className: 'dp-muted' }, ' (a viewer on this install, so only looks)'));
        list.append(li);
      });
      fillPeople();
    }

    function fillPeople() {
      if (!canManage || !userSel || panel.hidden) return;
      if (people === null) {
        people = [];
        fetch('/api/sharing/users', { credentials: 'same-origin' }).then(function (r) {
          return r.json().catch(function () { return null; }).then(function (body) {
            if (!r.ok || !Array.isArray(body)) throw new Error(messageOf(body, r.status));
            people = body;
            fillPeople();
          });
        }).catch(function (e) {
          people = null;
          userSel.textContent = '';
          userSel.append(el('option', { value: '' }, 'Could not load the people list'));
          showError('Could not load the list of people to add: ' + e.message);
        });
        return;
      }
      var have = {};
      (state ? state.members : []).forEach(function (m) { have[m.user_id] = true; });
      userSel.textContent = '';
      var left = people.filter(function (p) { return !have[p.id]; });
      userSel.append(el('option', { value: '' }, left.length ? 'Pick someone…' : 'Everyone is already on the list'));
      left.forEach(function (p) {
        userSel.append(el('option', { value: String(p.id) }, p.name + ' (' + p.role + ')'));
      });
    }

    if (canManage) {
      box.addEventListener('change', function () {
        var want = box.checked;
        post(base + '/private', { private: want ? 'true' : 'false' })
          .then(function (b) { done(b, want ? 'Made private.' : 'No longer private.'); })
          .catch(function (e) { box.checked = !want; showError(e.message); });
      });
      if (addBtn) addBtn.addEventListener('click', function () {
        if (!userSel.value) { showError('Pick someone to add first.'); return; }
        var label = userSel.options[userSel.selectedIndex].textContent;
        post(base + '/members', { user: userSel.value, role: roleSel.value })
          .then(function (b) { done(b, label + ' added as ' + roleSel.value + '.'); })
          .catch(function (e) { showError(e.message); });
      });
    }
    fetch(base, { credentials: 'same-origin' }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (body) {
        if (!r.ok || !body || !body.kind) throw new Error(messageOf(body, r.status));
        render(body);
      });
    }).catch(function (e) { showError('Could not load who can see this ' + noun + ': ' + e.message); });
  }

  function init() { document.querySelectorAll('.cx-sharing').forEach(mount); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
