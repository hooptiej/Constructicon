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
// Pass 2 (built in batch 14, "Dial It Up"): _renderSpans() fills .radio-dial-layer-spans with a
// start-to-end bar per card that has a real end (start-only cards stay a tick), stacked in rows above
// the ticks; _renderDensity() fills .radio-dial-layer-density with a per-year glow, brighter for years
// holding more cards. Both sit in the band under the ticks, laid out with the same bandX maths, and
// never grow or zoom. Their feel values are DIAL_DEFAULTS span* / density* (+ --dial-span-*,
// --dial-density-* on .radio-dial).
//
// The maths is radio-dial-math.js (pure, node-tested by scripts/test_radio_dial_775.js).

// Every feel value in one place (#775: "expose them ... with a live tuning slider on dev"). The dev
// tuning panel (radio-dial-tuning.js) edits these live through RadioDial#setTuning.
// Owner's tuning on dev, baked in (PR #782 review, 2026-10-18). Falloff and backlight are still the
// first guesses (the owner hasn't tried them yet).
const DIAL_DEFAULTS = {
  visibleYears: 5,      // years visible at once (owner's pick; #775 started at "about a decade")
  minPxPerYear: 30,     // never narrower than this per year, so every year label fits
  monthTickMinPx: 2,    // month minor ticks only when a month is at least this wide
  tolerancePx: 5,       // the readout names cards whose tick is within this many px of the needle
  readoutMax: 8,        // names listed before "+N more"
  friction: 0.92,       // inertia after a flick: share of speed kept per frame (0 = stops dead)
  falloff: 0.001,       // plus a steady braking (px/ms per ms) so the coast eases to a stop instead
                        // of drifting slowly and cutting off (owner feedback on PR #782; 0 = none)
  snapStrength: 0.5,    // 0 = free needle; 1 = settles right onto the nearest card once still
  snapPx: 28,           // ... but only when that card is this close (px)
  wheelSpeed: 0.1,      // wheel / trackpad px -> dial px
  glow: 0,              // outer glow around the chassis (CSS --tuner-glow; the owner turned it off)
  backlight: 0.36,      // internal backlight wash inside the chassis and behind the band
                        // (CSS --tuner-backlight; 0 = off; owner ask on PR #782)
  readoutHue: 173,      // the readout's colour, a hue (CSS --tuner-readout-hue): 173 = the owner's pick on
                        // PR #782, a cyan-teal (215 = the slider-thumb blue, 120 = phosphor green)
  accentHue: 44,        // #785: the hazard-yellow accent's hue and lightness (CSS --accent-yellow-hue /
  accentLight: 57,      // --accent-yellow-light): the needle, rail tab borders, active chip, Grid / List.
                        // 44 / 57 = #E8B93A. The panel's ranges keep dark text on it and it on the
                        // dark cards above WCAG AA (worst corner: 5.3:1 and 4.8:1).
  // #775 pass 2: start-to-end span bars and the per-year density glow. First guesses, for the owner
  // to tune with ?tune=1; the CSS ones mirror the --dial-* variables on .radio-dial (style.css).
  spanMinPx: 3,         // a span narrower than this at the current zoom stays just its tick
  spanLanes: 3,         // rows of bars stacked above the card ticks before bars share a row
  spanHeight: 3,        // a bar's height, px (CSS --dial-span-h)
  spanGap: 1,           // space between rows and above the ticks, px (CSS --dial-span-gap)
  spanOpacity: 0.55,    // a bar's opacity, so overlaps read brighter (CSS --dial-span-opacity)
  densityStrength: 0.35, // the busiest year's glow, 0 = off (CSS --dial-density-strength)
  densityCurve: 0.6,    // falloff: glow = (count / busiest) ^ curve; < 1 lifts quiet years
  densitySpread: 70,    // how far the glow spreads across its year, % (CSS --dial-density-spread)
  densitySpans: 1,      // 1 = a card lights every year its span touches; 0 = only its start year
};

// #775 pass 2: the feel values that are CSS variables on .radio-dial (key -> [variable, unit]).
const DIAL_CSS_VARS = {
  spanHeight: ['--dial-span-h', 'px'],
  spanGap: ['--dial-span-gap', 'px'],
  spanOpacity: ['--dial-span-opacity', ''],
  densityStrength: ['--dial-density-strength', ''],
  densitySpread: ['--dial-density-spread', '%'],
};

// Month-tick slider notches: fine at the low end, where the owner tunes (PR #782), coarser above.
// A slider with `values` moves through this list by index; the value shown and stored is exact.
const MONTH_TICK_NOTCHES = [
  ...Array.from({ length: 15 }, (_, i) => 0.25 * (i + 1)),   // 0.25 .. 3.75 in 0.25 steps
  ...Array.from({ length: 8 }, (_, i) => 4 + 0.5 * i),       // 4 .. 7.5 in 0.5 steps
  ...Array.from({ length: 33 }, (_, i) => 8 + i),            // 8 .. 40 in 1 px steps
];

// The sliders the dev tuning panel offers, in order.
const DIAL_TUNABLES = [
  { key: 'visibleYears', label: 'Visible span (years)', min: 2, max: 40, step: 1 },
  { key: 'minPxPerYear', label: 'Min px per year', min: 30, max: 160, step: 1 },
  { key: 'monthTickMinPx', label: 'Month ticks from (px per month)', min: 0.25, max: 40, values: MONTH_TICK_NOTCHES },
  { key: 'tolerancePx', label: 'Readout tolerance (px)', min: 1, max: 60, step: 1 },
  { key: 'readoutMax', label: 'Names before "+N more"', min: 1, max: 12, step: 1 },
  { key: 'friction', label: 'Inertia (0 = none)', min: 0, max: 0.98, step: 0.01 },
  { key: 'falloff', label: 'Falloff (braking)', min: 0, max: 0.01, step: 0.0005 },
  { key: 'snapStrength', label: 'Snap strength (0 = free)', min: 0, max: 1, step: 0.05 },
  { key: 'snapPx', label: 'Snap reach (px)', min: 2, max: 120, step: 1 },
  { key: 'wheelSpeed', label: 'Wheel speed', min: 0.1, max: 4, step: 0.1 },
  { key: 'glow', label: 'Outer glow', min: 0, max: 0.5, step: 0.01 },
  { key: 'backlight', label: 'Backlight', min: 0, max: 0.5, step: 0.01 },
  { key: 'readoutHue', label: 'Readout colour (hue)', min: 0, max: 360, step: 1 },
  { key: 'accentHue', label: 'Accent colour (hue)', min: 30, max: 60, step: 1 },        // #785
  { key: 'accentLight', label: 'Accent tint (lightness %)', min: 45, max: 70, step: 1 },  // #785
  // #775 pass 2
  { key: 'spanMinPx', label: 'Span bar from (px wide)', min: 0, max: 40, step: 1 },
  { key: 'spanLanes', label: 'Span bar rows', min: 1, max: 6, step: 1 },
  { key: 'spanHeight', label: 'Span bar height (px)', min: 1, max: 8, step: 0.5 },
  { key: 'spanGap', label: 'Span bar gap (px)', min: 0, max: 4, step: 0.5 },
  { key: 'spanOpacity', label: 'Span bar opacity', min: 0.1, max: 1, step: 0.05 },
  { key: 'densityStrength', label: 'Density glow strength (0 = off)', min: 0, max: 1, step: 0.01 },
  { key: 'densityCurve', label: 'Density falloff (curve)', min: 0.2, max: 3, step: 0.05 },
  { key: 'densitySpread', label: 'Density glow spread (%)', min: 20, max: 100, step: 1 },
  { key: 'densitySpans', label: 'Density counts spans (1) or starts (0)', min: 0, max: 1, step: 1 },
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
    this.rng = M.range(this.cards, { coverEnds: true });  // pass 2: the band reaches the latest end
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
    this.densityLayer = el('div', 'radio-dial-layer-density', this.band, hidden);  // pass 2: year glow
    this.spansLayer = el('div', 'radio-dial-layer-spans', this.band, hidden);      // pass 2: span bars
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

  // Pass 2 (#775): a bar from each card's start to its end (radio-dial-math spanBars), in rows above
  // the ticks. Static, like every mark: the row is a CSS variable, the look is --dial-span-*.
  _renderSpans() {
    const bars = this.M.spanBars(this.cards, this.rng, this.pps,
      { minPx: this.tuning.spanMinPx, lanes: this.tuning.spanLanes });
    this.spansLayer.innerHTML = bars.map((b) =>
      `<span class="radio-dial-span${b.isChild ? ' radio-dial-span--child' : ''}" style="left:${b.x.toFixed(1)}px;width:${b.w.toFixed(1)}px;--lane:${b.lane}"></span>`).join('');
  }

  // Pass 2 (#775): one glow per year, brighter the more cards it holds (yearDensity + densityLevels).
  // The level is a CSS variable; --dial-density-strength scales it, so the strength slider is live.
  _renderDensity() {
    const M = this.M;
    const years = M.densityLevels(M.yearDensity(this.cards, this.rng, { spans: !!this.tuning.densitySpans }),
      this.tuning.densityCurve);
    const x = (e) => M.bandX(e, this.rng, this.pps);
    this.densityLayer.innerHTML = years.filter((y) => y.level > 0).map((y) =>
      `<span class="radio-dial-density" style="left:${x(y.start).toFixed(1)}px;width:${(x(y.end) - x(y.start)).toFixed(1)}px;--lvl:${y.level.toFixed(3)}" title="${y.year}: ${y.count}"></span>`).join('');
  }

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

  // Inertia after a flick: velocity in px/ms of band movement, decayed by `friction` per ~16 ms frame,
  // then braked by a constant `falloff` (px/ms²). The friction alone leaves a long slow tail that ends
  // abruptly at a threshold; the falloff runs the speed down to exactly 0, so the coast eases out.
  // It never reverses: a frame whose braking would cross zero ends the coast there.
  _coast(velocity) {
    this._stopAnim();
    const friction = this.tuning.friction, falloff = Math.max(0, this.tuning.falloff || 0);
    if (!friction || Math.abs(velocity) < DIAL_MIN_FLICK) { this._settle(); return; }
    let v = velocity, last = performance.now();
    const step = (now) => {
      const dt = Math.min(48, now - last); last = now;
      const before = this.needle;
      this._setNeedle(this.M.needleAfterDrag(this.needle, v * dt, this.pps, this.rng));
      v *= Math.pow(friction, dt / 16);
      let stopped;
      if (falloff > 0) {
        const brake = falloff * dt;
        stopped = Math.abs(v) <= brake;  // speed reaches 0 this frame: stop, no sign flip
        if (!stopped) v -= Math.sign(v) * brake;
      } else {
        stopped = Math.abs(v) < DIAL_MIN_FLICK / 2;  // friction only: the old threshold
      }
      this.coastSpeed = stopped ? 0 : v;  // read by the UI test (#782 falloff)
      if (stopped || this.needle === before) { this._anim = null; this._settle(); return; }
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
    // Colours are the shared tuner theme (style.css :root, also read by the card page's timeline):
    // set on <html> so its derived colours recompute and everything themed on this page follows.
    const theme = document.documentElement.style;
    theme.setProperty('--tuner-glow', String(this.tuning.glow));
    theme.setProperty('--tuner-backlight', String(this.tuning.backlight));
    theme.setProperty('--tuner-readout-hue', String(this.tuning.readoutHue));
    // #785: the accent (needle and UI highlights share one hazard yellow, style.css :root).
    theme.setProperty('--accent-yellow-hue', String(this.tuning.accentHue));
    theme.setProperty('--accent-yellow-light', `${this.tuning.accentLight}%`);
    // #775 pass 2: the bar and glow looks are the dial's own --dial-* variables (on .radio-dial).
    Object.entries(DIAL_CSS_VARS).forEach(([k, [name, unit]]) => {
      this.root.style.setProperty(name, `${this.tuning[k]}${unit}`);
    });
    this._layout();
  }
}

RadioDial.DEFAULTS = DIAL_DEFAULTS;
RadioDial.TUNABLES = DIAL_TUNABLES;
RadioDial.CSS_VARS = DIAL_CSS_VARS;
window.RadioDial = RadioDial;
