// Date maths for dragging a marker on the project timeline (#593). Pure: no DOM, no fetch, so
// scripts/test_timeline_drag.js can run it under node. The browser gets it as
// window.TimelineDragMath; node gets it from require().
//
// All dates are unix SECONDS (what the server stores). "Day" arithmetic is on the MOUNTAIN TIME calendar
// (mountain-time.js, #544: the same zone every date is shown in, whatever the browser's zone is), so a
// step across a daylight-saving change keeps the same wall-clock time instead of drifting an hour.
//
// What the owner gets:
//   * a plain drag lands on a DAY and keeps the item's own time of day (snapDay);
//   * a drag with Shift held snaps to 15 minutes instead (snapFine);
//   * the arrow keys step a day, Shift+arrow (or PageUp / PageDown) a month (keyStep).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./mountain-time.js'));
  else root.TimelineDragMath = factory(root.MountainTime);
})(typeof self !== 'undefined' ? self : this, function (MT) {
  const FINE_MINUTES = 15;
  const MAX_EPOCH = 4102444800; // 2100-01-01, the server's upper bound
  const MIN_EPOCH = 0;          // 1970-01-01, the server's lower bound

  // x (pixels from the track's left edge) -> unix seconds, on an axis that runs min..max across `width` px.
  function pxToEpoch(px, width, min, max) {
    if (!(width > 0)) return min;
    const f = Math.min(1, Math.max(0, px / width));
    return min + f * (max - min);
  }

  // unix seconds -> percent along the axis (unclamped: a keyboard step can leave the visible range).
  function epochToPercent(epoch, min, max) {
    if (max === min) return 50;
    return ((epoch - min) / (max - min)) * 100;
  }

  function clamp(epoch, lo, hi) {
    return Math.min(hi, Math.max(lo, epoch));
  }

  // Land on the Mountain calendar day that `epoch` falls on, keeping `orig`'s Mountain time of day.
  function snapDay(epoch, orig) {
    const d = MT.parts(epoch);
    const o = MT.parts(orig);
    return MT.toEpoch(d.year, d.month, d.day, o.hour, o.minute, o.second);
  }

  // Round to the nearest FINE_MINUTES.
  function snapFine(epoch) {
    const step = FINE_MINUTES * 60;
    return Math.round(epoch / step) * step;
  }

  function snap(epoch, orig, fine) {
    return fine ? snapFine(epoch) : snapDay(epoch, orig);
  }

  // Add whole Mountain months, clamping the day of month (Jan 31 + 1 month = Feb 28/29, not Mar 3).
  function addMonths(epoch, n) {
    const p = MT.parts(epoch);
    const index = p.year * 12 + (p.month - 1) + n;
    const year = Math.floor(index / 12);
    const month = index - year * 12 + 1;
    const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
    return MT.toEpoch(year, month, Math.min(p.day, last), p.hour, p.minute, p.second);
  }

  function addDays(epoch, n) {
    const p = MT.parts(epoch);
    return MT.toEpoch(p.year, p.month, p.day + n, p.hour, p.minute, p.second);
  }

  // Can a plain (day-step) drag change ANY date on an axis running min..max? A plain drag lands on the
  // Mountain calendar day under the pointer (snapDay), so it only has somewhere to go when the axis
  // touches at least two Mountain days. When the whole axis sits inside one day (#631: a project whose
  // items all fall in the same minute) every drop snaps back to where the item started.
  function dragCanMove(min, max) {
    const a = MT.parts(min);
    const b = MT.parts(max);
    return a.year !== b.year || a.month !== b.month || a.day !== b.day;
  }

  // The date a key press moves a marker to, or null when the key isn't a move.
  // Left/Down = earlier, Right/Up = later. Arrow = a day, Shift+arrow = a month; PageUp/PageDown = a month.
  function keyStep(epoch, key, shift) {
    let dir = 0;
    let months = !!shift;
    if (key === 'ArrowLeft' || key === 'ArrowDown') dir = -1;
    else if (key === 'ArrowRight' || key === 'ArrowUp') dir = 1;
    else if (key === 'PageDown') { dir = -1; months = true; }
    else if (key === 'PageUp') { dir = 1; months = true; }
    else return null;
    const out = months ? addMonths(epoch, dir) : addDays(epoch, dir);
    return clamp(out, MIN_EPOCH, MAX_EPOCH);
  }

  // Group markers that sit within `thresholdPx` of their neighbour on a `width`-px axis (chained, like
  // the flag clustering). items: [{id, date}]. Returns arrays of ids, left to right; a single marker
  // is a group of one. The stack chooser opens for groups of two or more.
  function stackGroups(items, min, max, width, thresholdPx) {
    const sorted = items.slice().sort((a, b) => a.date - b.date);
    const groups = [];
    let cur = null;
    let lastPx = null;
    sorted.forEach((it) => {
      const px = (max === min) ? 0 : ((it.date - min) / (max - min)) * width;
      if (cur && px - lastPx <= thresholdPx) cur.push(it.id);
      else { cur = [it.id]; groups.push(cur); }
      lastPx = px;
    });
    return groups;
  }

  // "Tue, Mar 4, 2025, 6:39 PM" (day drag) -- the live readout. `locale` is for the tests.
  function formatReadout(epoch, locale) {
    return MT.formatLong(epoch, locale);
  }

  return { FINE_MINUTES, MAX_EPOCH, MIN_EPOCH, pxToEpoch, epochToPercent, clamp, snapDay, snapFine, snap,
    addMonths, addDays, dragCanMove, keyStep, stackGroups, formatReadout };
});
