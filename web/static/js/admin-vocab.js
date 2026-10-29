// #556: Admin > Word lists (brand roles + wallpaper tags), core/vocab.py over /api/vocab/<list>.
// The server is the source of truth: every write answers the whole list (with item counts) plus
// its batch_id, which feeds the Undo bar (undo-bar.js). Every refusal shows the server's message.
(function () {
  const VOCABS = ['brand_role', 'wallpaper_tag'];
  const status = document.getElementById('vocab-status');
  if (!status) return;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function say(text, isError) {
    status.textContent = text || '';
    status.style.color = isError ? '#b00020' : '';
  }

  function render(vocab, terms) {
    const ul = document.getElementById(`vocab-items-${vocab}`);
    ul.innerHTML = terms.map((t, i) => {
      const term = escapeHtml(t.term);
      const n = typeof t.items === 'number' ? t.items : null;
      const count = n === null ? '' : `<code class="prov-key">${n} item${n === 1 ? '' : 's'}</code>`;
      const merge = vocab === 'brand_role'
        ? `<button type="button" class="btn-ms" data-act="merge" aria-label="Merge ${term} into another role">Merge…</button>` : '';
      return `<li class="prov-item" data-term="${term}">
        <span class="prov-label">${term}</span>${count}
        <button type="button" class="btn-ms" data-act="up" aria-label="Move ${term} up"${i === 0 ? ' disabled' : ''}>&uarr;</button>
        <button type="button" class="btn-ms" data-act="down" aria-label="Move ${term} down"${i === terms.length - 1 ? ' disabled' : ''}>&darr;</button>
        <button type="button" class="btn-ms" data-act="rename" aria-label="Rename ${term}">Rename</button>
        ${merge}
        <button type="button" class="btn-ms" data-act="remove" aria-label="Remove ${term}">Remove</button>
      </li>`;
    }).join('');
  }

  async function call(vocab, path, fields, preview) {
    const res = await fetch(`/api/vocab/${encodeURIComponent(vocab)}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(fields || {}),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (body.error && body.error.message) || body.detail;
      throw new Error(typeof msg === 'string' ? msg : `That failed (HTTP ${res.status}). Nothing was changed.`);
    }
    if (!preview) render(vocab, body.terms || []);  // a dry run's list is the would-be one
    return body;
  }

  function done(body, message) {
    say(message);
    if (body.batch_id && window.showUndoBar) {
      window.showUndoBar(message, body.batch_id, (how) => { if (how === 'undone') load(); });
    }
  }

  async function load() {
    for (const vocab of VOCABS) {
      try {
        const res = await fetch(`/api/vocab/${vocab}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        render(vocab, (await res.json()).terms || []);
      } catch (e) {
        say(`Could not load the ${vocab === 'brand_role' ? 'brand roles' : 'wallpaper tags'} (${e.message}). Reload to try again.`, true);
      }
    }
  }

  VOCABS.forEach((vocab) => {
    document.getElementById(`vocab-items-${vocab}`).addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button[data-act]');
      if (!btn) return;
      const term = btn.closest('li').dataset.term;
      const act = btn.dataset.act;
      try {
        if (act === 'up' || act === 'down') {
          const body = await call(vocab, '/move', { term, direction: act });
          say('');
          if (body.batch_id && window.showUndoBar) window.showUndoBar(`Moved ${term}.`, body.batch_id, (how) => { if (how === 'undone') load(); });
        } else if (act === 'rename') {
          const next = prompt(`New name for "${term}"`, term);
          if (next === null) return;
          const body = await call(vocab, '/rename', { term, new_term: next });
          const extra = vocab === 'brand_role'
            ? ` ${body.items_moved || 0} item${body.items_moved === 1 ? '' : 's'} moved with it.`
            : ` The Wallpaper page shows ${body.wallpapers_after} (was ${body.wallpapers_before}).`;
          done(body, body.batch_id ? `Renamed ${term} to ${body.term}.${extra}` : (body.warnings || [])[0] || 'Nothing changed.');
        } else if (act === 'merge') {
          const into = prompt(`Merge "${term}" into which brand role? Its items take that role, and "${term}" leaves the list.`, '');
          if (into === null) return;
          const body = await call(vocab, '/merge', { term, into });
          done(body, `Merged ${term} into ${body.into}: ${body.items_moved} item${body.items_moved === 1 ? '' : 's'} moved.`);
        } else if (act === 'remove') {
          if (vocab === 'wallpaper_tag') {
            const preview = await call(vocab, '/remove', { term, dry_run: 'true' }, true);
            const n = preview.wallpapers_leaving;
            if (!confirm(`Remove "${term}"? ${n} picture${n === 1 ? '' : 's'} will leave the Wallpaper page (their tags stay).`)) return;
          }
          const body = await call(vocab, '/remove', { term });
          const extra = vocab === 'wallpaper_tag'
            ? ` ${body.wallpapers_leaving} picture${body.wallpapers_leaving === 1 ? '' : 's'} left the Wallpaper page.` : '';
          done(body, `Removed ${term}.${extra}`);
        }
      } catch (e) {
        say(e.message, true);
        load();
      }
    });
  });

  document.querySelectorAll('.vocab-add').forEach((form) => {
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const vocab = form.dataset.vocab;
      const input = document.getElementById(`vocab-add-${vocab}`);
      try {
        const body = await call(vocab, '', { term: input.value });
        input.value = '';
        done(body, `Added ${body.term} (${body.items} item${body.items === 1 ? '' : 's'} already ${vocab === 'brand_role' ? 'have that role' : 'carry that tag'}).`);
      } catch (e) { say(e.message, true); }
    });
  });

  load();
})();
