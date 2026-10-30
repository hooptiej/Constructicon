// #605 "Unpack the Crate": the archive item's "Unpack into a card..." dialog.
// The More menu's button carries data-action-ui="unpack" (object_detail.html); the page's generic
// type-action handler leaves it to this file. Flow: pick a shape (flat collection, flat project, or a
// project with nested cards for folders + a depth), Preview (POST .../action/unpack mode=preview:
// nothing is written), then Unpack (mode=start: a background job), then poll GET /api/unpack/jobs/{id}
// until it finishes; the result names every failure, and Undo reverses the whole conversion
// (POST /api/changes/{batch_id}/undo). Every failure shows the server's own words. No inline handlers
// (CSP, #645/#660): everything is wired here with addEventListener.
(function () {
  const POLL_MS = 1500;

  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'className') n.className = v; else if (k === 'style') n.style.cssText = v; else n.setAttribute(k, v);
    });
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function errText(body, res) {
    return (body && body.error && body.error.message) || (body && body.detail) ||
      `The server answered ${res ? res.status : '?'} with no reason.`;
  }

  async function post(slug, fields) {
    const form = new FormData();
    Object.entries(fields).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') form.append(k, v); });
    const res = await fetch(`/api/image/${encodeURIComponent(slug)}/action/unpack`, { method: 'POST', body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(errText(body, res));
    return body;
  }

  function list(title, rows, fmt, open) {
    const d = el('details', open ? { open: '' } : {});
    d.appendChild(el('summary', {}, `${title} (${rows.length.toLocaleString()})`));
    const ul = el('ul', { style: 'max-height:240px;overflow:auto;margin:6px 0 0 0;padding-left:18px;font-size:13px' });
    rows.forEach(r => ul.appendChild(el('li', {}, fmt(r))));
    d.appendChild(ul);
    return d;
  }

  function build(slug) {
    const backdrop = el('div', { className: 'modal-backdrop', id: 'unpack-modal-backdrop' });
    const modal = el('div', { className: 'modal', style: 'width:640px;max-width:calc(100vw - 32px);max-height:85vh;overflow:auto',
      role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'unpack-title' });
    const header = el('div', { className: 'modal-header' });
    header.appendChild(el('h2', { id: 'unpack-title' }, 'Unpack into a card'));
    const close = el('button', { type: 'button', className: 'modal-close', 'aria-label': 'Close' }, '×');
    header.appendChild(close);
    modal.appendChild(header);

    const intro = el('p', { className: 'dp-note' },
      'Every file inside becomes an item on a new card, through the normal upload pipeline. Junk, links and ' +
      'unsupported files are skipped and listed; files already in the library are linked, not stored twice. ' +
      'Preview first: it writes nothing.');
    modal.appendChild(intro);

    const fs = el('fieldset', { style: 'border:0;padding:0;margin:8px 0' });
    fs.appendChild(el('legend', {}, 'Make it into'));
    const shapes = [
      ['collection', 'A flat collection (recommended for reference material, like a manual)'],
      ['project', 'A flat project'],
      ['nested', 'A project with nested cards for its folders'],
    ];
    shapes.forEach(([v, label], i) => {
      const id = `unpack-shape-${v}`;
      const row = el('div');
      const r = el('input', { type: 'radio', name: 'unpack-shape', id, value: v });
      if (i === 0) r.checked = true;
      row.appendChild(r);
      row.appendChild(el('label', { for: id, style: 'margin-left:6px' }, label));
      fs.appendChild(row);
    });
    const depthRow = el('div', { style: 'margin:4px 0 0 24px' });
    depthRow.appendChild(el('label', { for: 'unpack-depth' }, 'Folder levels that become cards: '));
    const depth = el('select', { id: 'unpack-depth' });
    [['2', '2 (deeper folders fold into their level-2 card)'], ['1', '1 (only the top folders)']].forEach(([v, t]) => {
      depth.appendChild(el('option', { value: v }, t));
    });
    depthRow.appendChild(depth);
    fs.appendChild(depthRow);
    modal.appendChild(fs);

    const buttons = el('div', { style: 'display:flex;gap:8px;margin:8px 0' });
    const previewBtn = el('button', { type: 'button', className: 'dp-btn' }, 'Preview');
    const startBtn = el('button', { type: 'button', className: 'dp-btn' }, 'Unpack');
    const undoBtn = el('button', { type: 'button', className: 'dp-btn', hidden: '' }, 'Undo the whole conversion');
    startBtn.disabled = true;
    buttons.append(previewBtn, startBtn, undoBtn);
    modal.appendChild(buttons);

    const status = el('p', { role: 'status', 'aria-live': 'polite', style: 'margin:6px 0' });
    const bar = el('progress', { max: '1', value: '0', style: 'width:100%', hidden: '' });
    const out = el('div');
    modal.append(status, bar, out);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    let previewed = null; // the shape/depth the shown preview is for
    let timer = null;
    let job = null;

    const choice = () => {
      const shape = (modal.querySelector('input[name="unpack-shape"]:checked') || {}).value || 'collection';
      return { shape, depth: shape === 'nested' ? depth.value : '' };
    };
    const syncDepth = () => { depth.disabled = choice().shape !== 'nested'; };
    const stale = () => {
      const c = choice();
      startBtn.disabled = !(previewed && previewed.shape === c.shape && previewed.depth === c.depth) || !!job;
    };
    modal.querySelectorAll('input[name="unpack-shape"]').forEach(r => r.addEventListener('change', () => { syncDepth(); stale(); }));
    depth.addEventListener('change', stale);
    syncDepth();

    function showError(msg) {
      status.textContent = msg;
      status.style.color = 'var(--danger, #e06c5a)';
    }
    function showInfo(msg) {
      status.textContent = msg;
      status.style.color = '';
    }

    function renderPreview(p) {
      out.replaceChildren();
      const t = p.totals || {};
      out.appendChild(el('p', {}, p.message || ''));
      const facts = el('ul', { style: 'font-size:13px;margin:4px 0 8px 18px' });
      facts.appendChild(el('li', {}, `${(t.files || 0).toLocaleString()} files, ${((t.bytes || 0) / 1048576).toFixed(1)} MB unpacked, ${t.folders || 0} folders, ${p.max_depth || 0} levels deep (${p.format})`));
      const kinds = Object.entries(p.by_type || {}).slice(0, 8).map(([k, n]) => `${n.toLocaleString()} ${k}`).join(', ');
      if (kinds) facts.appendChild(el('li', {}, `By type: ${kinds}`));
      out.appendChild(facts);
      if (p.cards && p.cards.length) {
        out.appendChild(list('Cards it would make', p.cards, c => `${c.path ? c.path + ' → ' : ''}${c.title} (${c.kind}, ${c.files} file${c.files === 1 ? '' : 's'})`, p.cards.length <= 12));
      }
      if (p.skipped && p.skipped.length) out.appendChild(list('Skipped, and why', p.skipped, s => `${s.path}: ${s.reason}`, true));
      const linked = (p.members || []).filter(m => m.action !== 'ingest');
      if (linked.length) out.appendChild(list('Already in the library (linked, not stored again)', linked, m => `${m.path}: ${(m.notes || []).join('; ')}`, true));
      const noted = (p.members || []).filter(m => m.action === 'ingest' && m.notes && m.notes.length);
      if (noted.length) out.appendChild(list('Worth knowing', noted, m => `${m.path}: ${m.notes.join('; ')}`, true));
      out.appendChild(list('Every member', p.members || [], m => `${m.path} (${m.type_label || ''}${m.card ? ', on ' + m.card : ''})`, false));
      if (p.members_truncated) out.appendChild(el('p', { className: 'dp-muted' }, 'The member list is cut short; the counts above are whole.'));
    }

    function renderJob(j) {
      const p = j.progress || {};
      if (!j.finished) {
        bar.hidden = false;
        bar.max = Math.max(p.total || 1, 1);
        bar.value = p.done || 0;
        showInfo(j.message || 'Working.');
        return;
      }
      bar.hidden = true;
      clearInterval(timer);
      timer = null;
      const r = j.result || {};
      out.replaceChildren();
      if (j.status === 'failed') showError(j.message); else showInfo(j.message);
      if (r.card) {
        const a = el('a', { href: r.card.url }, `Open the card: ${r.card.title}`);
        out.appendChild(el('p')).appendChild(a);
      }
      if (r.failed && r.failed.length) out.appendChild(list('Failed, and why', r.failed, f => `${f.path}: ${f.reason}`, true));
      if (r.skipped && r.skipped.length) out.appendChild(list('Skipped, and why', r.skipped, s => `${s.path}: ${s.reason}`, false));
      if (r.linked && r.linked.length) out.appendChild(list('Linked to items already in the library', r.linked, l => `${l.path}: ${l.why || ''}`, false));
      if (r.batch_id) undoBtn.hidden = false;
    }

    async function poll() {
      try {
        const res = await fetch(`/api/unpack/jobs/${encodeURIComponent(job.id)}`);
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(errText(body, res));
        job = body.job;
        renderJob(job);
      } catch (e) {
        showError(`Couldn't read the job's progress: ${e.message}. It keeps running on the server; reopen this page to check.`);
      }
    }

    previewBtn.addEventListener('click', async () => {
      const c = choice();
      previewBtn.disabled = true;
      startBtn.disabled = true;
      showInfo('Reading the archive (nothing is written)…');
      out.replaceChildren();
      try {
        const body = await post(slug, { mode: 'preview', shape: c.shape, depth: c.depth });
        renderPreview(body.preview);
        previewed = c;
        showInfo('Preview ready. Nothing has been written.');
      } catch (e) {
        previewed = null;
        showError(e.message);
      }
      previewBtn.disabled = !!job;
      stale();
    });

    startBtn.addEventListener('click', async () => {
      const c = choice();
      startBtn.disabled = true;
      previewBtn.disabled = true;
      showInfo('Starting…');
      try {
        const body = await post(slug, { mode: 'start', shape: c.shape, depth: c.depth });
        job = body.job;
        modal.querySelectorAll('input[name="unpack-shape"]').forEach(r => { r.disabled = true; });
        depth.disabled = true;
        renderJob(job);
        timer = setInterval(poll, POLL_MS);
      } catch (e) {
        showError(e.message);
        previewBtn.disabled = false;
        stale();
      }
    });

    undoBtn.addEventListener('click', async () => {
      const batch = job && job.result && job.result.batch_id;
      if (!batch) return;
      if (!confirm('Undo the whole conversion? The card(s) go, and every item it made goes to the trash (7 days).')) return;
      undoBtn.disabled = true;
      try {
        const res = await fetch(`/api/changes/${encodeURIComponent(batch)}/undo`, { method: 'POST' });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(errText(body, res));
        undoBtn.hidden = true;
        out.replaceChildren();
        showInfo('Undone: the card(s) are gone and the items it made are in the trash. The archive is as it was.');
      } catch (e) {
        undoBtn.disabled = false;
        showError(`Undo failed: ${e.message}`);
      }
    });

    const hide = () => { backdrop.classList.remove('active'); };
    close.addEventListener('click', hide);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) hide(); });
    return { show: () => backdrop.classList.add('active') };
  }

  let dialog = null;
  document.querySelectorAll('.type-action-btn[data-action-ui="unpack"]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      if (!dialog) dialog = build(btn.dataset.slug);
      dialog.show();
    });
  });
})();
