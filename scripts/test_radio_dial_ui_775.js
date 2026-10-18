// Drives RadioDial (#775 pass 1, web/static/js/radio-dial.js) against a tiny fake DOM: no browser,
// no dependencies (the same approach as scripts/test_timeline_drag_ui.js).
//   node scripts/test_radio_dial_ui_775.js
// Covers the untuned start, drag / wheel / keys tuning in, the readout (names, links, dates, "+N
// more", "no cards here"), snapping, live tuning, and the clear messages for empty or bad data.
// The maths has its own test, scripts/test_radio_dial_775.js.
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = path.join(__dirname, '..');

class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.parent = null; this.attrs = {}; this.listeners = {};
    this._cls = new Set(); this._text = ''; this._html = '';
    const style = {};
    style.setProperty = (k, v) => { style[k] = v; };
    this.style = style;
    const self = this;
    this.classList = { add: (...c) => c.forEach((x) => self._cls.add(x)), remove: (...c) => c.forEach((x) => self._cls.delete(x)),
      contains: (c) => self._cls.has(c) };
  }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._cls].join(' '); }
  set innerHTML(v) { this.children = []; this._text = ''; this._html = String(v); }
  get innerHTML() { return this._html; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, ev = {}) { const e = { prevented: false, preventDefault() { this.prevented = true; }, target: this, ...ev }; (this.listeners[t] || []).slice().forEach((f) => f(e)); return e; }
  all(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach((c) => c.all(pred, out)); return out; }
  qa(cls) { return this.all((e) => e._cls.has(cls)); }
  querySelector(sel) { return this.qa(sel.replace(/^\./, ''))[0] || null; }
  setPointerCapture() {}
  get clientWidth() { return 1000; }
}

let now = 0;
let frames = [];
const flushFrames = (n = 200) => { for (let i = 0; i < n && frames.length; i++) { now += 16; const f = frames; frames = []; f.forEach((cb) => cb(now)); } };
const doc = { createElement: (t) => new El(t) };
const win = { addEventListener() {} };
const ctx = vm.createContext({
  window: win, document: doc, console, setTimeout: () => 0, clearTimeout() {}, Math, Date, Number, String, Object, Array, JSON,
  isFinite, encodeURIComponent, performance: { now: () => now },
  requestAnimationFrame: (cb) => { frames.push(cb); return frames.length; }, cancelAnimationFrame: () => { frames = []; },
});
const MT = require(path.join(ROOT, 'web/static/js/mountain-time.js'));
win.RadioDialMath = require(path.join(ROOT, 'web/static/js/radio-dial-math.js'));
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/radio-dial.js'), 'utf8'), ctx);
const RadioDial = win.RadioDial;

let failed = 0;
const ok = (n, c, x) => { console.log((c ? 'ok   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x))); if (!c) failed++; };
const day = (y, m, d) => MT.toEpoch(y, m, d, 12, 0, 0);
const row = (slug, title, start, extra = {}) => ({ id: slug, slug, title, cover_url: null, effective_start: start, effective_end: null, is_child: false, ...extra });
const mount = (data, opts) => {
  const root = new El('section'); root.className = 'radio-dial';
  const chassis = new El('div'); chassis.className = 'radio-dial-chassis'; root.appendChild(chassis);
  const dial = new RadioDial(root, data, { formatDate: (e) => MT.formatDate(e, 'en-US'), ...opts });
  return { root, chassis, dial };
};
const readoutText = (d) => d.readout.textContent;

const ROWS = [
  row('radio', 'Old radio build', day(1999, 1, 1)),
  row('part', 'A part of it', day(1999, 1, 1), { is_child: true }),
  row('desk', 'Desk build', day(2005, 6, 1)),
  row('recent', 'Recent project', day(2024, 12, 24)),
];

// --- untuned start ---
const { root, chassis, dial } = mount(ROWS);
ok('builds the window, band, needle and readout', ['radio-dial-window', 'radio-dial-band', 'radio-dial-needle', 'radio-dial-readout'].every((c) => chassis.qa(c).length === 1));
ok('pass 2 layers are in the band, empty', chassis.qa('radio-dial-layer-spans').length === 1 && chassis.qa('radio-dial-layer-density').length === 1);
ok('the needle starts mid-range', dial.needle === (day(1999, 1, 1) + day(2024, 12, 24)) / 2);
ok('untuned: the readout shows static, no card names', /untuned/.test(dial.readout.innerHTML) && !/Desk build/.test(readoutText(dial)), dial.readout.innerHTML);
ok('untuned: aria-valuetext says so', dial.win.getAttribute('aria-valuetext') === 'untuned');
const ticks = dial.ticks.innerHTML;
ok('a year tick and label for every year 1999..2025', Array.from({ length: 27 }, (_, i) => 1999 + i).every((y) => ticks.includes(`>${y}</span>`)));
ok('one tick per card', (ticks.match(/class="radio-dial-card/g) || []).length === 4);
ok('the child card gets the child tick', (ticks.match(/radio-dial-card--child/g) || []).length === 1);
ok('no month ticks at a decade per 1000 px', !ticks.includes('radio-dial-month'));
ok('a decade across 1000 px = 100 px per year', Math.abs(dial.pps * RadioDial.DEFAULTS.visibleYears * 365.2425 * 86400 - 1000) < 1e-6);

// --- drag tunes in: dragging the band left moves forward in time ---
const before = dial.needle;
dial.win.fire('pointerdown', { pointerId: 1, clientX: 500, button: 0 });
now += 10; dial.win.fire('pointermove', { pointerId: 1, clientX: 450 });
now += 10; dial.win.fire('pointermove', { pointerId: 1, clientX: 400 });
now += 200; dial.win.fire('pointerup', { pointerId: 1, clientX: 400 });
ok('drag left 100 px = about a year later', Math.abs(dial.needle - before - 365.2425 * 86400) < 86400, (dial.needle - before) / 86400);
ok('after a drag the dial is tuned', dial.tuned === true && dial.win.getAttribute('aria-valuetext') !== 'untuned');
ok('the readout shows the needle month and year', /\b20\d\d\b/.test(dial.readout.qa('radio-dial-freq')[0].textContent));
ok('the dragging class is cleared on release', !dial.win.classList.contains('radio-dial-window--dragging'));

// --- keys: End lands on the last card, which the readout names and links ---
let e = dial.win.fire('keydown', { key: 'End', shiftKey: false });
ok('End is handled (default prevented)', e.prevented === true);
ok('End lands on the last card', dial.needle === day(2024, 12, 24));
const links = dial.readout.qa('radio-dial-station');
ok('the readout names the card at the needle', links.length === 1 && links[0].textContent === 'Recent project', links.map((l) => l.textContent));
ok('the name links to its card page', links[0] && links[0].href === '/project/recent');
ok('with its Mountain date', dial.readout.qa('radio-dial-station-date')[0].textContent === '12/24/2024');
e = dial.win.fire('keydown', { key: 'x' });
ok('other keys are left alone', e.prevented === false);
dial.win.fire('keydown', { key: 'ArrowLeft', shiftKey: true });
ok('Shift+ArrowLeft goes back a year', Math.abs(day(2024, 12, 24) - dial.needle - 365.2425 * 86400) < 1);
ok('between stations: "no cards here"', /no cards here/.test(readoutText(dial)), readoutText(dial));

// --- several cards at one spot, capped ---
dial.setTuning({ readoutMax: 1 });
dial.win.fire('keydown', { key: 'Home' });
ok('Home lands on the first card', dial.needle === day(1999, 1, 1));
ok('a pile lists up to readoutMax names', dial.readout.qa('radio-dial-station').length === 1);
const more = dial.readout.qa('radio-dial-more')[0];
ok('... then "+N more", with the rest in its tooltip', more && more.textContent === '+1 more' && /A part of it|Old radio build/.test(more.title), more && more.textContent);
dial.setTuning({ readoutMax: 4 });
ok('raising the cap lists both', dial.readout.qa('radio-dial-station').length === 2);

// --- wheel and trackpad ---
const atWheel = dial.needle;
e = dial.win.fire('wheel', { deltaX: 0, deltaY: 100, deltaMode: 0 });
ok('a wheel turn is captured (page does not scroll under it)', e.prevented === true);
ok('scrolling down tunes forward by the scroll amount', Math.abs(dial.needle - atWheel - 365.2425 * 86400) < 86400);
const atPad = dial.needle;
dial.win.fire('wheel', { deltaX: -50, deltaY: 3, deltaMode: 0 });
ok('a sideways trackpad swipe uses deltaX (left = back in time)', dial.needle < atPad);

// --- snapping: off by default, pulls onto the nearest card when on ---
dial._setNeedle(day(2005, 6, 1) + 20 * 86400);  // ~5.5 px from Desk build
dial._settle(); flushFrames();
ok('snap off by default: the needle stays where it was left', dial.needle === day(2005, 6, 1) + 20 * 86400);
dial.setTuning({ snapStrength: 1, snapPx: 24 });
dial._settle(); flushFrames();
ok('snap on: settles onto the nearest card', Math.abs(dial.needle - day(2005, 6, 1)) < 1, (dial.needle - day(2005, 6, 1)) / 86400);
dial._setNeedle(day(2012, 1, 1));
dial._settle(); flushFrames();
ok('snap only within snapPx', dial.needle === day(2012, 1, 1));
dial.setTuning({ snapStrength: 0 });

// --- a flick coasts, then stops ---
const atFlick = dial.needle;
dial.win.fire('pointerdown', { pointerId: 2, clientX: 500, button: 0 });
now += 16; dial.win.fire('pointermove', { pointerId: 2, clientX: 470 });
now += 16; dial.win.fire('pointermove', { pointerId: 2, clientX: 440 });
dial.win.fire('pointerup', { pointerId: 2, clientX: 440 });
const afterRelease = dial.needle;
flushFrames();
ok('a flick keeps going after release (inertia)', dial.needle > afterRelease && afterRelease > atFlick);
ok('and comes to rest', frames.length === 0);
dial.setTuning({ friction: 0 });
const atNoInertia = dial.needle;
dial.win.fire('pointerdown', { pointerId: 3, clientX: 500, button: 0 });
now += 16; dial.win.fire('pointermove', { pointerId: 3, clientX: 470 });
dial.win.fire('pointerup', { pointerId: 3, clientX: 470 });
const stopped = dial.needle; flushFrames();
ok('friction 0 = stops dead', dial.needle === stopped && stopped > atNoInertia);

// --- falloff (owner feedback on PR #782): a steady brake so the coast eases to a stop ---
// The same flick (1 px/ms, band moving right = back in time) from the same spot, frame by frame.
const coastRun = (falloff) => {
  dial.setTuning({ friction: 0.92, falloff });
  dial._setNeedle(day(2012, 1, 1));
  frames = [];
  dial._coast(1.0);
  const needles = [dial.needle], speeds = [];
  let n = 0;
  while (frames.length && n < 2000) { flushFrames(1); needles.push(dial.needle); speeds.push(dial.coastSpeed); n++; }
  return { n, needles, speeds, done: frames.length === 0 };
};
ok('falloff has a default, above 0', RadioDial.DEFAULTS.falloff > 0, RadioDial.DEFAULTS.falloff);
ok('falloff is on the tuning panel', RadioDial.TUNABLES.some((t) => t.key === 'falloff'));
const free = coastRun(0), braked = coastRun(RadioDial.DEFAULTS.falloff);
const px = (r) => ((r.needles[0] - r.needles.at(-1)) * dial.pps).toFixed(0);
console.log(`info: a 1 px/ms flick coasts ${free.n} frames / ${px(free)} px with no falloff, ${braked.n} frames / ${px(braked)} px with the default`);
ok('with falloff the coast ends sooner (fewer frames)', braked.n < free.n, [braked.n, free.n]);
ok('... and travels less far', (braked.needles[0] - braked.needles.at(-1)) < (free.needles[0] - free.needles.at(-1)));
ok('... but a flick still coasts (not killed)', braked.n > 5 && braked.needles[0] - braked.needles.at(-1) > 0, braked.n);
const monotonic = (xs) => xs.every((x, i) => i === 0 || x <= xs[i - 1]);
ok('the needle never reverses during the coast', monotonic(braked.needles) && monotonic(free.needles));
ok('speed never changes sign', braked.speeds.every((s) => s >= 0), braked.speeds.filter((s) => s < 0));
ok('speed only ever drops', braked.speeds.every((s, i) => i === 0 || s <= braked.speeds[i - 1]));
ok('the coast ends at rest (speed 0, no frame pending)', braked.done && braked.speeds.at(-1) === 0);
const steps = braked.needles.slice(1).map((x, i) => braked.needles[i] - x);
ok('it eases out: the last step is tiny (no jump at the stop)', steps.at(-1) * dial.pps < 1, steps.at(-1) * dial.pps);
ok('steps shrink toward the stop', monotonic(steps), steps.map((s) => +(s * dial.pps).toFixed(2)));
const heavy = coastRun(0.01);
ok('a heavier falloff stops sooner still', heavy.n < braked.n && heavy.done, [heavy.n, braked.n]);
dial.setTuning({ falloff: RadioDial.DEFAULTS.falloff });

// --- live tuning ---
dial.setTuning({ glow: 0.3, visibleYears: 5 });
ok('glow goes to the --dial-glow CSS variable', root.style['--dial-glow'] === '0.3');
ok('backlight has a default and a slider', RadioDial.DEFAULTS.backlight > 0 && RadioDial.TUNABLES.some((t) => t.key === 'backlight' && t.max >= 0.4));
dial.setTuning({ backlight: 0.42 });
ok('backlight goes to the --dial-backlight CSS variable, live', root.style['--dial-backlight'] === '0.42');
ok('backlight is separate from the outer glow', root.style['--dial-glow'] === '0.3');
dial.setTuning({ backlight: 0 });
ok('backlight 0 = off', root.style['--dial-backlight'] === '0');
ok('visible span re-lays the band (5 years = 200 px per year)', Math.abs(dial.pps * 365.2425 * 86400 - 200) < 1e-6);
ok('month ticks appear when a month is wide enough', dial.ticks.innerHTML.includes('radio-dial-month'));
dial.setTuning({ visibleYears: 'nonsense', bogus: 3 });
ok('bad tuning values and unknown keys are ignored', dial.tuning.visibleYears === 5 && !('bogus' in dial.tuning));
ok('every tunable has a default and a range', RadioDial.TUNABLES.every((t) => t.key in RadioDial.DEFAULTS && t.min < t.max));

// --- never a blank bar ---
const empty = mount([]);
ok('no cards: a clear message, no dial', empty.dial.failed && /No dated cards yet/.test(empty.chassis.textContent) && !empty.chassis.qa('radio-dial-window').length);
const bad = mount({ not: 'a list' });
ok('not a list: says the data could not be read', bad.dial.failed && /could not be read/.test(bad.chassis.textContent));
const allBad = mount([row('x', 'X', null), row('y', 'Y', 'soon')]);
ok('every row unreadable: says how many and why', allBad.dial.failed && /None of the 2/.test(allBad.chassis.textContent), allBad.chassis.textContent);
const someBad = mount([...ROWS, row('z', 'Z', 'garbage')]);
ok('some rows unreadable: the dial works and a note says how many were left off',
  !someBad.dial.failed && /1 card left off the dial/.test(someBad.chassis.qa('radio-dial-note')[0].textContent));
ok('the message element is a status for screen readers', empty.chassis.qa('radio-dial-message')[0].getAttribute('role') === 'status');

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exitCode = failed ? 1 : 0;
