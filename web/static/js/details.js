/* Details panel behaviour for the project and item pages (#515). Markup: templates/project_detail.html
   and object_detail.html (section.dp-group > header + .dp-view + form.dp-edit). Styles: css/details.css.

   Each group is a fact sheet until its Edit button is pressed; then only that group shows its form
   (one group at a time), focus moves into it, and Save / Cancel appear. A page registers what Save does:

     DetailsPanel.register('status', {
       save: async () => {...},     // call the same endpoints the old controls called; throw Error(msg) to show it inline
       cancel: () => {...},         // optional: undo any JS-held state (native controls are reset for you)
       doneOnly: true               // optional: the group's controls act immediately; the button reads "Done"
     });

   A successful Save reloads the page (the fact sheets are rendered by the server, so they are always true).
   Anything that changed immediately (a removed chip) marks the group dirty so Cancel/Done reload too.
   DetailsPanel.edit(name) opens a group from other code; DetailsPanel.markDirty(name) flags it.

   Also here: the More menu (button + popover, aria-expanded, Esc / outside click / arrow keys). */
(function () {
  var handlers = {};
  var dirty = {};
  var current = null;
  var FOCUS_KEY = 'dp-focus';

  function groupEl(name) { return document.querySelector('.dp-group[data-group="' + name + '"]'); }
  function parts(g) {
    return {
      btn: g.querySelector('.dp-edit-btn'),
      view: g.querySelector('.dp-view'),
      form: g.querySelector('.dp-edit'),
      err: g.querySelector('.dp-error'),
      save: g.querySelector('.dp-save'),
      cancel: g.querySelector('.dp-cancel')
    };
  }

  function say(msg) {
    var live = document.getElementById('dp-status-live');
    if (live) { live.textContent = ''; setTimeout(function () { live.textContent = msg; }, 30); }
  }

  function showError(name, msg) {
    var g = groupEl(name); if (!g) return;
    var p = parts(g);
    if (p.err) p.err.textContent = msg || '';
  }

  function open(name) {
    var g = groupEl(name); if (!g) return;
    if (current && current !== name) closeGroup(current, true);
    var p = parts(g);
    if (!p.form) return;
    current = name;
    g.setAttribute('data-mode', 'edit');
    p.view.hidden = true;
    p.form.hidden = false;
    p.btn.setAttribute('aria-expanded', 'true');
    showError(name, '');
    var h = handlers[name];
    if (h && h.onEdit) h.onEdit();
    var first = p.form.querySelector('select, input:not([type="hidden"]), textarea, button:not(.dp-cancel):not(.dp-save)') || p.form.querySelector('button');
    if (first) first.focus();
  }

  function closeGroup(name, skipFocus) {
    var g = groupEl(name); if (!g) return;
    var p = parts(g);
    var h = handlers[name];
    if (p.form) { try { p.form.reset(); } catch (e) { /* ignore */ } }
    if (h && h.cancel) h.cancel();
    if (dirty[name]) { location.reload(); return; }
    g.removeAttribute('data-mode');
    if (p.view) p.view.hidden = false;
    if (p.form) p.form.hidden = true;
    if (p.btn) { p.btn.setAttribute('aria-expanded', 'false'); if (!skipFocus) p.btn.focus(); }
    showError(name, '');
    if (current === name) current = null;
  }

  async function save(name) {
    var g = groupEl(name); if (!g) return;
    var p = parts(g);
    var h = handlers[name] || {};
    showError(name, '');
    if (h.doneOnly || !h.save) { closeGroup(name); return; }
    if (p.save) p.save.disabled = true;
    try {
      await h.save();
    } catch (e) {
      showError(name, (e && e.message) || 'Could not save.');
      if (p.save) p.save.disabled = false;
      return;
    }
    try { sessionStorage.setItem(FOCUS_KEY, name); } catch (e) { /* storage may be blocked */ }
    location.reload();
  }

  function init() {
    document.querySelectorAll('.dp-group').forEach(function (g) {
      var name = g.getAttribute('data-group');
      var p = parts(g);
      if (!p.btn || !p.form) return;
      p.btn.addEventListener('click', function () {
        if (g.getAttribute('data-mode') === 'edit') closeGroup(name); else open(name);
      });
      p.form.addEventListener('submit', function (e) { e.preventDefault(); save(name); });
      if (p.cancel) p.cancel.addEventListener('click', function () { closeGroup(name); });
      g.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && g.getAttribute('data-mode') === 'edit' && !e.defaultPrevented) {
          e.preventDefault(); closeGroup(name);
        }
      });
    });
    try {
      var name = sessionStorage.getItem(FOCUS_KEY);
      if (name) {
        sessionStorage.removeItem(FOCUS_KEY);
        var g = groupEl(name);
        var b = g && g.querySelector('.dp-edit-btn');
        if (b) { b.focus(); say('Saved.'); }
      }
    } catch (e) { /* ignore */ }
  }

  // ---- More menu ----
  function initMenus() {
    document.querySelectorAll('[data-dp-more]').forEach(function (wrap) {
      var btn = wrap.querySelector('.dp-more-btn');
      var menu = wrap.querySelector('.dp-menu');
      if (!btn || !menu) return;
      function items() { return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]:not([disabled])')); }
      function setOpen(on, focusBtn) {
        menu.hidden = !on;
        btn.setAttribute('aria-expanded', on ? 'true' : 'false');
        if (on) { var it = items()[0]; if (it) it.focus(); }
        else if (focusBtn) btn.focus();
      }
      btn.addEventListener('click', function () { setOpen(menu.hidden); });
      menu.addEventListener('click', function (e) { if (e.target.closest('[role="menuitem"]')) setOpen(false, false); });
      wrap.addEventListener('keydown', function (e) {
        if (menu.hidden) return;
        var list = items(), i = list.indexOf(document.activeElement);
        if (e.key === 'Escape') { e.preventDefault(); setOpen(false, true); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); (list[i + 1] || list[0]).focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); (list[i - 1] || list[list.length - 1]).focus(); }
        else if (e.key === 'Tab') { setOpen(false, false); }
      });
      document.addEventListener('click', function (e) { if (!menu.hidden && !wrap.contains(e.target)) setOpen(false, false); });
    });
  }

  // ---- "Needs your input" strip: Accept / Choose... ----
  // Both resolve through POST /api/pending-decisions/{id}/resolve (the same route the admin queue
  // uses). Accept sends the decision's suggested answer; Choose... offers the options inline for a
  // single-pick question, or links to the admin queue for a multi-pick one. A refusal (a CardError,
  // 409/422) is shown in the strip and the decision stays open.
  function initNeeds() {
    document.querySelectorAll('.dp-needs[data-decision-id]').forEach(function (strip) {
      var id = strip.getAttribute('data-decision-id');
      var multi = strip.getAttribute('data-multi') === '1';
      var suggested = [];
      try { suggested = JSON.parse(strip.getAttribute('data-suggested') || '[]'); } catch (e) { suggested = []; }
      var err = strip.querySelector('.dp-needs-error');
      var accept = strip.querySelector('.dp-needs-accept');
      var choose = strip.querySelector('.dp-needs-choose');
      var chooser = strip.querySelector('.dp-needs-chooser');
      var apply = strip.querySelector('.dp-needs-apply');
      var pick = strip.querySelector('.dp-needs-pick');
      function fail(msg) { err.textContent = msg; err.hidden = !msg; }
      async function resolve(keys) {
        fail('');
        var body = new URLSearchParams();
        if (multi) keys.forEach(function (k) { body.append('choices', k); });
        else body.append('choice', keys[0] || '');
        var buttons = strip.querySelectorAll('button');
        buttons.forEach(function (b) { b.disabled = true; });
        try {
          var res = await fetch('/api/pending-decisions/' + encodeURIComponent(id) + '/resolve', { method: 'POST', body: body });
          var data = await res.json().catch(function () { return {}; });
          if (!res.ok) {
            fail((data.error && data.error.message) || data.detail || ('Could not apply that answer (' + res.status + ').'));
            buttons.forEach(function (b) { b.disabled = false; });
            return;
          }
          location.reload();
        } catch (e) {
          fail('Network error.');
          buttons.forEach(function (b) { b.disabled = false; });
        }
      }
      if (accept) accept.addEventListener('click', function () { resolve(suggested); });
      if (choose && chooser) {
        choose.addEventListener('click', function () {
          var on = chooser.hidden;
          chooser.hidden = !on;
          choose.setAttribute('aria-expanded', on ? 'true' : 'false');
          if (on && pick) pick.focus();
        });
        if (apply && pick) apply.addEventListener('click', function () { if (pick.value) resolve([pick.value]); });
      }
    });
  }

  window.DetailsPanel = {
    register: function (name, opts) { handlers[name] = opts || {}; },
    edit: open,
    close: closeGroup,
    markDirty: function (name) { dirty[name] = true; },
    error: showError,
    say: say
  };
  function boot() { init(); initMenus(); initNeeds(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
