/* #554 (batch 5): the cover pick, the same control on every page (templates/_cover_pick.html draws it,
   static/js/cards.js draws the same markup inside the item grids).

   The page embeds one JSON config block (id "cover-pick-config", templates/_cover_pick.html):
     endpoint     where a pick is POSTed (/api/projects/<id>/cover, /api/hobby/<id>/cover)
     file_field   the form field for a file pick (slug | cover_slug)
     card_field   the form field for a card pick (card | cover_card)
     current      {kind: "item" | "card", ref} or null: what is ticked now
     can_edit     false for a viewer: every pick is greyed (aria-disabled) and a click says why
     locked_message   what that click says
     target       "card" | "hobby" (how the answer names the new cover)
   One cover at a time: after a pick every .cx-cover-pick on the page is re-synced to `current`
   (a file can show in a pile's fan AND in the All files grid). A refusal puts the old pick back and
   shows the server's message (what, why, what to do) in the bar; a success offers Undo. A
   [data-cover-auto] button posts an empty pick (a hobby's "Back to the automatic cover").
   window.CoverPick.current() is what the item grid reads when it draws a page of cards. */
(function () {
  'use strict';
  var cfgEl = document.getElementById('cover-pick-config');
  if (!cfgEl) return;
  var cfg;
  try { cfg = JSON.parse(cfgEl.textContent); } catch (e) {
    console.error('cover-pick: the page config is not valid JSON', e);
    return;
  }
  var current = cfg.current || null;
  var busy = false;

  function bar(message, batchId, done) {
    if (window.showUndoBar) window.showUndoBar(message, batchId || null, done || null);
    else window.alert(message);
  }
  function picks() { return document.querySelectorAll('input.cx-cover-pick'); }
  function matches(r) {
    return !!current && r.dataset.coverKind === current.kind && String(r.dataset.coverRef) === String(current.ref);
  }
  function sync() { picks().forEach(function (r) { r.checked = matches(r); }); }

  function newCoverUrl(body) {
    if (cfg.target === 'hobby') return body.cover && body.cover.cover_url;
    return body.cover ? '/f/' + encodeURIComponent(body.cover) + '/thumb' : null;
  }
  // The page's own card shows the new cover at once (the rest of the page is right after a reload).
  function showOnHeader(url) {
    var art = document.querySelector('.detail-header-card .cx-art');
    if (!art || !url) return;
    var img = art.querySelector('img');
    if (!img) {
      art.textContent = '';
      img = document.createElement('img');
      img.alt = '';
      art.appendChild(img);
    }
    img.src = url;
  }

  async function post(fields) {
    var fd = new FormData();
    Object.keys(fields).forEach(function (k) { fd.append(k, fields[k]); });
    var res = await fetch(cfg.endpoint, { method: 'POST', body: fd });
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      throw new Error((body.error && body.error.message) || body.detail ||
                      "Couldn't change the cover (HTTP " + res.status + "). Nothing was changed.");
    }
    return body;
  }

  async function pick(r) {
    var before = current;
    var kind = r.dataset.coverKind === 'card' ? 'card' : 'item';
    var fields = {};
    fields[kind === 'card' ? cfg.card_field : cfg.file_field] = r.dataset.coverRef;
    current = { kind: kind, ref: r.dataset.coverRef };
    sync();
    busy = true;
    try {
      var body = await post(fields);
      if (!body.batch_id) { bar('That is already the cover.'); return; }
      showOnHeader(newCoverUrl(body));
      var title = r.dataset.coverTitle || 'It';
      bar(kind === 'card' ? 'Borrowing the cover of "' + title + '" (it follows that card\'s cover).'
                          : '"' + title + '" is the cover now.',
          body.batch_id, function (how) { if (how === 'undone') window.location.reload(); });
    } catch (err) {
      current = before;
      sync();
      bar(err.message);
    } finally {
      busy = false;
    }
  }

  // A greyed pick: the click (also a click on its label) never ticks it, and says why.
  document.addEventListener('click', function (e) {
    var t = e.target;
    var locked = t.closest && t.closest('.cx-cover-pick.is-locked, .cx-cover-pick-row.is-locked, [data-cover-auto].is-locked');
    if (locked) {
      e.preventDefault();
      bar(cfg.locked_message || 'Picking the cover needs the editor role.');
      return;
    }
    var auto = t.closest && t.closest('[data-cover-auto]');
    if (auto && !busy) {
      e.preventDefault();
      var fields = {};
      fields[cfg.file_field] = '';
      fields[cfg.card_field] = '';
      post(fields).then(function (body) {
        current = null;
        sync();
        auto.hidden = true;
        showOnHeader(newCoverUrl(body));
        bar('Back to the automatic cover.', body.batch_id, function (how) { if (how === 'undone') window.location.reload(); });
      }, function (err) { bar(err.message); });
    }
  });

  document.addEventListener('change', function (e) {
    var r = e.target;
    if (!(r.matches && r.matches('input.cx-cover-pick')) || !r.checked) return;
    if (busy) { sync(); return; }
    pick(r);
  });

  window.CoverPick = { current: function () { return current; }, sync: sync };
  sync();
})();
