// #851: unit test for web/static/js/admin-sections.js, the /admin disclosures and Tools rows.
//   node scripts/test_admin_sections_851.js
// The script is an IIFE that needs a browser, so it is run in a vm sandbox with a tiny hand-made DOM
// (details, links, tool rows, localStorage, a location hash). It checks what the owner asked for
// (2026-11-08): which sections you left open are remembered; the page works with storage blocked or
// garbled; a #hash opens its section without being remembered; a Tools row clicks its drawer's hidden
// trigger and says so, in words, when that trigger is missing.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'web', 'static', 'js', 'admin-sections.js'), 'utf8');
let failed = 0;
const eq = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : `: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
  if (!ok) failed++;
};

class El {
  constructor(attrs, opts = {}) {
    this.attrs = attrs || {};
    this.open = !!opts.open;
    this.hidden = !!opts.hidden;
    this.textContent = '';
    this.listeners = {};
    this.parent = opts.parent || null;
    this.isSection = !!opts.isSection;
    this.clicks = 0;
    this.id = this.attrs.id || '';
    this.scrolled = 0;
  }
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, ev) { (this.listeners[t] || []).forEach((fn) => fn(ev || {})); }
  closest(sel) {
    for (let e = this; e; e = e.parent) {
      if (sel === 'details.admin-section' && e.isSection) return e;
      if (sel === 'a[href^="#admin-"]' && e.attrs.href && e.attrs.href.startsWith('#admin-')) return e;
    }
    return null;
  }
  click() { this.clicks++; }
  scrollIntoView() { this.scrolled++; }
}

// A page: a "tools" section (open by default in the markup), a "users" section (closed), a link to the
// users section, and two Tools rows (one whose trigger is on the page, one whose trigger is missing).
function page({ hash = '', storage }) {
  const tools = new El({ 'data-admin-section': 'tools', id: 'admin-tools-section' }, { isSection: true, open: true });
  const users = new El({ 'data-admin-section': 'users', id: 'admin-users-section' }, { isSection: true });
  const inner = new El({ id: 'admin-users-rows' }, { parent: users });
  const link = new El({ href: '#admin-users-section' });
  const trigger = new El({ id: 'brand-drawer-trigger' });
  const rowOk = new El({ 'data-tool-trigger': 'brand-drawer-trigger', 'data-tool-label': 'Brand' });
  const rowMissing = new El({ 'data-tool-trigger': 'export-builder-trigger', 'data-tool-label': 'Export' });
  const msg = new El({ id: 'admin-tools-msg' }, { hidden: true });
  const byId = { 'admin-tools-section': tools, 'admin-users-section': users, 'admin-users-rows': inner,
                 'brand-drawer-trigger': trigger, 'admin-tools-msg': msg };
  const docListeners = {};
  const winListeners = {};
  const doc = {
    querySelectorAll(sel) {
      if (sel === 'details.admin-section[data-admin-section]') return [tools, users];
      if (sel === '.admin-tool-row[data-tool-trigger]') return [rowOk, rowMissing];
      return [];
    },
    getElementById(id) { return byId[id] || null; },
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
  };
  const win = {
    location: { hash },
    localStorage: storage,
    addEventListener(t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); },
  };
  vm.runInNewContext(SRC, { document: doc, window: win, decodeURIComponent });
  return { tools, users, inner, link, trigger, rowOk, rowMissing, msg, win, docListeners, winListeners };
}

function memStorage(initial) {
  const s = { data: Object.assign({}, initial), sets: 0 };
  s.getItem = (k) => (k in s.data ? s.data[k] : null);
  s.setItem = (k, v) => { s.sets++; s.data[k] = String(v); };
  return s;
}
const KEY = 'constructicon.admin.sections';
const userToggle = (el, open) => { el.open = open; el.dispatch('toggle'); };  // a person opening / closing it

// 1. nothing stored: the markup's defaults stand; a person's choices are remembered
{
  const st = memStorage({});
  const p = page({ storage: st });
  eq('defaults: Tools open, Users closed', [p.tools.open, p.users.open], [true, false]);
  eq('nothing is written just by loading', st.sets, 0);
  userToggle(p.users, true);
  eq('opening Users is remembered', JSON.parse(st.data[KEY]), { users: true });
  userToggle(p.tools, false);
  eq('closing Tools is remembered too, beside it', JSON.parse(st.data[KEY]), { users: true, tools: false });
}

// 2. stored state is restored, and restoring it is not itself a "choice" to save
{
  const st = memStorage({ [KEY]: JSON.stringify({ users: true, tools: false }) });
  const p = page({ storage: st });
  eq('restored: Users open, Tools closed', [p.users.open, p.tools.open], [true, false]);
  p.users.dispatch('toggle'); p.tools.dispatch('toggle');  // the browser's own toggle events from restoring
  eq('restoring writes nothing back', st.sets, 0);
  userToggle(p.users, false);
  eq('closing Users afterwards is saved', JSON.parse(st.data[KEY]), { users: false, tools: false });
}

// 3. storage blocked: reads and writes both throw; the page still works, it just forgets
{
  const blocked = { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('QuotaExceededError'); } };
  let p, threw = null;
  try { p = page({ storage: blocked }); userToggle(p.users, true); userToggle(p.users, false); } catch (e) { threw = e; }
  eq('storage blocked: no exception reaches the page', threw, null);
  eq('storage blocked: the defaults stand', p && [p.tools.open, p.users.open], [true, false]);
  const none = page({ storage: undefined });
  eq('no storage object at all: still fine', [none.tools.open, none.users.open], [true, false]);
}

// 4. garbled storage is ignored
{
  for (const bad of ['{not json', '"a string"', '[1,2]', 'null']) {
    const p = page({ storage: memStorage({ [KEY]: bad }) });
    eq('garbled value ' + bad + ': defaults', [p.tools.open, p.users.open], [true, false]);
  }
  const p = page({ storage: memStorage({ [KEY]: JSON.stringify({ users: 'yes', tools: 1 }) }) });
  eq('non-boolean entries are ignored', [p.tools.open, p.users.open], [true, false]);
}

// 5. a #hash opens the section that holds it, scrolls to it, and is not remembered
{
  const st = memStorage({});
  const p = page({ hash: '#admin-users-rows', storage: st });  // an element INSIDE the section
  eq('#hash on a child opens its section', p.users.open, true);
  eq('... and scrolls to the child', p.inner.scrolled, 1);
  p.users.dispatch('toggle');
  eq('... and that is not saved as a choice', st.sets, 0);
  const q = page({ hash: '#admin-users-section', storage: memStorage({}) });
  eq('#hash on the section itself opens it', q.users.open, true);
  const bad = page({ hash: '#%E0%A4%A', storage: memStorage({}) });
  eq('a malformed #hash opens nothing and does not throw', bad.users.open, false);
  const none = page({ hash: '#no-such-element', storage: memStorage({}) });
  eq('a #hash naming nothing opens nothing', none.users.open, false);
  // hashchange and a click on a same-page link
  const r = page({ storage: memStorage({}) });
  r.win.location.hash = '#admin-users-section';
  r.winListeners.hashchange.forEach((fn) => fn());
  eq('hashchange opens the section', r.users.open, true);
  const s = page({ storage: memStorage({}) });
  s.docListeners.click.forEach((fn) => fn({ target: s.link }));
  eq('a click on a link to the hash you are already on opens the section', s.users.open, true);
}

// 6. Tools rows
{
  const p = page({ storage: memStorage({}) });
  let stopped = 0, prevented = 0;
  p.rowOk.dispatch('click', { preventDefault() { prevented++; }, stopPropagation() { stopped++; } });
  eq('a Tools row clicks the hidden trigger, once', p.trigger.clicks, 1);
  eq('... stopping the click so an outside-click closer cannot shut the drawer at once', [prevented, stopped], [1, 1]);
  eq('... and leaves the message hidden', p.msg.hidden, true);
  p.rowMissing.dispatch('click', { preventDefault() {}, stopPropagation() {} });
  eq('a row whose trigger is missing is not a dead click: it says so', p.msg.hidden, false);
  eq('... naming the tool and what to do', /^Export can't open here: .*Reload the page/.test(p.msg.textContent), true);
  p.rowOk.dispatch('click', { preventDefault() {}, stopPropagation() {} });
  eq('a later working click clears the message', p.msg.hidden, true);
}

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exitCode = failed ? 1 : 0;
