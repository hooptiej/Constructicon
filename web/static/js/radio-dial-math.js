// The home page radio dial's maths (#775 "Tune in to the past"), kept pure (no DOM) so
// scripts/test_radio_dial_775.js can run it under node. The browser gets it as window.RadioDialMath;
// node gets it from require(). Needs MountainTime (mountain-time.js) for year/month boundaries: every
// stored date is UTC unix SECONDS and every boundary drawn is a Mountain one (#544).
//
// Coordinates: the BAND is one strip covering the whole range, x = 0 at range.start. The needle is
// fixed at the window's centre; sliding the dial moves the band under it, so the band's offset is
// (windowWidth / 2) - x(needle).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./mountain-time.js'));
  else root.RadioDialMath = factory(root.MountainTime);
})(typeof self !== 'undefined' ? self : this, function (MT) {
  const SECONDS_PER_YEAR = 365.2425 * 86400;

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // Raw timeline_projects rows -> {cards, skipped}. A row needs a finite numeric effective_start and a
  // slug to be on the dial; anything else is counted in `skipped` (the dial says so, never drops it
  // silently). Sorted by start so nearest-card lookups and ticks are stable.
  function parseCards(raw) {
    if (!Array.isArray(raw)) return { cards: [], skipped: 0, malformed: true };
    const cards = [];
    let skipped = 0;
    raw.forEach((p) => {
      const start = p && Number(p.effective_start);
      if (!p || p.effective_start === null || p.effective_start === undefined || p.effective_start === ''
          || !isFinite(start) || !p.slug) { skipped++; return; }
      const end = Number(p.effective_end);
      cards.push({
        id: p.id, slug: String(p.slug), title: String(p.title || p.slug), start,
        end: isFinite(end) && p.effective_end !== null ? end : null,  // pass 2 (span bars) reads this
        isChild: !!p.is_child, coverUrl: p.cover_url || null,
      });
    });
    cards.sort((a, b) => a.start - b.start || a.title.localeCompare(b.title));
    return { cards, skipped, malformed: false };
  }

  // The dial's range: from Mountain Jan 1 of the earliest card's year to Mountain Jan 1 of the year
  // after the latest card, so the first and last year labels sit on the band. The needle itself is
  // clamped to the cards' own first..last start (needleBounds), "earliest to latest card date".
  function range(cards) {
    if (!cards.length) return null;
    const first = cards[0].start, last = cards[cards.length - 1].start;
    const y0 = MT.parts(first).year, y1 = MT.parts(last).year;
    return { start: MT.toEpoch(y0, 1, 1), end: MT.toEpoch(y1 + 1, 1, 1), first, last, firstYear: y0, lastYear: y1 };
  }

  // Pixels per second for a window `width` px wide that shows `visibleYears` years, never so tight
  // that one year is narrower than `minPxPerYear` (a year label must always fit: on a narrow phone
  // the dial shows fewer than a decade rather than squeezing the labels).
  function pxPerSecond(width, visibleYears, minPxPerYear) {
    const years = Math.max(0.5, Math.min(visibleYears, width / Math.max(1, minPxPerYear || 1)));
    return width / (years * SECONDS_PER_YEAR);
  }

  // Epoch -> x on the band.
  function bandX(epoch, rng, pps) { return (epoch - rng.start) * pps; }

  // The band's translateX that puts `needle` under the window's centre.
  function bandOffset(needle, rng, pps, width) { return width / 2 - bandX(needle, rng, pps); }

  // Dragging the band right by dx px moves the needle back in time by dx / pps seconds.
  function needleAfterDrag(needle, dxPx, pps, rng) {
    return clamp(needle - dxPx / pps, rng.first, rng.last);
  }

  // A year tick per year (Mountain Jan 1), from range.start up to and including range.end.
  function yearTicks(rng) {
    const out = [];
    for (let y = rng.firstYear; y <= rng.lastYear + 1; y++) out.push({ year: y, epoch: MT.toEpoch(y, 1, 1) });
    return out;
  }

  // Month ticks (not Januaries: those are year ticks), only when a month is at least minPx wide;
  // otherwise [] (no minor ticks at that density).
  function monthTicks(rng, pps, minPx) {
    if (SECONDS_PER_YEAR / 12 * pps < minPx) return [];
    const out = [];
    for (let y = rng.firstYear; y <= rng.lastYear; y++) {
      for (let m = 2; m <= 12; m++) out.push({ year: y, month: m, epoch: MT.toEpoch(y, m, 1) });
    }
    return out;
  }

  // The cards the readout names: every card whose start is within `tolerancePx` of the needle (in
  // pixels at the current zoom), nearest first. [] when nothing is that close: the readout then says
  // what date the needle is on and that no card is there.
  function cardsAtNeedle(cards, needle, pps, tolerancePx) {
    const tol = tolerancePx / pps;
    return cards
      .map((c) => ({ card: c, dist: Math.abs(c.start - needle) }))
      .filter((d) => d.dist <= tol)
      .sort((a, b) => a.dist - b.dist || a.card.start - b.card.start)
      .map((d) => d.card);
  }

  // The nearest card to the needle (any distance), or null: the snap target.
  function nearestCard(cards, needle) {
    let best = null, bestDist = Infinity;
    cards.forEach((c) => { const d = Math.abs(c.start - needle); if (d < bestDist) { best = c; bestDist = d; } });
    return best;
  }

  // Readout list: the first `max` names plus how many more.
  function capList(list, max) {
    return { shown: list.slice(0, max), more: Math.max(0, list.length - max) };
  }

  // The needle's own "frequency": Mountain month and year, e.g. "Mar 2004".
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function needleLabel(epoch) {
    const p = MT.parts(epoch);
    return `${MONTHS[p.month - 1]} ${p.year}`;
  }

  // Keyboard steps: a month, a year with Shift / PageUp / PageDown; Home and End jump to the ends.
  // Returns the new needle, or null for a key the dial ignores.
  function keyStep(needle, key, shift, rng) {
    const month = SECONDS_PER_YEAR / 12;
    let next;
    if (key === 'ArrowLeft') next = needle - (shift ? SECONDS_PER_YEAR : month);
    else if (key === 'ArrowRight') next = needle + (shift ? SECONDS_PER_YEAR : month);
    else if (key === 'PageUp') next = needle - SECONDS_PER_YEAR;
    else if (key === 'PageDown') next = needle + SECONDS_PER_YEAR;
    else if (key === 'Home') next = rng.first;
    else if (key === 'End') next = rng.last;
    else return null;
    return clamp(next, rng.first, rng.last);
  }

  return {
    SECONDS_PER_YEAR, clamp, parseCards, range, pxPerSecond, bandX, bandOffset, needleAfterDrag,
    yearTicks, monthTicks, cardsAtNeedle, nearestCard, capList, needleLabel, keyStep,
  };
});
