// #555: the item page's tag chips (ABOUT > Tags). Each chip can rename its tag everywhere it is used
// (POST /api/tags/{id}/rename) or take it off this item (POST /api/image/{slug}/tags/{id}/remove). Both
// answer with a batch_id for the Undo bar. A refusal (a name another tag has, a card's own tag, a
// viewer) shows the server's message beside the chips; nothing fails silently. Viewers get the buttons
// greyed (is-locked); a click says the editor role is needed (the API refuses them with 403 too).
(function () {
  const box = document.getElementById('item-tag-chips');
  if (!box) return;
  const slug = box.dataset.slug;
  const canEdit = box.dataset.canEdit === '1';
  const status = document.getElementById('item-tag-chips-status');

  function say(text, isError) {
    if (!status) return;
    status.textContent = text || '';
    status.classList.toggle('is-error', !!isError);
  }

  async function post(url, fields) {
    const body = new URLSearchParams(fields || {});
    let res;
    try {
      res = await fetch(url, { method: 'POST', body: body });
    } catch (err) {
      return { ok: false, message: 'Could not reach the server (' + err.message + '). Nothing was changed.' };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const msg = (data && ((data.error && data.error.message) || data.detail)) ||
        ('The server answered ' + res.status + '. Nothing was changed.');
      return { ok: false, message: msg };
    }
    return { ok: true, data: data || {} };
  }

  function done(message, batchId) {
    if (window.showUndoBar) {
      window.showUndoBar(message, batchId, () => window.location.reload());
    } else {
      say(message, false);
      window.location.reload();
    }
  }

  box.addEventListener('click', async (e) => {
    const btn = e.target.closest('.tag-chip-btn');
    if (!btn || !box.contains(btn)) return;
    const chip = btn.closest('.tag-chip');
    const tagId = chip.dataset.tagId;
    const name = chip.dataset.tagName;
    if (!canEdit || btn.classList.contains('is-locked')) {
      say('Changing tags needs the editor role. You are signed in as a viewer: ask an admin for the editor role.', true);
      return;
    }
    say('');
    if (btn.dataset.act === 'rename') {
      const next = window.prompt('Rename the tag "' + name + '" everywhere it is used. New name:', name);
      if (next === null) return;
      const trimmed = next.trim();
      if (!trimmed) { say('A tag name can\'t be empty. Nothing was changed.', true); return; }
      if (trimmed === name) { say('That is already its name. Nothing was changed.', false); return; }
      btn.disabled = true;
      const r = await post('/api/tags/' + encodeURIComponent(tagId) + '/rename', { name: trimmed });
      btn.disabled = false;
      if (!r.ok) { say(r.message, true); return; }
      const n = (r.data.items === 1) ? '1 item' : (r.data.items || 0) + ' items';
      done('Renamed "' + name + '" to "' + trimmed + '" on ' + n + '.', r.data.batch_id);
    } else if (btn.dataset.act === 'remove') {
      btn.disabled = true;
      const r = await post('/api/image/' + encodeURIComponent(slug) + '/tags/' + encodeURIComponent(tagId) + '/remove');
      btn.disabled = false;
      if (!r.ok) { say(r.message, true); return; }
      if (!r.data.batch_id) { say('"' + name + '" was not on this item any more. Nothing was changed.', false); return; }
      chip.remove();
      done('Took "' + name + '" off this item.', r.data.batch_id);
    }
  });
})();
