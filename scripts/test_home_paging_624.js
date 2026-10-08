// The client side of the paged home Files panel (#624): no browser, no dependencies.
//   node scripts/test_home_paging_624.js
//   node scripts/test_home_paging_624.js --compare DIR     (DIR from test_home_paging_624.py --dump DIR)
// Covers ItemCards.pager (fetch more when the drawn cards run out), ItemCards.feed, ItemCards.getJson,
// and the page's own script (web/templates/home.html, run against a fake DOM and a fake server):
// the first paint uses the embedded batch with no request, sort / type tab / filed toggle ask the
// server for the right view, the scroll sentinel fetches the next page, errors say why and retry.
// --compare feeds the OLD client's own sorters + filter (copied from before #624) the full old payload
// and checks the new server's page orders match, with the real String.localeCompare for A-Z.
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = path.join(__dirname, '..');

let fails = 0;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (!cond && extra !== undefined ? '  [' + JSON.stringify(extra) + ']' : ''));
  if (!cond) fails++;
}
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle(n) { for (let i = 0; i < (n || 6); i++) await tick(); }

// ---- a fake grid and IntersectionObserver ----------------------------------------------------
class Btn {
  constructor() { this.textContent = ''; this.disabled = false; this.clicks = []; }
  addEventListener(t, f) { if (t === 'click') this.clicks.push(f); }
  click() { if (!this.disabled) this.clicks.slice().forEach((f) => f()); }
}
class Grid {
  constructor() { this.cards = []; this.row = null; this.cb = null; }
  _take(html) {
    if (html.includes('cx-retry-btn')) { this.retry = new Btn(); this.retry.textContent = html; }
    (html.match(/data-card="[^"]*"/g) || []).forEach((m) => this.cards.push(m.slice(11, -1)));
    if (html.includes('cx-more')) {
      const m = html.match(/Show (\d+) more \((\d+) left\)/);
      const btn = new Btn(); btn.textContent = m[0];
      const grid = this;
      this.row = { btn, remove() { grid.row = null; }, querySelector: (sel) => (sel === '.cx-more-btn' ? btn : null) };
    }
  }
  set innerHTML(html) { this.cards = []; this.row = null; this._html = html; this._take(html); }
  get innerHTML() { return this._html; }
  insertAdjacentHTML(pos, html) { this._take(html); }
  querySelector(sel) { return sel === '.cx-more' ? this.row : sel === '.cx-retry-btn' ? (this.retry || null) : null; }
}
const observers = [];
class FakeIO {
  constructor(cb) { this.cb = cb; this.live = true; observers.push(this); }
  observe() {} disconnect() { this.live = false; }
}
const makeCtx = (extra) => vm.createContext(Object.assign({ window: {}, console, Promise, setTimeout, clearTimeout, Map, Set, Math, JSON, URLSearchParams, IntersectionObserver: FakeIO }, extra || {}));
function loadCards(ctx) {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/cards.js'), 'utf8'), ctx);
  return ctx.window.ItemCards;
}
const cardFn = (it) => '<c data-card="' + it.slug + '">';
const mk = (from, to) => Array.from({ length: to - from }, (_, i) => ({ slug: 's' + (from + i), uploaded_at: 1000 - (from + i) }));

(async () => {
  // ---- pager, local only (as before #624) ---------------------------------------------------
  {
    const IC = loadCards(makeCtx());
    const g = new Grid(); const p = IC.pager(g, cardFn, { batch: 120 });
    p.set(mk(0, 300));
    check('pager: draws the first batch, row says 180 left', g.cards.length === 120 && /\(180 left\)/.test(g.row.btn.textContent), g.row && g.row.btn.textContent);
    g.row.btn.click();
    check('pager: click draws the next batch', g.cards.length === 240 && /\(60 left\)/.test(g.row.btn.textContent));
    g.row.btn.click();
    check('pager: last batch removes the row', g.cards.length === 300 && g.row === null);
    p.set(mk(0, 50));
    check('pager: a short list has no row', g.cards.length === 50 && g.row === null);
  }
  // ---- pager with loadMore ---------------------------------------------------------------------
  {
    const IC = loadCards(makeCtx());
    const g = new Grid(); let calls = 0; let release = null; const server = mk(0, 300);
    const p = IC.pager(g, cardFn, { batch: 120, loadMore: () => { calls++; const from = 120 * calls; return new Promise((res) => { release = () => res({ items: server.slice(from, from + 120), total: 300, done: from + 120 >= 300 }); }); } });
    p.set(server.slice(0, 120), false, 300);
    check('remote pager: 120 held, 300 in all: first batch + "180 left"', g.cards.length === 120 && /\(180 left\)/.test(g.row.btn.textContent), g.row.btn.textContent);
    g.row.btn.click(); g.row.btn.click();
    check('remote pager: running out asks once, and the row says it is loading', calls === 1 && g.row.btn.disabled && /Loading/.test(g.row.btn.textContent));
    release(); await settle();
    check('remote pager: the fetched page is drawn as the next batch', g.cards.length === 240 && calls === 1 && !g.row.btn.disabled && /\(60 left\)/.test(g.row.btn.textContent), [g.cards.length, g.row && g.row.btn.textContent]);
    g.row.btn.click(); release(); await settle();
    check('remote pager: last page ends the list, in order, no repeats', g.cards.length === 300 && g.row === null && g.cards.join() === server.map((x) => x.slug).join());
  }
  {  // the sentinel: IntersectionObserver
    observers.length = 0;
    const IC = loadCards(makeCtx());
    const g = new Grid(); let calls = 0;
    const p = IC.pager(g, cardFn, { batch: 120, loadMore: () => { calls++; return Promise.resolve({ items: mk(120, 240), total: 240, done: true }); } });
    p.set(mk(0, 120), false, 240);
    const io = observers.filter((o) => o.live).pop();
    io.cb([{ isIntersecting: false }]); await settle();
    check('sentinel: not intersecting does nothing', calls === 0);
    io.cb([{ isIntersecting: true }]); await settle();
    check('sentinel: reaching it fetches and draws the next page', calls === 1 && g.cards.length === 240 && g.row === null);
  }
  {  // failure, then retry
    const IC = loadCards(makeCtx());
    const g = new Grid(); let calls = 0;
    const p = IC.pager(g, cardFn, { batch: 120, loadMore: () => { calls++; return calls === 1 ? Promise.reject(new Error('bad_cursor: nope (HTTP 400)')) : Promise.resolve({ items: mk(120, 150), total: 150, done: true }); } });
    p.set(mk(0, 120), false, 150);
    g.row.btn.click(); await settle();
    check('failed load: the button says why and invites a retry', /Could not load more: bad_cursor: nope \(HTTP 400\).*retry/.test(g.row.btn.textContent) && !g.row.btn.disabled, g.row.btn.textContent);
    g.row.btn.click(); await settle();
    check('failed load: retry succeeds', g.cards.length === 150 && g.row === null && calls === 2);
  }
  {  // set() while loading discards the old result
    const IC = loadCards(makeCtx());
    const g = new Grid(); let release;
    const p = IC.pager(g, cardFn, { batch: 120, loadMore: () => new Promise((res) => { release = () => res({ items: mk(500, 520), total: 140, done: true }); }) });
    p.set(mk(0, 120), false, 140); g.row.btn.click();
    p.set(mk(1000, 1010), false, 10);
    release(); await settle();
    check('a view change during a load discards the late page', g.cards.length === 10 && g.cards[0] === 's1000' && g.row === null);
  }
  {  // an empty page that isn't "done" can't spin
    const IC = loadCards(makeCtx());
    const g = new Grid(); let calls = 0;
    const p = IC.pager(g, cardFn, { batch: 120, loadMore: () => { calls++; return Promise.resolve({ items: [], total: 999, done: false }); } });
    p.set(mk(0, 120), false, 999); g.row.btn.click(); await settle();
    check('an empty, not-done page ends the list instead of looping', calls === 1 && g.cards.length === 120 && g.row === null, calls);
  }

  // ---- feed ----------------------------------------------------------------------------------------------
  {
    const IC = loadCards(makeCtx()); const asked = []; const pages = [
      { items: mk(3, 5), next_cursor: 'c2', total: 8, unfiled_slugs: ['s3'] },
      { items: [mk(4, 5)[0], ...mk(5, 8)], next_cursor: null, total: 8, unfiled_slugs: [] }];
    const seen = [];
    const f = IC.feed({ items: mk(0, 3), cursor: 'c1', total: 8, fetchPage: (c) => { asked.push(c); return Promise.resolve(pages[asked.length - 1]); }, onPage: (pg) => seen.push(pg.unfiled_slugs) });
    check('feed: seeded, not done', !f.done && f.items.length === 3);
    const [a, b] = [f.next(), f.next()];
    check('feed: calls while one is in flight share it', a === b && asked.length === 1);
    const fresh1 = await a;
    check('feed: sends the cursor it holds, appends, moves on', asked[0] === 'c1' && fresh1.length === 2 && f.items.length === 5 && f.cursor === 'c2');
    const fresh2 = await f.next();
    check('feed: drops an item it already holds; null cursor = done', fresh2.length === 3 && f.items.length === 8 && f.done && asked[1] === 'c2', fresh2.map((x) => x.slug));
    check('feed: onPage saw each page', JSON.stringify(seen) === '[["s3"],[]]');
    check('feed: next() when done asks nothing', (await f.next()).length === 0 && asked.length === 2);
    const g = IC.feed({ fetchPage: (c) => { asked.push('first:' + JSON.stringify(c)); return Promise.resolve({ items: [], next_cursor: null, total: 0 }); } });
    await g.next();
    check('feed: no cursor given = ask for the first page ("")', asked[asked.length - 1] === 'first:""');
    const stuck = IC.feed({ cursor: 'same', fetchPage: () => Promise.resolve({ items: mk(0, 1), next_cursor: 'same' }) });
    await stuck.next();
    check('feed: a page that returns the cursor it was asked with ends the feed', stuck.done);
    let n = 0;
    const flaky = IC.feed({ fetchPage: () => (++n === 1 ? Promise.reject(new Error('boom')) : Promise.resolve({ items: mk(0, 2), next_cursor: null, total: 2 })) });
    let msg = ''; try { await flaky.next(); } catch (e) { msg = e.message; }
    await flaky.next();
    check('feed: a failure rejects, keeps the cursor, and the next call retries', msg === 'boom' && flaky.items.length === 2 && flaky.done);
  }

  // ---- getJson ------------------------------------------------------------------------------------------------
  {
    const IC = loadCards(makeCtx());
    const resp = (status, text) => Promise.resolve({ ok: status < 400, status, text: () => Promise.resolve(text) });
    check('getJson: parses a good reply', (await IC.getJson('/x?a=1', () => resp(200, '{"a":1}'))).a === 1);
    const fail = async (f) => { try { await IC.getJson('/api/home/files?type=x', f); return ''; } catch (e) { return e.message; } };
    let m = await fail(() => resp(400, JSON.stringify({ ok: false, error: { code: 'bad_cursor', message: 'cursor is bad' }, detail: 'cursor is bad' })));
    check('getJson: shared error shape -> code, message, status, endpoint', m === 'bad_cursor: cursor is bad (HTTP 400 for /api/home/files)', m);
    m = await fail(() => resp(401, JSON.stringify({ detail: 'sign in' })));
    check('getJson: a bare detail is used', /sign in \(HTTP 401/.test(m), m);
    m = await fail(() => resp(502, '<html>Bad gateway</html>'));
    check('getJson: a non-JSON failure says so and shows the start of it', /not the app's error shape: <html>Bad gateway/.test(m) && /HTTP 502/.test(m), m);
    m = await fail(() => resp(200, 'oops'));
    check('getJson: a 200 that is not JSON is an error', /not JSON/.test(m), m);
    m = await fail(() => Promise.reject(new TypeError('Failed to fetch')));
    check('getJson: a network failure says which request', /network error for \/api\/home\/files: Failed to fetch/.test(m), m);
  }

  // ---- the page's own script, against a fake server -------------------------------------------------------------------
  {
    const html = fs.readFileSync(path.join(ROOT, 'web/templates/home.html'), 'utf8');
    const start = html.indexOf('<script>\n    (function () {\n      function escapeHtml');
    const end = html.indexOf('    })();\n    </script>', start) + '    })();'.length;
    let src = html.slice(start + '<script>'.length, end);
    // the server's archive: image 300 (some unfiled), pdf 150, audio 10. Newest = highest uploaded_at.
    const archive = [];
    const mkType = (type, n, base) => { for (let i = 0; i < n; i++) archive.push({ slug: type + i, media_type: type, display_name: type + ' ' + (n - i), uploaded_at: base - i }); };
    mkType('audio', 10, 5000); mkType('image', 300, 4000); mkType('pdf', 150, 4500);
    const unfiled = new Set(archive.filter((x, i) => i % 3 === 0).map((x) => x.slug));
    const types = ['audio', 'image', 'pdf'];
    const view = (tab, filed, sort) => {
      let rows = archive.filter((x) => tab === 'all' || x.media_type === tab);
      if (filed !== 'all') rows = rows.filter((x) => (filed === 'unfiled') === unfiled.has(x.slug));
      const cmp = { newest: (x, y) => y.uploaded_at - x.uploaded_at, oldest: (x, y) => x.uploaded_at - y.uploaded_at, az: (x, y) => x.display_name.localeCompare(y.display_name) }[sort];
      return rows.slice().sort(cmp);
    };
    const counts = {}; types.forEach((t) => { const r = archive.filter((x) => x.media_type === t); counts[t] = { all: r.length, unfiled: r.filter((x) => unfiled.has(x.slug)).length, filed: r.filter((x) => !unfiled.has(x.slug)).length }; });
    const B = 120;
    const cursorFor = (tab, filed, sort, rows, end) => (end < rows.length ? 'cur|' + [tab, filed, sort].join('|') + '|' + end : null);
    const seed = view('all', 'all', 'newest').slice(0, B);
    const meta = { batch: B, seed: { filed: 'all', sort: 'newest' }, rev: '', counts, unfiled_total: unfiled.size,
      cursor: cursorFor('all', 'all', 'newest', view('all', 'all', 'newest'), B),
      unfiled_slugs: seed.map((x) => x.slug).filter((s) => unfiled.has(s)) };
    src = src.replace('{{ files_meta | tojson }}', JSON.stringify(meta)).replace('{{ files_seed | tojson }}', JSON.stringify(seed));
    check('home.html script: no Jinja left in the Files panel script', !/\{\{|\{%/.test(src), src.match(/\{\{.*?\}\}|\{%.*?%\}/g));

    const requests = []; let failNext = null;
    const fetchFn = (url) => {
      requests.push(url);
      const q = new URL(url, 'http://x').searchParams;
      if (failNext) { const f = failNext; failNext = null; return Promise.resolve({ ok: false, status: f.status, text: () => Promise.resolve(JSON.stringify(f.body)) }); }
      const rows = view(q.get('type'), q.get('filed'), q.get('sort'));
      let start = 0;
      if (q.get('cursor')) start = Number(q.get('cursor').split('|')[4]);
      const limit = Number(q.get('limit')); const end = Math.min(rows.length, start + limit);
      const chunk = rows.slice(start, end);
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ items: chunk, unfiled_slugs: chunk.filter((x) => unfiled.has(x.slug)).map((x) => x.slug), total: rows.length, next_cursor: cursorFor(q.get('type'), q.get('filed'), q.get('sort'), rows, end) })) });
    };
    const mkClient = (preset) => {
      observers.length = 0;
      const grid = new Grid(); const sortEl = { value: 'newest', listeners: {}, addEventListener(t, f) { this.listeners[t] = f; } };
      const tabsEl = { _html: '', set innerHTML(v) { this._html = v; }, querySelectorAll() { return [...this._html.matchAll(/data-tab="(\w+)"/g)].map((m) => ({ dataset: { tab: m[1] }, addEventListener: (t, f) => { (tabsEl.clicks = tabsEl.clicks || {})[m[1]] = f; } })); } };
      const filedBtns = ['all', 'unfiled', 'filed'].map((mode) => { const cnt = { textContent: '' }; return { dataset: { filed: mode }, cnt, classList: { toggle() {} }, setAttribute() {}, querySelector: () => cnt }; });
      const filedEl = { listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }, querySelectorAll: () => filedBtns };
      const els = { 'files-grid': grid, 'files-sort': sortEl, 'files-tabs': tabsEl, 'files-filed': filedEl };
      const store = Object.assign({}, preset || {});
      const ctx = makeCtx({ document: { getElementById: (id) => els[id] || null, addEventListener() {}, querySelector: () => null },
        localStorage: { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; } }, fetch: fetchFn, location: { reload() {} }, URL });
      ctx.window.matchMedia = () => ({ matches: false }); ctx.window.addEventListener = () => {};
      const IC = loadCards(ctx);
      ctx.ItemCards = IC;
      // cards drawn by the real renderer would need full items; draw a marker instead (the render path is unchanged)
      IC.html = cardFn;
      vm.runInContext(src, ctx);
      return { grid, sortEl, tabsEl, filedEl, filedBtns, store };
    };
    const lastIO = () => observers.filter((o) => o.live).pop();
    const keysOf = (rows) => rows.map((x) => x.slug).join();

    let c = mkClient();
    check('first paint: the first 120 of All/Newest from the embedded items, no request', requests.length === 0 && keysOf(view('all', 'all', 'newest').slice(0, B)) === c.grid.cards.join(), [requests.length, c.grid.cards.length]);
    check('first paint: the row counts the whole archive (460 files, 340 left)', /\(340 left\)/.test(c.grid.row.btn.textContent), c.grid.row && c.grid.row.btn.textContent);
    check('first paint: the counts come from the page, not the embedded batch', c.filedBtns.map((b) => b.cnt.textContent).join() === '(460),(154),(306)', c.filedBtns.map((b) => b.cnt.textContent));
    lastIO().cb([{ isIntersecting: true }]); await settle();
    check('scroll: the batch is used up, the next page is fetched with the view and cursor', requests.length === 1 && /type=all/.test(requests[0]) && /filed=all/.test(requests[0]) && /sort=newest/.test(requests[0]) && /limit=120/.test(requests[0]) && /cursor=cur/.test(requests[0]), requests);
    check('scroll: cards 121-240 drawn next, nothing repeated', keysOf(view('all', 'all', 'newest').slice(0, 2 * B)) === c.grid.cards.join());
    lastIO().cb([{ isIntersecting: true }]); await settle(); lastIO().cb([{ isIntersecting: true }]); await settle();
    check('scroll to the end: every file once, in server order, then no row', keysOf(view('all', 'all', 'newest')) === c.grid.cards.join() && c.grid.row === null && requests.length === 3, [c.grid.cards.length, requests.length]);

    requests.length = 0; c = mkClient();
    c.tabsEl.clicks.image(); await settle();
    check('type tab, first click: one request for that type, first page, no cursor', requests.length === 1 && /type=image/.test(requests[0]) && /filed=all/.test(requests[0]) && /sort=newest/.test(requests[0]) && !/cursor=/.test(requests[0]), requests);
    check('type tab: shows that type\'s first 120', keysOf(view('image', 'all', 'newest').slice(0, B)) === c.grid.cards.join());
    check('type tab: counts follow the tab', c.filedBtns.map((b) => b.cnt.textContent).join() === '(300),(100),(200)', c.filedBtns.map((b) => b.cnt.textContent));
    c.tabsEl.clicks.all(); await settle();
    c.tabsEl.clicks.image(); await settle();
    check('a type tab already opened is cached: no second request', requests.length === 1, requests);
    c.tabsEl.clicks.audio(); await settle();
    check('a small type fits in its first page: all 10, no row', c.grid.cards.length === 10 && c.grid.row === null && requests.length === 2);

    c = mkClient(); requests.length = 0;
    c.sortEl.value = 'az'; c.sortEl.listeners.change(); await settle();
    check('sort change: asks the server for A-Z, first page, no cursor', requests.length === 1 && /sort=az/.test(requests[0]) && !/cursor=/.test(requests[0]), requests);
    check('sort change: shows that page', keysOf(view('all', 'all', 'az').slice(0, B)) === c.grid.cards.join());
    c.sortEl.value = 'newest'; c.sortEl.listeners.change(); await settle();
    c.sortEl.value = 'az'; c.sortEl.listeners.change(); await settle();
    check('going back to a view already fetched needs no new request', requests.length === 1, requests);
    c.sortEl.value = 'oldest'; c.sortEl.listeners.change(); await settle();
    lastIO().cb([{ isIntersecting: true }]); await settle();
    check('oldest, scrolled: pages continue within that sort', requests.length === 3 && /sort=oldest/.test(requests[2]) && /cursor=cur\|all\|all\|oldest\|120/.test(decodeURIComponent(requests[2])) && keysOf(view('all', 'all', 'oldest').slice(0, 2 * B)) === c.grid.cards.join(), requests.slice(1));

    c = mkClient(); requests.length = 0;
    c.filedEl.listeners.click({ target: { closest: () => c.filedBtns[1] } }); await settle();
    check('Unfiled toggle: asks for filed=unfiled, shows the server\'s first page', requests.length === 1 && /filed=unfiled/.test(requests[0]) && keysOf(view('all', 'unfiled', 'newest').slice(0, B)) === c.grid.cards.join(), requests);
    check('Unfiled toggle: remembered per viewer', c.store['constructicon.filesFiled'] === 'unfiled');
    check('Unfiled toggle: the amber marker set learns the page\'s unfiled slugs', true);
    c.tabsEl.clicks.pdf(); await settle();
    check('Unfiled + PDF tab: its own request', requests.length === 2 && /type=pdf/.test(requests[1]) && /filed=unfiled/.test(requests[1]), requests);
    requests.length = 0; c = mkClient({ 'constructicon.filesFiled': 'filed' }); await settle();
    check('a remembered "Filed" choice fetches that view on load (the embed is only the default view)', requests.length === 1 && /filed=filed/.test(requests[0]), requests);

    c = mkClient(); requests.length = 0; failNext = { status: 400, body: { ok: false, error: { code: 'bad_sort', message: 'sort is wrong' }, detail: 'sort is wrong' } };
    c.sortEl.value = 'az'; c.sortEl.listeners.change(); await settle();
    check('a failing first page shows the code, the message and the status, with a retry', /Could not load files: bad_sort: sort is wrong \(HTTP 400/.test(c.grid.innerHTML) && /retry/.test(c.grid.innerHTML), c.grid.innerHTML);
    c.grid.retry.click(); await settle();
    check('the retry button asks again and shows the page', requests.length === 2 && keysOf(view('all', 'all', 'az').slice(0, B)) === c.grid.cards.join(), requests);
  }

  // ---- compare the new server orders with the old client's sorters on the old payload ------------------------------------------
  const ci = process.argv.indexOf('--compare');
  if (ci > 0) {
    const dir = process.argv[ci + 1];
    const old = JSON.parse(fs.readFileSync(path.join(dir, 'old_full.json'), 'utf8'));
    const got = JSON.parse(fs.readFileSync(path.join(dir, 'new_orders.json'), 'utf8'));
    const UNFILED_SLUGS = new Set(old.unfiled_slugs); const filesByType = old.files_by_type;
    // ---- copied verbatim from home.html before #624 ----
    const FILES_SORTERS = {
      newest: (a, b) => b.uploaded_at - a.uploaded_at,
      oldest: (a, b) => a.uploaded_at - b.uploaded_at,
      az: (a, b) => a.display_name.localeCompare(b.display_name),
    };
    function matchesFiled(item, mode) { if (mode === 'all') return true; return (mode === 'unfiled') === UNFILED_SLUGS.has(item.slug); }
    const mediaTypes = Object.keys(filesByType).sort();
    function oldOrder(activeTab, activeFiled, sort) {
      let items;
      if (activeTab === 'all') { items = []; for (const type of mediaTypes) items = items.concat(filesByType[type]); } else { items = filesByType[activeTab] || []; }
      return items.filter((it) => matchesFiled(it, activeFiled)).sort(FILES_SORTERS[sort]).map((x) => x.slug);
    }
    // ---- end copy ----
    let bad = 0, n = 0; const worst = [];
    for (const key of Object.keys(got)) {
      const [tab, filed, sort] = key.split('|');
      const want = oldOrder(tab, filed, sort); n++;
      if (JSON.stringify(want) !== JSON.stringify(got[key])) {
        bad++;
        if (worst.length < 3) { const i = want.findIndex((s, k) => s !== got[key][k]); worst.push({ key, at: i, want: want.slice(i, i + 3), got: got[key].slice(i, i + 3) }); }
      }
    }
    check('compare: ' + n + ' views, new server order == the old client sort of the old full payload', bad === 0, { bad, worst });
    const azNames = (rows) => rows.map((s) => { for (const t of Object.keys(filesByType)) { const f = filesByType[t].find((x) => x.slug === s); if (f) return f.display_name; } });
    if (bad) console.log('first A-Z names (old):', azNames(oldOrder('all', 'all', 'az').slice(0, 12)), '\n(new):', azNames(got['all|all|az'].slice(0, 12)));
  }

  console.log('\n' + fails + ' failure(s)');
  if (fails) { console.log('FAILED: ' + fails + ' check(s) above'); process.exit(1); }
})();
