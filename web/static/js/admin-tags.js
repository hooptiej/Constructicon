// #555: Admin > Tags. The tag tree (GET /api/tags/tree) with rename / merge / move / delete per tag,
// and the cleanup sweep (GET /api/tags/sweep), which only lists candidates: the owner ticks the ones to
// delete and confirms (POST /api/tags/sweep/apply). Every write answers with a batch_id for the Undo bar;
// every refusal shows the server's own message (what was tried, that nothing changed, what to do).
// Everything user-visible is escaped; the hobby word comes from HobbyLabel (#700).
(function () {
  const treeEl = document.getElementById('admin-tags-tree');
  if (!treeEl) return;
  const statusEl = document.getElementById('admin-tags-status');
  const countEl = document.getElementById('admin-tags-count');
  const filterEl = document.getElementById('admin-tags-filter');
  const sweepBtn = document.getElementById('admin-tags-sweep-btn');
  const sweepEl = document.getElementById('admin-tags-sweep');
  const label = window.HobbyLabel;  // base.html loads hobby-label.js before every other script
  let tree = [];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function say(text, isError) {
    statusEl.textContent = text || '';
    statusEl.style.color = isError ? '#b00020' : '';
  }

  async function getJson(url) {
    const res = await fetch(url);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      throw new Error((data && ((data.error && data.error.message) || data.detail)) || ('The server answered ' + res.status + '.'));
    }
    return data;
  }

  async function post(url, fields) {
    const body = new URLSearchParams();
    Object.keys(fields || {}).forEach((k) => {
      const v = fields[k];
      if (Array.isArray(v)) v.forEach((x) => body.append(k, x)); else body.append(k, v);
    });
    let res;
    try {
      res = await fetch(url, { method: 'POST', body: body });
    } catch (err) {
      return { ok: false, message: 'Could not reach the server (' + err.message + '). Nothing was changed.' };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      return { ok: false, message: (data && ((data.error && data.error.message) || data.detail)) ||
        ('The server answered ' + res.status + '. Nothing was changed.') };
    }
    return { ok: true, data: data || {} };
  }

  function after(r, message) {
    if (!r.ok) { say(r.message, true); return; }
    if (!r.data.batch_id) {
      say((r.data.warnings && r.data.warnings[0]) || 'Nothing changed.', false);
      return;
    }
    say(message, false);
    if (window.showUndoBar) window.showUndoBar(message, r.data.batch_id, () => { loadTree(); if (sweepEl.dataset.loaded) loadSweep(); });
    loadTree();
    if (sweepEl.dataset.loaded) loadSweep();
  }

  function count(nodes) {
    return nodes.reduce((n, t) => n + 1 + count(t.children || []), 0);
  }

  function nodeHtml(t) {
    const badges = [];
    badges.push(`<span class="admin-tag-count">${t.items === 1 ? '1 item' : t.items + ' items'}</span>`);
    if (t.hobby) badges.push(`<span class="admin-tag-badge">${esc(label.one)}</span>`);
    (t.cards || []).forEach((c) => badges.push(`<a class="admin-tag-badge" href="/project/${encodeURIComponent(c.slug)}">Card: ${esc(c.title)}</a>`));
    const kids = (t.children || []).length ? `<ul>${t.children.map(nodeHtml).join('')}</ul>` : '';
    return `<li class="admin-tag" data-id="${t.id}" data-name="${esc(t.name)}" data-path="${esc(t.path.toLowerCase())}">
      <div class="admin-tag-row"><span class="admin-tag-name" title="${esc(t.path)}">${esc(t.name)}</span> ${badges.join(' ')}
        <span class="admin-tag-actions">
          <button type="button" class="btn-ms" data-act="rename">Rename</button>
          <button type="button" class="btn-ms" data-act="merge">Merge into&hellip;</button>
          <button type="button" class="btn-ms" data-act="move">Move&hellip;</button>
          <button type="button" class="btn-ms" data-act="delete">Delete</button>
        </span></div>${kids}</li>`;
  }

  function applyFilter() {
    const q = (filterEl.value || '').trim().toLowerCase();
    treeEl.querySelectorAll('li.admin-tag').forEach((li) => {
      li.hidden = !!q && !li.dataset.path.includes(q) && !li.querySelector(`li.admin-tag[data-path*="${CSS.escape(q)}"]`);
    });
  }

  async function loadTree() {
    try {
      tree = await getJson('/api/tags/tree');
    } catch (err) {
      treeEl.innerHTML = `<li class="admin-row-hint">Couldn't load the tags: ${esc(err.message)}</li>`;
      return;
    }
    countEl.textContent = String(count(tree));
    treeEl.innerHTML = tree.length ? tree.map(nodeHtml).join('') : '<li class="admin-row-hint">No tags yet.</li>';
    applyFilter();
  }

  treeEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const li = btn.closest('li.admin-tag');
    const id = li.dataset.id;
    const name = li.dataset.name;
    say('');
    if (btn.dataset.act === 'rename') {
      const next = window.prompt('Rename "' + name + '" everywhere it is used. New name:', name);
      if (next === null || !next.trim() || next.trim() === name) return;
      after(await post(`/api/tags/${id}/rename`, { name: next.trim() }), `Renamed "${name}" to "${next.trim()}".`);
    } else if (btn.dataset.act === 'merge') {
      const into = window.prompt('Merge "' + name + '" into which tag? Its items and sub-tags move there, then "' + name + '" is removed. Type the other tag\'s name or id:', '');
      if (into === null || !into.trim()) return;
      after(await post(`/api/tags/${id}/merge`, { into: into.trim() }), `Merged "${name}" into "${into.trim()}".`);
    } else if (btn.dataset.act === 'move') {
      const parent = window.prompt('Move "' + name + '" under which tag? Type its name or id, or leave it blank for the top level:', '');
      if (parent === null) return;
      after(await post(`/api/tags/${id}/move`, { parent: parent.trim() }),
        parent.trim() ? `Moved "${name}" under "${parent.trim()}".` : `Moved "${name}" to the top level.`);
    } else if (btn.dataset.act === 'delete') {
      if (!window.confirm('Delete the tag "' + name + '"? It comes off every item that has it. You can undo this.')) return;
      after(await post(`/api/tags/${id}/delete`, {}), `Deleted the tag "${name}".`);
    }
  });

  filterEl.addEventListener('input', applyFilter);

  const KIND_WORDS = { orphan: 'Unused', empty: 'Empty branch', near_duplicate: 'Looks like another tag' };

  async function loadSweep() {
    sweepEl.innerHTML = '<p class="admin-row-hint">Looking&hellip;</p>';
    let data;
    try {
      data = await getJson('/api/tags/sweep');
    } catch (err) {
      sweepEl.innerHTML = `<p class="admin-row-hint">Couldn't build the cleanup list: ${esc(err.message)}</p>`;
      return;
    }
    sweepEl.dataset.loaded = '1';
    const rows = data.entries || [];
    if (!rows.length) { sweepEl.innerHTML = '<p class="admin-row-hint">Nothing to clean up.</p>'; return; }
    sweepEl.innerHTML = `<table class="admin-tags-sweep-table"><thead><tr><th scope="col">Delete?</th><th scope="col">Tag</th>
      <th scope="col">Why it is listed</th><th scope="col">Suggestion</th><th scope="col"></th></tr></thead><tbody>
      ${rows.map((r) => {
        const best = (r.duplicates || []).slice().sort((a, b) => b.items - a.items)[0];
        const merge = best ? `<button type="button" class="btn-ms" data-merge="${r.id}" data-into="${best.id}" data-name="${esc(r.name)}" data-into-name="${esc(best.path)}">Merge into ${esc(best.path)}</button>` : '';
        const box = r.deletable
          ? `<input type="checkbox" data-pick="${r.id}" aria-label="Delete ${esc(r.path)}">`
          : `<input type="checkbox" disabled aria-disabled="true" title="Can't be deleted here: ${esc(r.why_not)}" aria-label="${esc(r.path)} can't be deleted here: ${esc(r.why_not)}">`;
        return `<tr><td>${box}</td><td>${esc(r.path)}${r.items ? ` <span class="admin-tag-count">(${r.items})</span>` : ''}</td>
          <td>${r.kinds.map((k) => esc(KIND_WORDS[k] || k)).join(', ')}${r.why_not ? `<br><span class="admin-row-hint">Not deletable: ${esc(r.why_not)}</span>` : ''}</td>
          <td>${esc(r.suggestion)}</td><td>${merge}</td></tr>`;
      }).join('')}</tbody></table>
      <button type="button" class="btn-ms" id="admin-tags-sweep-apply">Delete the ticked tags</button>`;
  }

  sweepBtn.addEventListener('click', loadSweep);

  sweepEl.addEventListener('click', async (e) => {
    const mergeBtn = e.target.closest('button[data-merge]');
    if (mergeBtn) {
      const name = mergeBtn.dataset.name;
      if (!window.confirm('Merge "' + name + '" into "' + mergeBtn.dataset.intoName + '"? You can undo this.')) return;
      after(await post(`/api/tags/${mergeBtn.dataset.merge}/merge`, { into: mergeBtn.dataset.into }),
        `Merged "${name}" into "${mergeBtn.dataset.intoName}".`);
      return;
    }
    if (e.target.id !== 'admin-tags-sweep-apply') return;
    const picked = Array.from(sweepEl.querySelectorAll('input[data-pick]:checked')).map((c) => c.dataset.pick);
    if (!picked.length) { say('Tick at least one tag to delete. Nothing was changed.', true); return; }
    if (!window.confirm('Delete ' + picked.length + ' tag(s)? Only these are deleted, as one change you can undo.')) return;
    after(await post('/api/tags/sweep/apply', { tag_ids: picked }), `Deleted ${picked.length} tag(s) from the cleanup list.`);
  });

  loadTree();
})();
