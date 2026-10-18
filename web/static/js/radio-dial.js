// The home page timeline as a sliding radio dial (#775 "Tune in to the past", pass 1). Replaces the
// old vertical left rail (timeline-rail.js, retired here). Styled like the card page's tuner
// (project-video-timeline.js): near-black chassis, hazard-yellow axis and needle, phosphor-green
// readouts.
//
// A fixed needle sits at the window's centre; the band (years, month ticks, one tick per card at its
// start date) slides under it. About a decade shows at once (DIAL_DEFAULTS.visibleYears; fewer on a
// narrow screen so a year label always fits). Drag (pointer events: mouse, touch, pen), the wheel or
// trackpad, and the arrow keys (when focused) slide it. BROWSE ONLY: the dial never scrolls, filters
// or changes the card gallery; the readout under the needle names what's there and links to it.
//
// Starts UNTUNED (owner decision on #775): the needle sits mid-range and the readout shows static
// until the user moves the dial. Marks never grow or zoom (#775's lesson comment): they're static,
// the readout carries the detail.
//
// Pass 2 (not built) hooks: _renderSpans() fills .radio-dial-layer-spans with start-to-end bars from
// card.end; _renderDensity() fills .radio-dial-layer-density with a per-year glow. Both layers are
// already in the band, under the ticks, and are laid out with the same bandX maths.
//
// The maths is radio-dial-math.js (pure, node-tested by scripts/test_radio_dial_775.js).

// Every feel value in one place (#775: "expose them ... with a live tuning slider on dev"). The dev
// tuning panel (radio-dial-tuning.js) edits these live through RadioDial#setTuning.
const DIAL_DEFAULTS = {
  visibleYears: 10,     // years visible at once ("about a decade")
  minPxPerYear: 64,     // never narrower than this per year, so every year label fits
  monthTickMinPx: 9,    // month minor ticks only when a month is at least this wide
  tolerancePx: 10,      // the readout names cards whose tick is within this many px of the needle
  readoutMax: 4,        // names listed before "+N more"
  friction: 0.92,       // inertia after a flick: share of speed kept per frame (0 = stops dead)
  snapStrength: 0,      // 0 = free needle; 1 = settles right onto the nearest card once still
  snapPx: 24,           // ... but only when that card is this close (px)
  wheelSpeed: 1,        // wheel / trackpad px -> dial px
  glow: 0.10,           // chassis backlight intensity (CSS --dial-glow)
};

// The sliders the dev tuning panel offers, in order.
const DIAL_TUNABLES = [
  { key: 'visibleYears', label: 'Visible span (years)', min: 2, max: 40, step: 1 },
  { key: 'minPxPerYear', label: 'Min px per year', min: 30, max: 160, step: 1 },
  { key: 'monthTickMinPx', label: 'Month ticks from (px per month)', min: 2, max: 40, step: 1 },
  { key: 'tolerancePx', label: 'Readout tolerance (px)', min: 1, max: 60, step: 1 },
  { key: 'readoutMax', label: 'Names before "+N more"', min: 1, max: 12, step: 1 },
  { key: 'friction', label: 'Inertia (0 = none)', min: 0, max: 0.98, step: 0.01 },
  { key: 'snapStrength', label: 'Snap strength (0 = free)', min: 0, max: 1, step: 0.05 },
  { key: 'snapPx', label: 'Snap reach (px)', min: 2, max: 120, step: 1 },
  { key: 'wheelSpeed', label: 'Wheel speed', min: 0.1, max: 4, step: 0.1 },
  { key: 'glow', label: 'Backlight glow', min: 0, max: 0.5, step: 0.01 },
];

const DIAL_DRAG_CLICK_PX = 3;   // a press that moves less than this isn't a drag
const DIAL_MIN_FLICK = 0.05;    // px/ms: slower than this on release = no inertia

class RadioDial {
  // root: the .radio-dial element (its .radio-dial-chassis is filled here). rawCards: the page's
  // timeline_projects rows. opts.formatDate(epoch) -> text (MountainTime.formatDate).
  constructor(root, rawCards, { formatDate } = {}) {
    const M = window.RadioDialMath;
    this.M = M;
    this.root = root;
    this.formatDate = formatDate || ((e) => new Date(e * 1000).toLocaleDateString());
    this.tuning = { ...DIAL_DEFAULTS };
    this.chassis = root.querySelector('.radio-dial-chassis');
    const parsed = M.parseCards(rawCards);
    this.cards = parsed.cards;
    // Never fail silently (#775 item 9): malformed or empty data says so in the chassis.
    if (parsed.malformed) { this._fail('The timeline data could not be read (it was not a list). Reload the page; if this stays, the home page route is sending bad data.'); return; }
    if (!this.cards.length) {
      this._fail(parsed.skipped
        ? `None of the ${parsed.skipped} dated cards had a readable start date, so the dial has nothing to show.`
        : 'No dated cards yet: the dial lights up once a card has a real date.');
      return;
    }
    this.skipped = parsed.skipped;
    this.rng = M.range(this.cards);
    this.needle = (this.rng.first + this.rng.last) / 2;  // untuned: mid-range, nothing previewed
    this.tuned = false;
    this._readoutKey = null;
    this._anim = null;
    this._build();
    this._layout();
    let t = null;
    window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => this._layout(), 120); });
  }

  _fail(message) {
    this.chassis.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'radio-dial-message';
    p.setAttribute('role', 'status');
    p.textContent = message;
    this.chassis.appendChild(p);
    this.failed = true;
  }

  _build() {
    const el = (tag, cls, parent, attrs) => {
      const n = document.createElement(tag);
      n.className = cls;
      Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
      if (parent) parent.appendChild(n);
      return n;
    };
    const hidden = { 'aria-hidden': 'true' };
    this.chassis.innerHTML = '';
    // window (focusable slider) > band (slides) > layers; the needle stays put over the band.
    this.win = el('div', 'radio-dial-window', this.chassis, { tabindex: '0', role: 'slider',
      'aria-label': 'Timeline dial: drag, scroll or use the arrow keys to tune through the years' });
    this.band = el('div', 'radio-dial-band', this.win);
    el('div', 'radio-dial-layer-density', this.band, hidden);  // pass 2 hook
    el('div', 'radio-dial-layer-spans', this.band, hidden);    // pass 2 hook
    el('div', 'radio-dial-axis', this.band, hidden);
    this.ticks = el('div', 'radio-dial-ticks', this.band, hidden);
    el('div', 'radio-dial-needle', this.win, hidden);
    this.readout = el('div', 'radio-dial-readout', this.chassis, { 'aria-live': 'polite' });
    if (this.skipped) {
      const note = el('p', 'radio-dial-note', this.chassis);
      note.textContent = `${this.skipped} card${this.skipped === 1 ? '' : 's'} left off the dial: unreadable start date.`;
    }
    this.win.setAttribute('aria-valuemin', String(this.rng.firstYear));
    this.win.setAttribute('aria-valuemax', String(this.rng.lastYear));
    this._wirePointer();
    this._wireWheel();
    this._wireKeys();
  }

  // --- layout: recomputed on load, resize and tuning changes; sliding only moves the band ---

  _layout() {
    if (this.failed) return;
    const M = this.M;
    this.width = this.win.clientWidth || 1;
    this.pps = M.pxPerSecond(this.width, this.tuning.visibleYears, this.tuning.minPxPerYear);
    this.band.style.width = `${Math.ceil(M.bandX(this.rng.end, this.rng, this.pps))}px`;
    const x = (e) => M.bandX(e, this.rng, this.pps).toFixed(1);
    const parts = [];
    M.monthTicks(this.rng, this.pps, this.tuning.monthTickMinPx).forEach((m) => {
      parts.push(`<span class="radio-dial-month" style="left:${x(m.epoch)}px"></span>`);
    });
    M.yearTicks(this.rng).forEach((y) => {
      parts.push(`<span class="radio-dial-year" style="left:${x(y.epoch)}px"><span class="radio-dial-year-mark"></span><span class="radio-dial-year-label">${y.year}</span></span>`);
    });
    this.cards.forEach((c) => {
      parts.push(`<span class="radio-dial-card${c.isChild ? ' radio-dial-card--child' : ''}" style="left:${x(c.start)}px"></span>`);
    });
    this.ticks.innerHTML = parts.join('');
    this._renderSpans();
    this._renderDensity();
    this._readoutKey = null;
    this._place();
  }

  _renderSpans() { /* pass 2 (#775): start-to-end bars from card.end into .radio-dial-layer-spans */ }

  _renderDensity() { /* pass 2 (#775): per-year density glow into .radio-dial-layer-density */ }

  _place() {
    const offset = this.M.bandOffset(this.needle, this.rng, this.pps, this.width);
    this.band.style.transform = `translateX(${offset.toFixed(1)}px)`;
    const label = this.M.needleLabel(this.needle);  // "Mar 2004"
    this.win.setAttribute('aria-valuenow', label.slice(-4));
    this.win.setAttribute('aria-valuetext', this.tuned ? label : 'untuned');
    this._renderReadout();
  }

  _renderReadout() {
    const M = this.M;
    if (!this.tuned) {
      if (this._readoutKey === 'untuned') return;
      this._readoutKey = 'untuned';
      this.readout.innerHTML = '<span class="radio-dial-static">~ untuned ~ slide the dial to tune in</span>';
      return;
    }
    const here = M.cardsAtNeedle(this.cards, this.needle, this.pps, this.tuning.tolerancePx);
    const label = M.needleLabel(this.needle);
    const key = label + '|' + here.map((c) => c.slug).join(',');
    if (key === this._readoutKey) return;
    this._readoutKey = key;
    this.readout.innerHTML = '';
    const freq = document.createElement('span');
    freq.className = 'radio-dial-freq';
    freq.textContent = label;
    this.readout.appendChild(freq);
    if (!here.length) {
      const quiet = document.createElement('span');
      quiet.className = 'radio-dial-quiet';
      quiet.textContent = 'no cards here';
      this.readout.appendChild(quiet);
      return;
    }
    const { shown, more } = M.capList(here, this.tuning.readoutMax);
    const list = document.createElement('ul');
    list.className = 'radio-dial-stations';
    shown.forEach((c) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.className = 'radio-dial-station';
      a.href = `/project/${encodeURIComponent(c.slug)}`;
      a.textContent = c.title;
      const d = document.createElement('span');
      d.className = 'radio-dial-station-date';
      d.textContent = this.formatDate(c.start);
      li.append(a, d);
      list.appendChild(li);
    });
    this.readout.appendChild(list);
    if (more) {
      const m = document.createElement('span');
      m.className = 'radio-dial-more';
      m.textContent = `+${more} more`;
      m.title = here.slice(shown.length).map((c) => c.title).join(', ');
      this.readout.appendChild(m);
    }
  }

  // --- moving the needle ---

  _setNeedle(epoch) {
    this.needle = this.M.clamp(epoch, this.rng.first, this.rng.last);
    this.tuned = true;
    this._place();
  }

  _stopAnim() { if (this._anim) cancelAnimationFrame(this._anim); this._anim = null; }

  // Inertia after a flick: velocity in px/ms of band movement, decayed by `friction` per ~16 ms frame.
  _coast(velocity) {
    this._stopAnim();
    const friction = this.tuning.friction;
    if (!friction || Math.abs(velocity) < DIAL_MIN_FLICK) { this._settle(); return; }
    let v = velocity, last = performance.now();
    const step = (now) => {
      const dt = Math.min(48, now - last); last = now;
      const before = this.needle;
      this._setNeedle(this.M.needleAfterDrag(this.needle, v * dt, this.pps, this.rng));
      v *= Math.pow(friction, dt / 16);
      if (Math.abs(v) < DIAL_MIN_FLICK / 2 || this.needle === before) { this._anim = null; this._settle(); return; }
      this._anim = requestAnimationFrame(step);
    };
    this._anim = requestAnimationFrame(step);
  }

  // Once still: pull toward the nearest card by snapStrength, if it's within snapPx (0 = free needle).
  _settle() {
    const s = this.tuning.snapStrength;
    if (!s || !this.tuned) return;
    const near = this.M.nearestCard(this.cards, this.needle);
    if (!near || Math.abs(near.start - this.needle) * this.pps > this.tuning.snapPx) return;
    const from = this.needle, to = from + (near.start - from) * s, t0 = performance.now(), dur = 180;
    this._stopAnim();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur);
      this._setNeedle(from + (to - from) * (1 - Math.pow(1 - k, 3)));
      this._anim = k < 1 ? requestAnimationFrame(step) : null;
    };
    this._anim = requestAnimationFrame(step);
  }

  _wirePointer() {
    let drag = null;
    this.win.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      this._stopAnim();
      drag = { id: e.pointerId, x: e.clientX, startX: e.clientX, t: performance.now(), v: 0, moved: false };
      this.win.setPointerCapture(e.pointerId);
      this.win.classList.add('radio-dial-window--dragging');
    });
    this.win.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = e.clientX - drag.x, now = performance.now();
      if (!drag.moved && Math.abs(e.clientX - drag.startX) < DIAL_DRAG_CLICK_PX) return;
      drag.moved = true;
      const dt = Math.max(1, now - drag.t);
      drag.v = 0.8 * (dx / dt) + 0.2 * drag.v;  // smoothed px/ms for the flick
      drag.x = e.clientX; drag.t = now;
      this._setNeedle(this.M.needleAfterDrag(this.needle, dx, this.pps, this.rng));
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const flick = drag.moved && performance.now() - drag.t < 80 ? drag.v : 0;
      drag = null;
      this.win.classList.remove('radio-dial-window--dragging');
      this._coast(flick);
    };
    this.win.addEventListener('pointerup', end);
    this.win.addEventListener('pointercancel', end);
  }

  _wireWheel() {
    let idle = null;
    this.win.addEventListener('wheel', (e) => {
      const dx = Math.abs(e.deltaX) >= Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      if (!dx) return;
      e.preventDefault();
      this._stopAnim();
      const scale = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? this.width : 1;
      // Scrolling right / down tunes forward in time: the band moves left.
      this._setNeedle(this.M.needleAfterDrag(this.needle, -dx * scale * this.tuning.wheelSpeed, this.pps, this.rng));
      clearTimeout(idle);
      idle = setTimeout(() => this._settle(), 160);
    }, { passive: false });
  }

  _wireKeys() {
    this.win.addEventListener('keydown', (e) => {
      const next = this.M.keyStep(this.needle, e.key, e.shiftKey, this.rng);
      if (next === null) return;
      e.preventDefault();
      this._stopAnim();
      this._setNeedle(next);
    });
  }

  // Live tuning (the dev panel): merge, push the CSS variables, re-lay the band.
  setTuning(partial) {
    Object.keys(partial || {}).forEach((k) => {
      if (k in DIAL_DEFAULTS && isFinite(Number(partial[k]))) this.tuning[k] = Number(partial[k]);
    });
    this.root.style.setProperty('--dial-glow', String(this.tuning.glow));
    this._layout();
  }
}

RadioDial.DEFAULTS = DIAL_DEFAULTS;
RadioDial.TUNABLES = DIAL_TUNABLES;
window.RadioDial = RadioDial;
