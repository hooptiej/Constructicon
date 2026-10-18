// Unit test for web/static/js/radio-dial-math.js, the home page radio dial's maths (#775 pass 1).
//   node scripts/test_radio_dial_775.js
// Dates to band position, needle to nearest card(s), year/month ticks, keys, and the data checks
// that make a malformed timeline say so instead of drawing a blank bar.
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

// --- parseCards: valid rows kept and sorted, bad ones counted, never silently dropped ---
const raw = [
  { id: 3, slug: 'c', title: 'Gamma', effective_start: day(2010, 6, 1), effective_end: day(2011, 1, 1), is_child: true },
  { id: 1, slug: 'a', title: 'Alpha', effective_start: day(1998, 3, 4), effective_end: null, is_child: false },
  { id: 2, slug: 'b', title: 'Beta', effective_start: day(2010, 6, 1), effective_end: null },
  { id: 9, slug: 'bad1', title: 'No date', effective_start: null },
  { id: 8, slug: 'bad2', title: 'Garbage date', effective_start: 'soon' },
  { id: 7, title: 'No slug', effective_start: day(2001, 1, 1) },
  null,
];
const parsed = M.parseCards(raw);
eq('parseCards keeps the three good rows, start order (ties by title)', parsed.cards.map((c) => c.slug), ['a', 'b', 'c']);
eq('parseCards counts the four bad rows', parsed.skipped, 4);
eq('parseCards: not a list is malformed', M.parseCards({ nope: 1 }).malformed, true);
eq('parseCards: empty list is not malformed, just empty', [M.parseCards([]).malformed, M.parseCards([]).cards.length], [false, 0]);
eq('parseCards keeps end for pass 2 and the child flag', [parsed.cards[2].end, parsed.cards[2].isChild], [day(2011, 1, 1), true]);

// --- range: Mountain Jan 1 of the first year to Jan 1 after the last; needle bounds = the cards ---
const cards = parsed.cards;
const rng = M.range(cards);
eq('range starts at Mountain Jan 1 1998', rng.start, MT.toEpoch(1998, 1, 1));
eq('range ends at Mountain Jan 1 2011 (the year after the last card)', rng.end, MT.toEpoch(2011, 1, 1));
eq('needle bounds are the first and last card', [rng.first, rng.last], [day(1998, 3, 4), day(2010, 6, 1)]);
eq('range of nothing is null', M.range([]), null);
// A card on Jan 1 03:00 UTC is still Dec 31 in Mountain: its year is the earlier one.
const nyCards = M.parseCards([{ slug: 'ny', effective_start: Date.UTC(2026, 0, 1, 3) / 1000 }]).cards;
eq('range uses Mountain years (Jan 1 03:00Z is 2025)', M.range(nyCards).firstYear, 2025);

// --- pxPerSecond: about a decade across the window, never squeezing a year below minPxPerYear ---
const pps = M.pxPerSecond(1000, 10, 64);
eq('1000 px showing 10 years = 100 px per year', near(pps * YEAR, 100), true);
const narrow = M.pxPerSecond(360, 10, 64);
eq('a 360 px phone shows fewer years so each year is still 64 px', near(narrow * YEAR, 64), true);

// --- positions: date -> band x; the needle date sits at the window centre ---
eq('bandX of range start is 0', M.bandX(rng.start, rng, pps), 0);
eq('bandX one year in is one year of px', near(M.bandX(rng.start + YEAR, rng, pps), 100), true);
const needle = day(2004, 7, 1);
const off = M.bandOffset(needle, rng, pps, 1000);
eq('bandOffset puts the needle date under the centre (x = 500)', near(off + M.bandX(needle, rng, pps), 500), true);

// --- dragging: band right = back in time; clamped to the cards' range ---
const back = M.needleAfterDrag(needle, 100, pps, rng);
eq('drag right 100 px = one year earlier', near(needle - back, YEAR, 1), true);
eq('drag left = later', M.needleAfterDrag(needle, -100, pps, rng) > needle, true);
eq('drag far left clamps to the last card', M.needleAfterDrag(needle, -1e6, pps, rng), rng.last);
eq('drag far right clamps to the first card', M.needleAfterDrag(needle, 1e6, pps, rng), rng.first);

// --- ticks ---
const years = M.yearTicks(rng);
eq('a year tick for every year 1998..2011, gaps included', years.map((y) => y.year), Array.from({ length: 14 }, (_, i) => 1998 + i));
eq('year ticks sit on Mountain Jan 1', years[0].epoch, MT.toEpoch(1998, 1, 1));
eq('no month ticks when a month is too narrow (100 px/year = 8.3 px/month < 9)', M.monthTicks(rng, pps, 9).length, 0);
const months = M.monthTicks(rng, M.pxPerSecond(1000, 2, 64), 9);
eq('month ticks when a month is wide enough: 11 per year (Jan is the year tick)', months.length, 13 * 11);
eq('month ticks start at Feb 1', [months[0].month, months[0].epoch], [2, MT.toEpoch(1998, 2, 1)]);

// --- the needle to the card(s) under it ---
const june = day(2010, 6, 1);
eq('cards within tolerance are named, nearest first (two on one day)', M.cardsAtNeedle(cards, june, pps, 10).map((c) => c.slug), ['b', 'c']);
const nineDays = june + 9 * 86400;  // 9 days = ~2.5 px at 100 px/year
eq('a few px off still tunes in', M.cardsAtNeedle(cards, nineDays, pps, 10).length, 2);
const twoMonths = june - 60 * 86400;  // ~16 px away
eq('beyond the tolerance: nothing named', M.cardsAtNeedle(cards, twoMonths, pps, 10), []);
eq('a wider tolerance reaches it', M.cardsAtNeedle(cards, twoMonths, pps, 20).length, 2);
eq('nearestCard picks the closest at any distance', M.nearestCard(cards, day(1999, 1, 1)).slug, 'a');
eq('nearestCard of nothing is null', M.nearestCard([], 0), null);
eq('capList shows the first N and counts the rest', [M.capList([1, 2, 3, 4, 5, 6], 4).shown, M.capList([1, 2, 3, 4, 5, 6], 4).more], [[1, 2, 3, 4], 2]);
eq('capList with room to spare', M.capList([1, 2], 4).more, 0);

// --- readout label and keys ---
eq('needleLabel is the Mountain month and year', M.needleLabel(day(2004, 3, 15)), 'Mar 2004');
eq('needleLabel of Jan 1 03:00Z is Dec of the year before (Mountain)', M.needleLabel(Date.UTC(2026, 0, 1, 3) / 1000), 'Dec 2025');
eq('ArrowRight = a month later', near(M.keyStep(needle, 'ArrowRight', false, rng) - needle, YEAR / 12, 1), true);
eq('Shift+ArrowLeft = a year earlier', near(needle - M.keyStep(needle, 'ArrowLeft', true, rng), YEAR, 1), true);
eq('PageDown = a year later', near(M.keyStep(needle, 'PageDown', false, rng) - needle, YEAR, 1), true);
eq('Home / End jump to the first and last card', [M.keyStep(needle, 'Home', false, rng), M.keyStep(needle, 'End', false, rng)], [rng.first, rng.last]);
eq('keys clamp at the ends', M.keyStep(rng.last, 'ArrowRight', false, rng), rng.last);
eq('other keys are ignored', M.keyStep(needle, 'a', false, rng), null);

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exitCode = failed ? 1 : 0;
