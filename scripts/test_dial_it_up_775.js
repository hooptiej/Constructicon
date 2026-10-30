// Unit test for #775 pass 2 ("Dial It Up", batch 14): the span-bar and per-year density maths in
// web/static/js/radio-dial-math.js (spanBars, yearDensity, densityLevels, range coverEnds).
//   node scripts/test_dial_it_up_775.js
// Pure maths, no DOM. The widget side (bars and glow drawn into the band) is in
// scripts/test_radio_dial_ui_775.js.
const path = require('path');
const MT = require(path.join(__dirname, '..', 'web', 'static', 'js', 'mountain-time.js'));
const M = require(path.join(__dirname, '..', 'web', 'static', 'js', 'radio-dial-math.js'));

let failed = 0;
const eq = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : `: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
  if (!ok) failed++;
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;
const day = (y, m, d) => MT.toEpoch(y, m, d, 12, 0, 0);
const YEAR = M.SECONDS_PER_YEAR;

const rows = [
  { slug: 'long', title: 'Long build', effective_start: day(2000, 1, 1), effective_end: day(2003, 7, 1) },
  { slug: 'short', title: 'Short one', effective_start: day(2001, 3, 1), effective_end: day(2001, 3, 5) },
  { slug: 'point', title: 'Start only', effective_start: day(2001, 6, 1), effective_end: null },
  { slug: 'same', title: 'Single moment', effective_start: day(2002, 2, 1), effective_end: day(2002, 2, 1) },
  { slug: 'mid', title: 'Overlapping', effective_start: day(2001, 1, 1), effective_end: day(2002, 1, 1), is_child: true },
  { slug: 'late', title: 'Runs past the last start', effective_start: day(2004, 1, 1), effective_end: day(2008, 6, 1) },
];
const cards = M.parseCards(rows).cards;

// --- range: coverEnds reaches the year after the latest END; needle bounds stay the starts ---
const plain = M.range(cards);
const rng = M.range(cards, { coverEnds: true });
eq('range without coverEnds still ends after the last START (pass 1 behaviour)', plain.end, MT.toEpoch(2005, 1, 1));
eq('range with coverEnds ends after the latest END (2008 -> Jan 1 2009)', [rng.end, rng.lastYear], [MT.toEpoch(2009, 1, 1), 2008]);
eq('coverEnds keeps the needle bounds on the starts', [rng.first, rng.last], [plain.first, plain.last]);
eq('coverEnds with no ends is the same range', M.range(M.parseCards([rows[2]]).cards, { coverEnds: true }).end, MT.toEpoch(2002, 1, 1));

// --- spanBars: start-to-end pixels; start-only and too-short stay ticks ---
const pps = M.pxPerSecond(1000, 10, 64);  // 100 px per year
const bars = M.spanBars(cards, rng, pps, { minPx: 3, lanes: 3 });
const bySlug = Object.fromEntries(bars.map((b) => [b.slug, b]));
eq('bars only for cards with a real end after the start, at least minPx wide', bars.map((b) => b.slug).sort(), ['late', 'long', 'mid']);
eq('a start-only card has no bar', 'point' in bySlug, false);
eq('a start == end card has no bar', 'same' in bySlug, false);
eq('a 4-day span (~1 px) under minPx stays a tick', 'short' in bySlug, false);
eq('with minPx 0 the short span gets its bar', M.spanBars(cards, rng, pps, { minPx: 0, lanes: 3 }).some((b) => b.slug === 'short'), true);
eq('bar x = bandX(start)', near(bySlug.long.x, M.bandX(day(2000, 1, 1), rng, pps)), true);
eq('bar width = (end - start) * pps (3.5 years = ~350 px)', near(bySlug.long.w, (day(2003, 7, 1) - day(2000, 1, 1)) * pps), true);
eq('the long bar is about 350 px', Math.round(bySlug.long.w / 10), 35);
eq('the child flag rides along', bySlug.mid.isChild, true);

// lanes: overlapping bars take the next row; a bar after the first one ended reuses row 0
eq('first bar in row 0, the overlapping one in row 1', [bySlug.long.lane, bySlug.mid.lane], [0, 1]);
eq('a bar that starts after row 0 is free goes back to row 0', bySlug.late.lane, 0);
const stack = M.parseCards([0, 1, 2, 3].map((i) => ({ slug: 's' + i, effective_start: day(2010, 1, 1 + i), effective_end: day(2012, 1, 1) }))).cards;
const sRng = M.range(stack, { coverEnds: true });
const sBars = M.spanBars(stack, sRng, pps, { minPx: 3, lanes: 2 });
eq('more overlaps than rows: the extra bars share rows, never a new one', sBars.map((b) => b.lane), [0, 1, 0, 1]);
eq('one row = every bar in row 0', M.spanBars(stack, sRng, pps, { minPx: 3, lanes: 1 }).map((b) => b.lane), [0, 0, 0, 0]);
eq('a bar is clipped to the band end', M.spanBars(M.parseCards([{ slug: 'x', effective_start: day(2001, 1, 1), effective_end: day(2030, 1, 1) }]).cards,
  plain, pps, { minPx: 0, lanes: 1 })[0].w <= M.bandX(plain.end, plain, pps) - M.bandX(day(2001, 1, 1), plain, pps) + 1e-9, true);
eq('bar positions stay finite at any zoom', M.spanBars(cards, rng, M.pxPerSecond(1000, 40, 30), { minPx: 0, lanes: 3 })
  .every((b) => isFinite(b.x) && isFinite(b.w) && b.w > 0), true);
eq('no cards, no bars', M.spanBars([], rng, pps, { minPx: 0, lanes: 3 }), []);

// --- yearDensity: per Mountain year, gaps included ---
const startsOnly = M.yearDensity(cards, rng, { spans: false });
eq('one entry per band year 2000..2008', startsOnly.map((d) => d.year), [2000, 2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008]);
eq('starts only: counts per start year', startsOnly.map((d) => d.count), [1, 3, 1, 0, 1, 0, 0, 0, 0]);
const withSpans = M.yearDensity(cards, rng, { spans: true });
eq('spans: a card counts in every year its span touches', withSpans.map((d) => d.count), [1, 4, 3, 1, 1, 1, 1, 1, 1]);
eq('a year runs Mountain Jan 1 to Jan 1', [withSpans[0].start, withSpans[0].end], [MT.toEpoch(2000, 1, 1), MT.toEpoch(2001, 1, 1)]);
// A card on Jan 1 03:00 UTC is Dec 31 in Mountain: it counts in the earlier year.
const ny = M.parseCards([{ slug: 'ny', effective_start: Date.UTC(2026, 0, 1, 3) / 1000 }]).cards;
eq('density uses Mountain years', M.yearDensity(ny, M.range(ny), {}).map((d) => [d.year, d.count]), [[2025, 1]]);

// --- densityLevels: (count / busiest) ^ curve ---
const lv = M.densityLevels(withSpans, 1);
eq('busiest year is 1, empty-free years scale linearly', [lv[1].level, lv[2].level, lv[0].level], [1, 0.75, 0.25]);
const soft = M.densityLevels(withSpans, 0.5);
eq('curve < 1 lifts the quiet years', near(soft[0].level, Math.sqrt(0.25)), true);
const hard = M.densityLevels(withSpans, 2);
eq('curve > 1 keeps only the busy ones bright', near(hard[0].level, 0.0625), true);
eq('an empty year is always 0', M.densityLevels(startsOnly, 0.6)[3].level, 0);
eq('nothing at all = every level 0, no NaN', M.densityLevels([{ year: 1, count: 0 }], 0.6)[0].level, 0);
eq('a bad curve falls back to linear', M.densityLevels(withSpans, 0)[2].level, 0.75);
eq('levels keep the year fields', Object.keys(lv[0]).sort(), ['count', 'end', 'level', 'start', 'year']);

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exitCode = failed ? 1 : 0;
