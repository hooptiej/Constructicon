// #851: /admin's disclosures and Tools rows. Sent to every role; it fetches nothing.
//
//  * Every admin section is a collapsed <details class="admin-section" data-admin-section="key">
//    (the Tools group starts open). Which ones you left open is remembered in localStorage under
//    KEY, as {key: true|false}; a key that was never touched keeps the markup's default. Every
//    storage read and write is in try/catch: with storage blocked (a private window, site data
//    off) the page works exactly the same, it just forgets.
//  * A link or address with a #hash that names a section (or something inside one), such as
//    /admin#admin-vocab-section from the Wallpaper page or the "Finish setting up" banner, opens that
//    section and scrolls to it. That is code opening it, not you, so it is not remembered.
//  * A Tools row with data-tool-trigger clicks the hidden trigger of its drawer (the same ids the
//    nav rail's tabs used). If the trigger is not on the page the row says so in #admin-tools-msg:
//    never a dead click. A greyed (role-locked) row has no trigger here; static/js/role-lock.js
//    explains it.
(function () {
  'use strict';
  var KEY = 'constructicon.admin.sections';

  function load() {
    try {
      var raw = window.localStorage.getItem(KEY);
      var m = raw ? JSON.parse(raw) : {};
      return (m && typeof m === 'object' && !Array.isArray(m)) ? m : {};
    } catch (e) {
      return {};  // storage blocked or the value is garbled: use the defaults
    }
  }
  function save(m) {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(m));
    } catch (e) {
      // storage blocked: nothing to remember it with, and nothing to tell the user either
    }
  }

  var sections = Array.prototype.slice.call(document.querySelectorAll('details.admin-section[data-admin-section]'));
  var last = {};  // key -> the open state we last knew, so a toggle caused by code is not saved
  var stored = load();

  function setOpen(el, open) {
    last[el.getAttribute('data-admin-section')] = open;
    el.open = open;
  }

  sections.forEach(function (el) {
    var k = el.getAttribute('data-admin-section');
    last[k] = el.open;
    if (typeof stored[k] === 'boolean') setOpen(el, stored[k]);
    el.addEventListener('toggle', function () {
      if (el.open === last[k]) return;  // code set it (stored state, a #hash): not a choice
      last[k] = el.open;
      var m = load();  // merge with whatever another tab saved meanwhile
      m[k] = el.open;
      save(m);
    });
  });

  function openById(id) {
    if (!id) return;
    var target = document.getElementById(id);
    if (!target) return;
    var d = target.closest ? target.closest('details.admin-section') : null;
    if (d && !d.open) setOpen(d, true);
    if (target.scrollIntoView) target.scrollIntoView();
  }
  function openForHash() {
    var id = '';
    try {
      id = decodeURIComponent((window.location.hash || '').slice(1));
    } catch (e) {
      id = '';  // a malformed escape in the address: there is no section to open
    }
    openById(id);
  }
  openForHash();
  window.addEventListener('hashchange', openForHash);
  // Following a link to the hash you are already on fires no hashchange.
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href^="#admin-"]') : null;
    if (a) openById(a.getAttribute('href').slice(1));
  });

  var msg = document.getElementById('admin-tools-msg');
  function say(text) {
    if (!msg) return;
    msg.textContent = text;
    msg.hidden = false;
  }
  Array.prototype.forEach.call(document.querySelectorAll('.admin-tool-row[data-tool-trigger]'), function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      // As the rail's tabs did: an outside-click closer on the page must not see this click, or
      // the drawer it opens would be shut again at once.
      e.stopPropagation();
      var trigger = document.getElementById(btn.getAttribute('data-tool-trigger'));
      if (!trigger) {
        say((btn.getAttribute('data-tool-label') || 'This tool') +
          " can't open here: its panel did not load with this page. Reload the page; if it still fails, ask an admin.");
        return;
      }
      if (msg) msg.hidden = true;
      trigger.click();
    });
  });
})();
