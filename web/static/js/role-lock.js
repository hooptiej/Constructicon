// #779: role-locked controls, the one shared behaviour (server side: web/role_lock.py).
// Owner rule: "a permission never makes a feature invisible." A control the user's role can't use
// is drawn greyed (.role-locked), carries data-locked-role + data-locked-message, and a click (or
// Enter / Space) shows that message in a small note beside it instead of doing anything. Never a dead
// button. Escape, a click elsewhere or clicking it again closes the note. One delegated listener for
// the whole page (capture phase, so a locked control's click never reaches another handler), so
// controls built later by JS work too. textContent only.
//
//   RoleLock.decorate(el, need, message, title)  make a JS-built control locked (the bulk bar)
//   RoleLock.show(el)                            show the note for a locked control
(function () {
  'use strict';
  var note = null;
  var openFor = null;

  function ensureNote() {
    if (note) return note;
    note = document.createElement('div');
    note.className = 'role-locked-msg';
    note.id = 'role-locked-msg';
    note.setAttribute('role', 'status');
    note.setAttribute('aria-live', 'polite');
    note.hidden = true;
    document.body.appendChild(note);
    return note;
  }

  function close() {
    if (!openFor) return;
    openFor.removeAttribute('aria-describedby');
    note.hidden = true;
    openFor = null;
  }

  function show(el) {
    var n = ensureNote();
    n.textContent = el.getAttribute('data-locked-message') || 'This needs a role your account does not have.';
    n.hidden = false;
    var r = el.getBoundingClientRect();
    var h = n.offsetHeight, w = n.offsetWidth;
    var top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);
    var left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - w - 8));
    n.style.top = top + 'px';
    n.style.left = left + 'px';
    el.setAttribute('aria-describedby', n.id);
    openFor = el;
  }

  function decorate(el, need, message, title) {
    el.classList.add('role-locked');
    el.setAttribute('aria-disabled', 'true');
    el.setAttribute('data-locked-role', need);
    el.setAttribute('data-locked-message', message);
    if (title) el.setAttribute('title', title);
    return el;
  }

  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('.role-locked[data-locked-message]') : null;
    if (!el) {
      if (openFor && note && !note.contains(e.target)) close();
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    if (openFor === el) { close(); return; }
    close();
    show(el);
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && openFor) { var b = openFor; close(); if (b.focus) b.focus(); }
  });
  window.addEventListener('resize', close);

  window.RoleLock = { decorate: decorate, show: show, close: close };
})();
