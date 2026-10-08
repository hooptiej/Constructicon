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
win.MountainTime = require(path.join(ROOT, 'web/static/js/mountain-time.js'));
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
  // a pile looks like a pile: one count badge per stack, and its members are marked as stacked
  const badges = container.qa('project-video-timeline-stack-count');
  ok('a stack shows ONE count badge with its size', badges.length === 1 && badges[0].textContent === '2', badges.map((b) => b.textContent));
  ok('stack members carry the stack class', container.qa('project-video-timeline-event--stack').length === 2);
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

  // 3. a stack asks which one: the PRESS opens the chooser at once (no wait for the release),
  // and the click that follows the release neither closes it nor navigates.
  events[1]._el.fire('pointerdown', { button: 0, clientX: 250, pointerId: 9 });
  ok('pressing a stack opens the chooser immediately', bodyEl.qa('timeline-chooser').length === 1);
  events[1]._el.fire('click', {});
  const chooser = bodyEl.qa('timeline-chooser')[0];
  ok('the click after the press keeps the chooser open', !!chooser && chooser.qa('timeline-chooser-row').length === 2);
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

  // 8. #631: a timeline that spans about a minute. Dragging can't move anything, so say so up front,
  // don't dangle the drag affordances, and explain a drag that ends where it started.
  const msgs = [];
  const T0 = ep(2026, 10, 6, 17, 39);
  const shortSaved = [];
  const shortEvents = [
    { id: 's1', label: 'Shot one', date: T0, dateLabel: '', openUrl: '/object/s1' },
    { id: 's2', label: 'Shot two', date: T0 + 0.5, dateLabel: '', openUrl: '/object/s2' },
    { id: 's3', label: 'Shot three', date: T0 + 60, dateLabel: '', openUrl: '/object/s3' },
  ];
  const cShort = new El('div');
  const tlS = new PVT(cShort, { events: shortEvents, canEdit: true,
    onSave: async (e, d) => { shortSaved.push([e.id, d]); return { effective_date: d, set_by_hand: true, batch_id: 'S' }; },
    onNoMove: (m, info) => msgs.push({ m, info }) });
  tlS._eventRow.rect = { left: 0, top: 100, width: 1000, height: 20, right: 1000, bottom: 120 };
  const note = cShort.qa('project-video-timeline-note');
  ok('short timeline: a note is shown', note.length === 1, note.length);
  ok('note says the timeline is shorter than one day and dragging cannot move items',
    /shorter than one day/.test(note[0].textContent) && /can't move items/.test(note[0].textContent), note[0] && note[0].textContent);
  ok('note names the DATES group and the Timeline date field in the item\'s Details',
    /Details/.test(note[0].textContent) && /DATES/.test(note[0].textContent) && /Timeline date/.test(note[0].textContent));
  ok('short timeline: no marker offers the grab cursor class', cShort.qa('project-video-timeline-event--editable').length === 0);
  ok('short timeline: the stack hint does not say "to drag"', shortEvents.every((e) => !/to drag/.test(e._el.title || '')) && /click to choose one/.test(shortEvents[0]._el.title));
  bodyEl.qa("timeline-chooser").forEach((c) => c.remove()); // the viewer chooser left open by section 7
  const s1 = shortEvents[0]._el;
  s1.fire('pointerdown', { button: 0, clientX: 0, pointerId: 3 }); // stack: opens the chooser, as before
  ok('short timeline: clicking a stack still opens the chooser', bodyEl.qa('timeline-chooser').length === 1);
  ok('chooser does not promise a drag', !/drag/.test(bodyEl.qa('timeline-chooser')[0].textContent) && /arrow keys/.test(bodyEl.qa('timeline-chooser')[0].textContent));
  s1.fire('click', {});
  bodyEl.qa('timeline-chooser')[0].qa('timeline-chooser-pick')[0].fire('click', {}); // pick Shot one
  ok('short timeline: picking still works', tlS._picked === 's1');
  ok('picked readout does not say "drag it"', !/drag it/.test(tlS._readout.textContent) && /arrow keys/.test(tlS._readout.textContent), tlS._readout.textContent);
  const pS = shortEvents[0]._el;
  pS.fire('pointerdown', { button: 0, clientX: 0, pointerId: 4 });
  pS.fire('pointermove', { clientX: 900 });
  pS.fire('pointerup', {});
  await new Promise((r) => setTimeout(r, 5));
  ok('short timeline: a drag saves nothing', shortSaved.length === 0, shortSaved);
  ok('short timeline: a drag reports why it did not move', msgs.length === 1 && /Shot one/.test(msgs[0].m) && /shorter than one day/.test(msgs[0].m)
    && /Timeline date/.test(msgs[0].m) && msgs[0].info.mode === 'pointer', msgs);
  ok('the reason is Mountain-time free of surprises: no raw epoch numbers', !/\d{9,}/.test(msgs[0].m));

  // 9. a normal multi-month timeline: still drags, still re-dates, no note
  const nSaved = [];
  const cLong = new El('div');
  const longEvents = [
    { id: 'l1', label: 'Long one', date: ep(2025, 1, 1), dateLabel: '', openUrl: '/object/l1' },
    { id: 'l2', label: 'Long two', date: ep(2025, 12, 1), dateLabel: '', openUrl: '/object/l2' },
  ];
  const longMsgs = [];
  const tlL = new PVT(cLong, { events: longEvents, canEdit: true,
    onSave: async (e, d) => { nSaved.push([e.id, d]); return { effective_date: d, set_by_hand: true, batch_id: 'L' }; },
    onNoMove: (m) => longMsgs.push(m) });
  tlL._eventRow.rect = { left: 0, top: 100, width: 1000, height: 20, right: 1000, bottom: 120 };
  ok('long timeline: no note', cLong.qa('project-video-timeline-note').length === 0);
  ok('long timeline: markers keep the drag affordance', cLong.qa('project-video-timeline-event--editable').length === 2);
  const l1 = longEvents[0]._el;
  l1.fire('pointerdown', { button: 0, clientX: 0, pointerId: 5 });
  l1.fire('pointermove', { clientX: 500 });
  l1.fire('pointerup', {});
  await new Promise((r) => setTimeout(r, 5));
  ok('long timeline: a real drag still re-dates', nSaved.length === 1 && nSaved[0][0] === 'l1' && nSaved[0][1] > ep(2025, 5, 1), nSaved);
  ok('long timeline: a real drag gives no "did not move" message', longMsgs.length === 0, longMsgs);

  // 10. a zero-distance drag on a long timeline: out 100px and back to the start
  const l2 = longEvents[1]._el;
  l2.fire('pointerdown', { button: 0, clientX: 1000, pointerId: 6 });
  l2.fire('pointermove', { clientX: 900 });
  l2.fire('pointermove', { clientX: 1000 });
  l2.fire('pointerup', {});
  await new Promise((r) => setTimeout(r, 5));
  ok('long timeline: a drag that returns to the start saves nothing', nSaved.length === 1, nSaved);
  ok('long timeline: and says why, naming the day-step and the Details route',
    longMsgs.length === 1 && /Long two/.test(longMsgs[0]) && /same day/.test(longMsgs[0]) && /whole days/.test(longMsgs[0]) && /Timeline date/.test(longMsgs[0]), longMsgs);
  ok('long timeline message is not the short-timeline one', !/shorter than one day/.test(longMsgs[0]));
  // a Shift drag that lands back on its 15-minute step names that step, not the day
  const l2b = longEvents[1]._el; // the no-op re-rendered the markers
  l2b.fire('pointerdown', { button: 0, clientX: 1000, pointerId: 7 });
  l2b.fire('pointermove', { clientX: 900, shiftKey: true });
  l2b.fire('pointermove', { clientX: 1000, shiftKey: true });
  l2b.fire('pointerup', {});
  await new Promise((r) => setTimeout(r, 5));
  // (the fine snap of an arbitrary epoch is a 15-minute multiple, so it only counts as a no-op when
  // the item already sits on one; this one is at :39 so it moves, which is the existing behaviour)
  ok('long timeline: a Shift drag keeps working as before', nSaved.length === 2 && nSaved[1][0] === 'l2', nSaved);

  // 11. no onNoMove hook: the reason still shows, in the drag readout
  const tlN = new PVT(new El('div'), { events: shortEvents.map((e) => ({ ...e })), canEdit: true, onSave: async () => ({}) });
  tlN._eventRow.rect = { left: 0, top: 100, width: 1000, height: 20, right: 1000, bottom: 120 };
  tlN._setPending(tlN.events[0], tlN.events[0].date, 'pointer');
  await tlN._commit();
  ok('without a page hook the readout carries the reason', /shorter than one day/.test(tlN._readout.textContent), tlN._readout.textContent);

  // 12. a viewer never sees the note
  const cV = new El('div');
  new PVT(cV, { events: shortEvents.map((e) => ({ ...e })), canEdit: false });
  ok('viewer: no note on a short timeline', cV.qa('project-video-timeline-note').length === 0);

  console.log(failed ? `\n${failed} FAILED` : '\nall passed');
  process.exit(failed ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(2); });
