/* The Curator queue (#519), client side. The markup is server-rendered (templates/_curation_queue.html,
   autoescaped); this file only loads it and wires its buttons. No value from the server is ever put
   into the page through innerHTML except that fragment itself.

     CuratorQueue.mount(rootEl, { onCount(open, deferred), isActive() })
       loads GET /api/curator/queue/html (the shell: one collapsed header per card group, with its
       item count) into rootEl, re-loads whenever 'constructicon:needs-changed' fires (not while
       isActive() says the host, e.g. a closed drawer, is hidden: it re-loads on its next reload()),
       and handles:
         toggle       expand / collapse a group; its items are fetched on first expand from
                      GET /api/curator/queue/html?group=<id>&section=open|deferred (#524), and
                      stay expanded across reloads
         accept       resolve the question with its suggested key(s)
         choose       show the options; the form's submit resolves with the picked key(s)
         defer / bring-back / dismiss
       Resolve uses POST /api/pending-decisions/{id}/resolve, the same route the project page's strip uses.
   Updates are announced through an aria-live region; every control is a real button, link or form. */
(function () {
  function say(root, msg) {
    var live = root.querySelector('.cq-live');
    if (!live) return;
    live.textContent = '';
    setTimeout(function () { live.textContent = msg; }, 30);
  }

  function fail(item, msg) {
    var err = item.querySelector('.cq-error');
    if (!err) return;
    err.textContent = msg || '';
    err.hidden = !msg;
  }

  function busy(item, on) {
    item.querySelectorAll('button, input').forEach(function (b) { b.disabled = on; });
  }

  async function post(url, params) {
    var res = await fetch(url, { method: 'POST', body: new URLSearchParams(params) });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      throw new Error((data.error && data.error.message) || data.detail || ('Could not do that (' + res.status + ').'));
    }
    return data;
  }

  function mount(root, opts) {
    opts = opts || {};
    var live = document.createElement('div');
    live.className = 'cq-live dp-sr-label';
    live.setAttribute('aria-live', 'polite');
    live.setAttribute('role', 'status');
    var body = document.createElement('div');
    body.className = 'cq-body';
    root.appendChild(live);
    root.appendChild(body);
    var focusKey = null;
    var expanded = {};   // "section|group id" -> true, for groups the owner has opened
    var stale = false;   // a reload was skipped while the host was hidden
    var loadSeq = 0;

    function groupKey(sec) { return sec.dataset.cqSection + '|' + sec.dataset.cqGroup; }

    // Fetch one group's items into its (already unhidden) body element.
    async function fillGroup(sec) {
      var bodyEl = sec.querySelector('.cq-group-body');
      var url = '/api/curator/queue/html?group=' + encodeURIComponent(sec.dataset.cqGroup) +
        '&section=' + encodeURIComponent(sec.dataset.cqSection);
      bodyEl.setAttribute('aria-busy', 'true');
      try {
        var res = await fetch(url);
        if (!res.ok) throw new Error('status ' + res.status);
        bodyEl.innerHTML = await res.text();
      } catch (e) {
        bodyEl.textContent = 'Could not load this group. Collapse it and open it again to retry.';
        delete expanded[groupKey(sec)];
      } finally {
        bodyEl.removeAttribute('aria-busy');
      }
    }

    function setOpen(sec, on) {
      sec.querySelector('.cq-group-toggle').setAttribute('aria-expanded', on ? 'true' : 'false');
      sec.querySelector('.cq-group-body').hidden = !on;
    }

    async function load() {
      if (opts.isActive && !opts.isActive()) { stale = true; return; }
      stale = false;
      var seq = ++loadSeq;
      try {
        var res = await fetch('/api/curator/queue/html');
        if (!res.ok) return;
        // Build the new shell off-screen, re-open the groups that were open, then swap it in
        // once, so the list never collapses and re-grows under the owner's cursor.
        var next = document.createElement('div');
        next.className = 'cq-body';
        next.innerHTML = await res.text();
        var pending = [];
        next.querySelectorAll('.cq-group').forEach(function (sec) {
          if (expanded[groupKey(sec)]) { setOpen(sec, true); pending.push(fillGroup(sec)); }
        });
        await Promise.all(pending);
        if (seq !== loadSeq) return;   // a newer load superseded this one
        body.replaceChildren.apply(body, Array.prototype.slice.call(next.childNodes));
        var r = body.querySelector('.cq-root');
        if (r && opts.onCount) opts.onCount(parseInt(r.dataset.cqOpen || '0', 10), parseInt(r.dataset.cqDeferred || '0', 10));
        if (focusKey) {
          var again = Array.prototype.find.call(body.querySelectorAll('.cq-item'), function (el) { return el.dataset.cqKey === focusKey; });
          var target = again ? again.querySelector('button, a') : body.querySelector('.cq-group-toggle');
          if (target) target.focus();
          focusKey = null;
        }
      } catch (e) { /* leave whatever is shown */ }
    }

    function changed(msg, nextFocusItem) {
      say(root, msg);
      // Move focus to the neighbouring item so a keyboard user is not dropped at the top.
      var sib = nextFocusItem && (nextFocusItem.nextElementSibling || nextFocusItem.previousElementSibling);
      focusKey = sib && sib.dataset ? sib.dataset.cqKey : null;
      // In place: the item leaves its group right now; the reload below then settles the counts.
      if (nextFocusItem && nextFocusItem.parentNode) {
        var sec = nextFocusItem.closest('.cq-group');
        nextFocusItem.remove();
        if (sec) {
          var left = sec.querySelectorAll('.cq-item').length;
          var meta = sec.querySelector('.cq-group-meta');
          if (meta) meta.textContent = meta.textContent.replace(/^\d+ items?/, left + ' item' + (left === 1 ? '' : 's'));
        }
      }
      document.dispatchEvent(new CustomEvent('constructicon:needs-changed'));
    }

    async function resolve(item, keys) {
      var multi = item.dataset.cqMulti === '1';
      var param = item.dataset.cqParam || 'choice';
      var params = new URLSearchParams();
      if (param === 'project_ids') keys.forEach(function (k) { params.append('project_ids', k); });
      else if (multi) keys.forEach(function (k) { params.append('choices', k); });
      else params.append('choice', keys[0] || '');
      busy(item, true);
      fail(item, '');
      try {
        var res = await fetch('/api/pending-decisions/' + encodeURIComponent(item.dataset.cqId) + '/resolve', { method: 'POST', body: params });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error((data.error && data.error.message) || data.detail || ('Could not apply that answer (' + res.status + ').'));
        changed('Answered. It is out of the queue.', item);
      } catch (e) {
        busy(item, false);
        fail(item, (e && e.message) || 'Network error.');
      }
    }

    async function simple(item, url, msg, param) {
      busy(item, true);
      fail(item, '');
      try {
        var params = {};
        params[param || 'key'] = item.dataset.cqKey;
        await post(url, params);
        changed(msg, item);
      } catch (e) {
        busy(item, false);
        fail(item, (e && e.message) || 'Network error.');
      }
    }

    body.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-cq-action]');
      if (!btn) return;
      if (btn.dataset.cqAction === 'toggle') {
        var group = btn.closest('.cq-group');
        var on = btn.getAttribute('aria-expanded') !== 'true';
        setOpen(group, on);
        if (on) {
          expanded[groupKey(group)] = true;
          if (!group.querySelector('.cq-group-body').childNodes.length) fillGroup(group);
        } else {
          delete expanded[groupKey(group)];
        }
        return;
      }
      var item = btn.closest('.cq-item');
      if (!item) return;
      var action = btn.dataset.cqAction;
      if (action === 'accept') {
        var keys = [];
        try { keys = JSON.parse(item.dataset.cqSuggested || '[]'); } catch (x) { keys = []; }
        resolve(item, keys);
      } else if (action === 'choose') {
        var form = item.querySelector('.cq-chooser');
        if (!form) return;
        var on = form.hidden;
        form.hidden = !on;
        btn.setAttribute('aria-expanded', on ? 'true' : 'false');
        if (on) { var first = form.querySelector('input'); if (first) first.focus(); }
      } else if (action === 'defer') {
        simple(item, '/api/curator/queue/defer', 'Deferred. It moved to the Deferred list.');
      } else if (action === 'bring-back') {
        simple(item, '/api/curator/queue/bring-back', 'Brought back into the queue.');
      } else if (action === 'dismiss') {
        simple(item, '/api/curator/needs/dismiss', 'Dismissed.', 'nudge_key');  // that route names the key nudge_key
      }
    });

    body.addEventListener('submit', function (e) {
      var form = e.target.closest('.cq-chooser');
      if (!form) return;
      e.preventDefault();
      var item = form.closest('.cq-item');
      var keys = Array.prototype.map.call(form.querySelectorAll('input:checked'), function (i) { return i.value; });
      var multi = item.dataset.cqMulti === '1';
      var param = item.dataset.cqParam || 'choice';
      if (!keys.length && param !== 'project_ids') { fail(item, 'Pick an answer first.'); return; }
      resolve(item, multi || param === 'project_ids' ? keys : [keys[0]]);
    });

    document.addEventListener('constructicon:needs-changed', load);
    load();
    return { reload: load, isStale: function () { return stale; } };
  }

  window.CuratorQueue = { mount: mount };
})();
