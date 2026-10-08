// Drives ProjectVideoTimeline (#593) against a tiny fake DOM: no browser, no dependencies.
//   node scripts/test_timeline_drag_ui.js
// Covers the pointer drag, the stack chooser and picking, the keyboard (arrows, Enter, Esc, Delete
// to reset), a failed save, and the viewer's static timeline. The date maths has its own test,
// scripts/test_timeline_drag.js.
process.env.TZ = process.env.TZ || 'America/Denver';
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = path.join(__dirname, '..');

class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.parent = null; this.style = {}; this.dataset = {};
    this.attrs = {}; this.listeners = {}; this._cls = new Set(); this._text = ''; this.rect = null;
    const self = this;
    this.classList = { add: (...c) => c.forEach((x) => self._cls.add(x)), remove: (...c) => c.forEach((x) => self._cls.delete(x)),
      contains: (c) => self._cls.has(c) };
  }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._cls].join(' '); }
  set innerHTML(v) { this.children = []; this._text = ''; this._html = v; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f); }
  fire(t, ev = {}) { const e = { preventDefault() { this.prevented = true; }, target: this, ...ev }; (this.listeners[t] || []).slice().forEach((f) => f(e)); return e; }
  all(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.all(pred, out)); return out; }
  qa(cls) { return this.all((e) => e._cls.has(cls)); }
  querySelector(sel) { const c = sel.replace(/^\./, ''); return this.qa(c)[0] || null; }
  contains(o) { return o === this || this.children.some((c) => c.contains(o)); }
  focus() { doc.activeElement = this; }
  setPointerCapture() {}
  getBoundingClientRect() { return this.rect || { left: 0, top: 0, width: 20, height: 20, right: 20, bottom: 20 }; }
  get clientWidth() { return 1000; }
}
const bodyEl = new El('body');
const docListeners = {};
const doc = { createElement: (t) => new El(t), body: bodyEl, activeElement: bodyEl,
  addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); }, removeEventListener: () => {} };
const winListeners = {};
const win = { addEventListener: (t, f) => { (winListeners[t] = winListeners[t] || []).push(f); }, removeEventListener() {}, innerWidth: 1200, innerHeight: 800, location: { href: '' } };
const ctx = vm.createContext({ window: win, document: doc, console, setTimeout, clearTimeout, Math, Date, Map, Set, Promise });
win.TimelineDragMath = require(path.join(ROOT, 'web/static/js/timeline-drag-math.js'));
ctx.window = win;
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/project-video-timeline.js'), 'utf8') + '\nglobalThis.PVT = ProjectVideoTimeline;', ctx);
const PVT = ctx.PVT;

let failed = 0;
const ok = (n, c, x) => { console.log((c ? 'ok   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x))); if (!c) failed++; };
const ep = (y, m, d, h = 18, mi = 39) => new Date(y, m - 1, d, h, mi).getTime() / 1000;
const D1 = ep(2025, 1, 1);

async function main() {
  const saved = [];
  const afters = [];
  const container = new El('div');
  const events = [
    { id: 'a', label: 'Alpha', date: ep(2025, 1, 1), thumbUrl: null, dateLabel: 'x', openUrl: '/object/a', setByHand: false },
    { id: 'b', label: 'Bravo', date: ep(2025, 6, 1), thumbUrl: '/t/b', dateLabel: 'x', openUrl: '/object/b', setByHand: true },
    { id: 'c', label: 'Charlie', date: ep(2025, 6, 2), thumbUrl: null, dateLabel: 'x', openUrl: '/object/c', setByHand: false },
    { id: 'z', label: 'Zulu', date: ep(2025, 12, 1), thumbUrl: null, dateLabel: 'x', openUrl: '/object/z', setByHand: false },
  ];
  const tl = new PVT(container, {
    blocks: [], events, canEdit: true,
    onSave: async (event, epoch) => { saved.push([event.id, epoch]); return { effective_date: epoch === null ? D1 : epoch, set_by_hand: epoch !== null, batch_id: 'B' + saved.length }; },
    onSaved: (info) => afters.push(info),
  });
  const row = tl._eventRow;
  row.rect = { left: 0, top: 100, width: 1000, height: 20, right: 1000, bottom: 120 };
  ok('rendered four markers', container.qa('project-video-timeline-event').length === 4);
  ok('hand-set marker is ringed', container.qa('project-video-timeline-event--hand').length === 1);
  ok('b and c form a stack, a does not', tl._stackOf(events[1]).length === 2 && tl._stackOf(events[0]).length === 1);

  // 1. drag a (single) to ~ 40% of the axis
  let elA = events[0]._el;
  elA.fire('pointerdown', { button: 0, clientX: 0, pointerId: 1 });
  elA.fire('pointermove', { clientX: 2, shiftKey: false });
  ok('a 2px wiggle is not a drag', tl._pending === null);
  elA.fire('pointermove', { clientX: 400, shiftKey: false });
  ok('a real move sets a pending date', tl._pending && tl._pending.event.id === 'a', tl._pending);
  ok('the readout is visible', tl._readout.classList.contains('visible') && /2025/.test(tl._readout.textContent), tl._readout && tl._readout.textContent);
  const pend = tl._pending.date;
  const dd = new Date(pend * 1000);
  ok('pending date is day-snapped with a 6:39 PM time of day', dd.getHours() === 18 && dd.getMinutes() === 39, dd.toString());
  elA.fire('pointerup', {});
  const clickAfterDrag = elA.fire('click', {});
  await new Promise((r) => setTimeout(r, 5));
  ok('onSave called once with the snapped epoch', saved.length === 1 && saved[0][0] === 'a' && saved[0][1] === pend, saved);
  ok('onSaved fired with before and not reset', afters.length === 1 && afters[0].before === D1 && afters[0].reset === false);
  ok('marker re-rendered at the new date and ringed', events[0].date === pend && events[0].setByHand === true && events[0]._el.classList.contains('project-video-timeline-event--hand'));
  ok('the click that follows a drag does not open the item', win.location.href === '' && clickAfterDrag.prevented);
  void clickAfterDrag;

  // 2. click on a plain marker navigates
  events[3]._el.fire('click', {});
  ok('plain click opens the item', win.location.href === '/object/z', win.location.href);
  win.location.href = '';

  // 3. a stack asks which one
  events[1]._el.fire('click', {});
  const chooser = bodyEl.qa('timeline-chooser')[0];
  ok('click on a stack opens the chooser', !!chooser && chooser.qa('timeline-chooser-row').length === 2);
  ok('chooser lists names', /Bravo/.test(chooser.textContent) && /Charlie/.test(chooser.textContent));
  ok('the hand-set member has a Reset button, the other does not', chooser.qa('timeline-chooser-reset').length === 1);
  ok('click on a stack did not navigate', win.location.href === '');
  chooser.qa('timeline-chooser-pick')[1].fire('click', {});   // pick Charlie
  ok('chooser closed after a pick and Charlie is picked', bodyEl.qa('timeline-chooser').length === 0 && tl._picked === 'c');
  const elC = events[2]._el;
  ok('picked marker is raised', events[2]._wrap.classList.contains('project-video-timeline-event-wrap--picked'));
  elC.fire('pointerdown', { button: 0, clientX: 500, pointerId: 2 });
  elC.fire('pointermove', { clientX: 700 });
  elC.fire('pointerup', {});
  await new Promise((r) => setTimeout(r, 5));
  ok('the picked member saved, not the other', saved.length === 2 && saved[1][0] === 'c', saved);
  ok('pick cleared after the save', tl._picked === null);

  // 4. keyboard
  const elZ = events[3]._el;
  elZ.fire('keydown', { key: 'ArrowRight', shiftKey: false });
  const p1 = tl._pending.date;
  ok('ArrowRight +1 day', Math.round((p1 - events[3].date) / 86400) === 1, p1 - events[3].date);
  elZ.fire('keydown', { key: 'ArrowRight', shiftKey: true });
  ok('Shift+Right adds a month on top', new Date(tl._pending.date * 1000).getMonth() === 0, new Date(tl._pending.date * 1000).toString());
  const esc = elZ.fire('keydown', { key: 'Escape' });
  ok('Escape cancels', tl._pending === null && saved.length === 2 && esc.prevented);
  const z2 = events[3]._el;
  z2.fire('keydown', { key: 'ArrowLeft', shiftKey: false });
  const want = tl._pending.date;
  const en = z2.fire('keydown', { key: 'Enter' });
  await new Promise((r) => setTimeout(r, 5));
  ok('Enter commits (and does not click through)', saved.length === 3 && saved[2][0] === 'z' && saved[2][1] === want && en.prevented, saved);

  // 5. reset by Delete
  const elA2 = events[0]._el;
  elA2.fire('keydown', { key: 'Delete' });
  await new Promise((r) => setTimeout(r, 5));
  ok('Delete on a hand-set marker resets (save with null)', saved.length === 4 && saved[3][0] === 'a' && saved[3][1] === null, saved[3]);
  ok('onSaved says reset', afters[afters.length - 1].reset === true);
  ok('marker no longer ringed', events[0].setByHand === false);

  // 6. a failed save puts it back
  const tl2 = new PVT(new El('div'), { events: [{ id: 'q', label: 'Q', date: D1, dateLabel: '', openUrl: '/q' }, { id: 'r', label: 'R', date: D1 + 100 * 86400, dateLabel: '', openUrl: '/r' }],
    canEdit: true, onSave: async () => { throw new Error('forbidden'); } });
  tl2._eventRow.rect = { left: 0, top: 0, width: 1000, height: 20, right: 1000, bottom: 20 };
  const q = tl2.events[0]._el;
  q.fire('keydown', { key: 'ArrowRight' });
  q.fire('keydown', { key: 'Enter' });
  await new Promise((r) => setTimeout(r, 5));
  ok('failed save shows the reason and keeps the old date', /not saved: forbidden/.test(tl2._readout.textContent) && tl2.events[0].date === D1, tl2._readout.textContent);

  // 7. viewer: static
  const tl3 = new PVT(new El('div'), { events: [{ id: 'v', label: 'V', date: D1, dateLabel: '', openUrl: '/v' }], canEdit: false });
  const v = tl3.events[0]._el;
  ok('viewer markers have no drag or key handlers', !(v.listeners.pointerdown || []).length && !(v.listeners.keydown || []).length);
  v.fire('click', {});
  ok('viewer click still opens', win.location.href === '/v');
  win.location.href = '';
  const tl4 = new PVT(new El('div'), { events: [{ id: 's1', label: 'S1', date: D1, dateLabel: '', openUrl: '/s1' }, { id: 's2', label: 'S2', date: D1, dateLabel: '', openUrl: '/s2' }], canEdit: false });
  tl4.events[0]._el.fire('click', {});
  const vc = bodyEl.qa('timeline-chooser')[0];
  ok('viewer gets the chooser as links, no Move/Reset', !!vc && vc.qa('timeline-chooser-reset').length === 0 && vc.qa('timeline-chooser-pick')[0].tagName === 'a');

  console.log(failed ? `\n${failed} FAILED` : '\nall passed');
  process.exit(failed ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(2); });
