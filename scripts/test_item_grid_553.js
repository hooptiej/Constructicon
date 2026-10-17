// The shared file grid's script (#553): web/static/js/item-grid.js, against a small fake DOM and a fake
// server. No browser, no dependencies.
//   node scripts/test_item_grid_553.js
// Covers: mounting from the config block (ItemGrid.mountAll, once), the first paint from the embedded
// batch, the type filter (options, counts, labels) and the filed toggle, the source and the card links,
// Select and the selection count, every bulk action (the POST it sends, the Undo bar with the batch,
// the refresh that follows, the refresh after an undo, a refused action's message), a viewer's grid (no
// checkboxes, no bulk wiring), and the empty state.
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = path.join(__dirname, '..');

let fails = 0;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (!cond && extra !== undefined ? '  [' + JSON.stringify(extra) + ']' : ''));
  if (!cond) fails++;
}
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle(n) { for (let i = 0; i < (n || 8); i++) await tick(); }

// ---- a fake DOM ------------------------------------------------------------------------------------------
class El {
  constructor(id) { this.id = id; this.listeners = {}; this.value = ''; this.disabled = false; this.style = {}; this.textContent = ''; this.html = ''; this.classes = new Set(); this.attrs = {}; this.dataset = {}; this.className = ''; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, ev) { (this.listeners[t] || []).forEach((f) => f(Object.assign({ target: this, preventDefault() {} }, ev || {}))); }
  get classList() {
    const s = this.classes;
    return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
      toggle: (c, on) => { if (on === undefined) on = !s.has(c); if (on) s.add(c); else s.delete(c); return on; } };
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  set innerHTML(v) { this.html = v; } get innerHTML() { return this.html; }
  insertAdjacentHTML(p, h) { this.html += h; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  contains(x) { return !!x && x.box === this.id; }
  closest() { return null; }
}
class GridEl extends El {
  constructor(id) { super(id); this.row = null; }
  _take(html) {
    if (html.includes('cx-more')) {
      const btn = new El('more'); btn.textContent = (html.match(/Show \d+ more \(\d+ left\)/) || [''])[0];
      const g = this; this.row = { btn, remove() { g.row = null; }, querySelector: (s) => (s === '.cx-more-btn' ? btn : null) };
    }
  }
  set innerHTML(v) { this.html = v; this.row = null; this._take(v); }
  get innerHTML() { return this.html; }
  insertAdjacentHTML(p, h) { this.html += h; this._take(h); }
  get cards() { return [...this.html.matchAll(/<div class="cx-item[^"]*" data-slug="([^"]+)"/g)].map((m) => m[1]); }
  querySelector(sel) { return sel === '.cx-more' ? this.row : null; }
}

function makeDom(id, opts) {
  opts = opts || {};
  const els = {};
  const add = (part, el) => { els[part ? id + '-' + part : id] = el || new El(part); return els[part ? id + '-' + part : id]; };
  const box = add('', new El(id));
  box.dataset.itemGrid = id;
  const grid = add('grid', new GridEl('grid'));
  add('sort').value = 'date_newest';
  const type = add('type');
  const filedBtns = ['all', 'unfiled', 'filed'].map((mode) => {
    const b = new El('f-' + mode); b.dataset.filed = mode; const cnt = new El('cnt'); b.cnt = cnt; b.querySelector = () => cnt; b.closest = () => b; return b;
  });
  const filed = add('filed'); filed.querySelectorAll = () => filedBtns;
  add('empty'); add('count');
  if (opts.bulk) {
    ['select', 'bar', 'selcount', 'clear', 'card', 'new-card', 'tag', 'tag-suggestions', 'provenance', 'provenance-apply', 'sensitive', 'delete'].forEach((p) => add(p));
  }
  const listeners = {};
  const doc = {
    getElementById: (k) => els[k] || null,
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    dispatch: (t, detail) => (listeners[t] || []).forEach((f) => f({ detail })),
    querySelectorAll: (sel) => (sel === '[data-item-grid]' ? [box] : []),
  };
  return { els, box, grid, type, filedBtns, doc, get: (p) => els[id + '-' + p] };
}

// ---- a fake server ------------------------------------------------------------------------------------------
function makeServer(archive, unfiled) {
  const log = [];
  let failNext = null;
  const view = (src, type, filed) => archive.filter((x) => (type === 'all' || x.media_type === type)
    && (filed === 'all' || (filed === 'unfiled') === unfiled.has(x.slug)));
  const counts = () => { const c = {}; archive.forEach((x) => { const t = c[x.media_type] = c[x.media_type] || { all: 0, unfiled: 0, filed: 0 }; t.all++; t[unfiled.has(x.slug) ? 'unfiled' : 'filed']++; }); return c; };
  const reply = (status, body) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  const fetch = (url, opts) => {
    const u = new URL(url, 'http://x');
    const form = opts && opts.body ? [...opts.body.entries()] : null;
    log.push({ url, path: u.pathname, q: Object.fromEntries(u.searchParams), method: (opts && opts.method) || 'GET', form });
    if (failNext) { const f = failNext; failNext = null; return reply(f.status, f.body); }
    if (u.pathname === '/api/projects') return reply(200, [{ id: 7, title: 'Desk' }, { id: 9, title: 'Lamp' }]);
    if (u.pathname === '/api/tags') return reply(200, [{ name: 'Wiring', path: 'Builds > Wiring' }]);
    if (u.pathname === '/api/items/grid') {
      const rows = view(u.searchParams.get('source'), u.searchParams.get('type'), u.searchParams.get('filed'));
      return reply(200, { items: rows.slice(0, 120), total: rows.length, next_cursor: null, unfiled_slugs: rows.filter((x) => unfiled.has(x.slug)).map((x) => x.slug),
        counts: counts(), labels: { image: 'Image', audio: 'Audio' } });
    }
    if (u.pathname === '/api/delete') return reply(200, { deleted: form.filter(([k]) => k === 'slugs').length, batch_id: 'b-del' });
    if (u.pathname === '/api/projects/from-selection') return reply(200, { id: 11, slug: 'new-card', title: 'New card', batch_id: 'b-new' });
    if (u.pathname.startsWith('/api/bulk/')) return reply(200, { count: form.filter(([k]) => k === 'slugs').length, batch_id: 'b-' + u.pathname.split('/').pop() });
    return reply(404, { ok: false, error: { code: 'not_found', message: 'no route ' + u.pathname } });
  };
  return { fetch, log, fail: (status, body) => { failNext = { status, body }; }, counts };
}

function loadInto(ctx, rel) { vm.runInContext(fs.readFileSync(path.join(ROOT, rel), 'utf8'), ctx); }
function newCtx() {
  const ctx = vm.createContext({ window: {}, console, Promise, setTimeout, clearTimeout, Map, Set, Math, JSON, URL, URLSearchParams, FormData,
    IntersectionObserver: class { observe() {} disconnect() {} } });
  loadInto(ctx, 'web/static/js/cards.js');
  loadInto(ctx, 'web/static/js/item-sort.js');
  loadInto(ctx, 'web/static/js/item-grid.js');
  return ctx;
}

const mkItem = (slug, type, at) => ({ slug, media_type: type, display_name: slug, type_label: type, card_date: 'x', card_date_epoch: at, uploaded_at: at, tags: [] });

(async () => {
  const archive = [];
  for (let i = 0; i < 6; i++) archive.push(mkItem('img' + i, 'image', 1000 - i));
  for (let i = 0; i < 3; i++) archive.push(mkItem('aud' + i, 'audio', 900 - i));
  const unfiled = new Set(['img0', 'img2', 'aud1']);
  const BULK = ['add_to_card', 'new_card', 'tag', 'provenance', 'sensitive', 'delete'];
  const baseMeta = (counts) => ({ batch: 120, seed: { filed: 'all', sort: 'date_newest' }, rev: '', counts, labels: { image: 'Image', audio: 'Audio' },
    cursor: null, source: 'user:bulk', unfiled_total: 3, unfiled_slugs: ['img0', 'img2', 'aud1'] });

  function setup(opts) {
    opts = opts || {};
    const server = makeServer(archive, unfiled);
    const dom = makeDom('g', { bulk: opts.bulk !== false });
    const ctx = newCtx();
    const undo = []; const alerts = []; const store = {};
    let confirmAnswer = true; let promptAnswer = 'New card';
    const cfg = { id: 'g', endpoint: '/api/items/grid', seed: archive.slice(), meta: baseMeta(server.counts()), filters: opts.filters || ['type', 'filed'],
      bulk: opts.bulk === false ? [] : BULK, can_edit: opts.bulk !== false, href_from: 'user:bulk', unfiled_lamp: true,
      filed_key: 'k.filed', labels: { all_types: 'All types', new_card_prompt: 'New card name:' }, count: { one: 'upload', other: 'uploads', suffix: '' } };
    Object.assign(cfg, opts.cfg || {});
    const g = ctx.window.ItemGrid.mount(cfg, {
      doc: dom.doc, fetch: server.fetch, storage: { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; } },
      showUndoBar: (m, b, done) => undo.push({ m, b, done }), confirm: () => confirmAnswer, prompt: () => promptAnswer, alert: (m) => alerts.push(m),
    });
    return { g, dom, server, ctx, undo, alerts, store, cfg, setConfirm: (v) => { confirmAnswer = v; }, setPrompt: (v) => { promptAnswer = v; } };
  }
  const gridReqs = (s) => s.server.log.filter((r) => r.path === '/api/items/grid');
  const posts = (s) => s.server.log.filter((r) => r.method === 'POST');
  const pick = (s, slugs) => slugs.forEach((slug) => s.ctx.window.toggleCardSelect({ checked: true, dataset: { slug }, box: 'g' }));

  // ---- first paint ----------------------------------------------------------------------------------------
  {
    const s = setup(); await settle();
    check('first paint: the embedded batch, no grid request', gridReqs(s).length === 0 && s.dom.grid.cards.join() === archive.map((x) => x.slug).join(), s.dom.grid.cards);
    check('first paint: the type filter lists All + each type with its label and count',
      /<option value="all">All types \(9\)<\/option><option value="audio">Audio \(3\)<\/option><option value="image">Image \(6\)<\/option>/.test(s.dom.type.html), s.dom.type.html);
    check('first paint: the filed toggle counts', s.dom.filedBtns.map((b) => b.cnt.textContent).join() === '(9),(3),(6)');
    check('first paint: the count line', s.dom.get('count').textContent === '9 uploads');
    check('card links carry ?from= (the user gallery\'s breadcrumb)', /href="\/object\/img0\?from=user%3Abulk"/.test(s.dom.grid.html));
    check('cards carry the unfiled lamp and a select checkbox (an editor\'s grid)',
      /Not filed into a project yet/.test(s.dom.grid.html) && /class="card-select"/.test(s.dom.grid.html));
    check('the card picker is filled from /api/projects', /<option value="7" data-project>Desk<\/option>/.test(s.dom.get('card').html), s.dom.get('card').html);
    check('bulk buttons start disabled (nothing selected)', ['card', 'new-card', 'tag', 'provenance-apply', 'sensitive', 'delete'].every((p) => s.dom.get(p).disabled));

    s.dom.type.value = 'audio'; s.dom.type.fire('change'); await settle();
    const r = gridReqs(s).pop();
    check('type filter: asks the shared endpoint with the source, type and sort', r && r.q.source === 'user:bulk' && r.q.type === 'audio' && r.q.filed === 'all' && r.q.sort === 'date_newest' && !r.q.cursor, r);
    check('type filter: shows that type', s.dom.grid.cards.join() === 'aud0,aud1,aud2', s.dom.grid.cards);
    check('type filter: the filed counts follow the type, the count line says "of"',
      s.dom.filedBtns.map((b) => b.cnt.textContent).join() === '(3),(1),(2)' && s.dom.get('count').textContent === '3 of 9 uploads shown');
    s.dom.get('filed').fire('click', { target: s.dom.filedBtns[1] }); await settle();
    const r2 = gridReqs(s).pop();
    check('filed toggle: asks for filed=unfiled and remembers it', r2.q.filed === 'unfiled' && s.store['k.filed'] === 'unfiled' && s.dom.grid.cards.join() === 'aud1');
  }

  // ---- select + bulk ------------------------------------------------------------------------------------
  {
    const s = setup(); await settle();
    const sel = s.dom.get('select');
    sel.fire('click');
    check('Select turns selecting on (class + aria-pressed)', s.dom.box.classes.has('is-selecting') && sel.attrs['aria-pressed'] === 'true');
    pick(s, ['img1', 'img3']);
    check('two picked: "2 selected", buttons enabled', s.dom.get('selcount').textContent === '2 selected' && !s.dom.get('delete').disabled);
    s.ctx.window.toggleCardSelect({ checked: false, dataset: { slug: 'img3' }, box: 'g' });
    check('unpicking one: "1 selected"', s.dom.get('selcount').textContent === '1 selected');
    pick(s, ['img3']);

    // tag
    const before = gridReqs(s).length;
    s.dom.get('tag').value = 'Wiring';
    s.dom.get('tag').fire('keydown', { key: 'Enter' }); await settle();
    let p = posts(s).pop();
    check('tag: Enter posts /api/bulk/attach-tags with the selection and the name',
      p && p.path === '/api/bulk/attach-tags' && JSON.stringify(p.form) === JSON.stringify([['slugs', 'img1'], ['slugs', 'img3'], ['tag_names', 'Wiring']]), p && p.form);
    check('tag: the Undo bar gets the batch and says what happened', s.undo.length === 1 && s.undo[0].b === 'b-attach-tags' && /Tagged 2 files "Wiring"/.test(s.undo[0].m), s.undo[0]);
    check('tag: the selection is cleared and the grid refreshed from the server (no cursor)',
      s.dom.get('selcount').textContent === '0 selected' && gridReqs(s).length === before + 1 && !gridReqs(s).pop().q.cursor);
    const n = gridReqs(s).length;
    s.undo[0].done('undone'); await settle();
    check('tag: an undo refreshes the grid again', gridReqs(s).length === n + 1);
    s.undo[0].done('closed');

    // add to card
    pick(s, ['aud0']);
    s.dom.get('card').value = '9'; s.dom.get('card').fire('change'); await settle();
    p = posts(s).pop();
    check('add to card: posts /api/bulk/add-to-project with project_id', p.path === '/api/bulk/add-to-project' && p.form.some(([k, v]) => k === 'project_id' && v === '9') && s.dom.get('card').value === '', p.form);
    check('add to card: Undo bar with its batch', s.undo.pop().b === 'b-add-to-project');

    // provenance
    pick(s, ['img4', 'img5']);
    s.dom.get('provenance').value = 'found'; s.dom.get('provenance-apply').fire('click'); await settle();
    p = posts(s).pop();
    check('provenance: one POST /api/bulk/provenance for the whole selection (was one per file)',
      p.path === '/api/bulk/provenance' && p.form.filter(([k]) => k === 'slugs').length === 2 && p.form.some(([k, v]) => k === 'provenance' && v === 'found') && posts(s).filter((x) => x.path === '/api/bulk/provenance').length === 1);
    check('provenance: Undo bar, select reset', s.undo.pop().b === 'b-provenance' && s.dom.get('provenance').value === '');

    // sensitive: confirm no, then yes
    pick(s, ['img2']);
    s.setConfirm(false); const k = posts(s).length;
    s.dom.get('sensitive').fire('click'); await settle();
    check('sensitive: a "no" at the confirm sends nothing', posts(s).length === k);
    s.setConfirm(true); s.dom.get('sensitive').fire('click'); await settle();
    p = posts(s).pop();
    check('sensitive: posts /api/bulk/sensitive sensitive=true', p.path === '/api/bulk/sensitive' && p.form.some(([a, b]) => a === 'sensitive' && b === 'true'));
    s.undo.pop();

    // new card
    pick(s, ['img0']);
    s.dom.get('new-card').fire('click'); await settle();
    p = posts(s).pop();
    check('turn into card: prompts, posts from-selection with the title, Undo bar with its batch',
      p.path === '/api/projects/from-selection' && p.form.some(([a, b]) => a === 'title' && b === 'New card') && s.undo.pop().b === 'b-new');

    // delete
    pick(s, ['img0', 'aud2']);
    s.dom.get('delete').fire('click'); await settle();
    p = posts(s).pop();
    const u = s.undo.pop();
    check('delete: posts /api/delete, "Deleted 2 items", Undo bar with its batch', p.path === '/api/delete' && /Deleted 2 items/.test(u.m) && u.b === 'b-del', u);

    // a refused action
    pick(s, ['img1']);
    s.undo.length = 0;
    s.server.fail(403, { ok: false, error: { code: 'forbidden', message: 'needs an editor' }, detail: 'needs an editor' });
    s.dom.get('delete').fire('click'); await settle();
    check('a refused action: the alert says why, no Undo bar, the selection stays',
      s.alerts.length === 1 && /Delete failed: needs an editor/.test(s.alerts[0]) && s.undo.length === 0 && s.dom.get('selcount').textContent === '1 selected', s.alerts);

    // Select off clears the selection
    s.dom.get('select').fire('click');
    check('Select off: selecting off, the selection cleared', !s.dom.box.classes.has('is-selecting') && s.dom.get('selcount').textContent === '0 selected');

    // an upload refreshes the grid
    const m = gridReqs(s).length;
    s.dom.doc.dispatch('constructicon:upload-complete', { failures: [] }); await settle();
    check('an upload refreshes the grid', gridReqs(s).length === m + 1);
  }

  // ---- a viewer -------------------------------------------------------------------------------------------
  {
    const s = setup({ bulk: false }); await settle();
    check('viewer: cards have no select checkbox', s.dom.grid.cards.length === 9 && !/card-select/.test(s.dom.grid.html));
    check('viewer: no /api/projects or /api/tags request (no bulk controls to fill)', !s.server.log.some((r) => r.path === '/api/projects' || r.path === '/api/tags'));
  }

  // ---- /unfiled's shape: no filed toggle, no lamp --------------------------------------------------------------
  {
    const s = setup({ filters: ['type'], cfg: { unfiled_lamp: false, href_from: 'unfiled' } }); await settle();
    check('/unfiled: no filed counts written (no toggle), no unfiled lamp on the cards',
      s.dom.filedBtns.every((b) => b.cnt.textContent === '') && !/Not filed into a project yet/.test(s.dom.grid.html) && /\?from=unfiled"/.test(s.dom.grid.html));
  }

  // ---- empty grid -------------------------------------------------------------------------------------------------
  {
    const server = makeServer([], new Set());
    const dom = makeDom('e', {});
    const ctx = newCtx();
    ctx.window.ItemGrid.mount({ id: 'e', seed: [], meta: { batch: 120, seed: { filed: 'all', sort: 'date_newest' }, counts: {}, labels: {}, cursor: null, source: 'hobby:x' },
      filters: ['type', 'filed'], bulk: [] }, { doc: dom.doc, fetch: server.fetch, storage: null });
    await settle();
    check('empty grid: the empty state shows, no request', dom.get('empty').style.display === 'block' && server.log.length === 0);
  }

  // ---- mountAll -------------------------------------------------------------------------------------------------
  {
    const server = makeServer(archive, unfiled);
    const dom = makeDom('m', {});
    const cfgEl = new El('m-config');
    cfgEl.textContent = JSON.stringify({ id: 'm', seed: archive.slice(0, 2), meta: baseMeta({ image: { all: 2, unfiled: 0, filed: 2 } }), filters: ['type'], bulk: [] });
    dom.els['m-config'] = cfgEl;
    const ctx = newCtx();
    ctx.window.document = dom.doc; ctx.window.fetch = server.fetch;
    const first = ctx.window.ItemGrid.mountAll(dom.doc);
    const again = ctx.window.ItemGrid.mountAll(dom.doc);
    check('mountAll: mounts each grid from its config block once', first.length === 1 && again.length === 0 && dom.grid.cards.join() === 'img0,img1', dom.grid.cards);
  }

  console.log('\n' + fails + ' failure(s)');
  if (fails) { console.log('FAILED: ' + fails + ' check(s) above'); process.exit(1); }
  console.log('ALL PASS');
})();
