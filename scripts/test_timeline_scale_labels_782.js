// The card page timeline's ruler labels never pile up (PR #782): web/static/js/project-video-timeline.js.
//   node scripts/test_timeline_scale_labels_782.js
// Owner saw the scale labels run together into one line of text (thought to be Tension Biped, a ~61-day
// card). For a spread of spans around every unit switch (14 / 60 / 730 days) and panel widths from a
// phone to a wide desktop, it checks that labelled ticks are at least one estimated label width apart,
// that every tick keeps its mark, and that a span with room to spare still labels every tick. It also
// prints how many overlapping label pairs the old code (a label on every tick) would have drawn.
process.env.TZ = process.env.TZ || 'America/Denver';
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = path.join(__dirname, '..');
const MT = require(path.join(ROOT, 'web/static/js/mountain-time.js'));
const ctx = vm.createContext({ window: { MountainTime: MT }, console, Math, Date, Set });
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/static/js/project-video-timeline.js'), 'utf8')
  + '\nglobalThis.PVT = ProjectVideoTimeline; globalThis.CHAR = SCALE_LABEL_CHAR_PX; globalThis.GAP = SCALE_LABEL_GAP_PX;', ctx);
const { PVT, CHAR, GAP } = ctx;

let failed = 0;
const ok = (n, c, x) => { console.log((c ? 'ok   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x))); if (!c) failed++; };

const START = MT.toEpoch(2015, 6, 30, 9, 0);  // Tension Biped's first dated item
const DAY = 86400;
const SPANS = [7, 13, 14, 15, 30, 45, 59, 60, 61, 62, 75, 90, 180, 365, 700, 729, 731, 1100, 3650, 9000];
const WIDTHS = [320, 360, 600, 900, 1200, 1600];

const overlaps = (ticks, idx, range, width) => {
  const span = range.max - range.min;
  const xs = [...idx].sort((a, b) => a - b).map((i) => ({ x: ((ticks[i].date - range.min) / span) * width, w: ticks[i].label.length * CHAR }));
  let n = 0;
  for (let i = 1; i < xs.length; i++) if (xs[i].x - xs[i - 1].x < (xs[i].w + xs[i - 1].w) / 2 + GAP - 0.5) n++;
  return n;
};

const table = [];
SPANS.forEach((days) => {
  const range = { min: START, max: START + days * DAY };
  const ticks = PVT.prototype._scaleTicks.call({}, range);
  WIDTHS.forEach((w) => {
    const labelled = PVT.labelledTicks(ticks, range, w);
    const before = overlaps(ticks, new Set(ticks.map((_, i) => i)), range, w);
    const after = overlaps(ticks, labelled, range, w);
    table.push(`${String(days).padStart(5)} d ${String(w).padStart(5)} px  ${ticks.kind.padEnd(5)} ticks ${String(ticks.length).padStart(3)}  labels ${String(labelled.size).padStart(3)}  overlaps before ${String(before).padStart(3)} after ${after}`);
    if (after) ok(`${days} days at ${w} px: no labels touch`, false, { after, kind: ticks.kind });
    if (labelled.size < 1) ok(`${days} days at ${w} px: at least one label`, false);
  });
});
console.log('info: span / width / unit / ticks / labels drawn / overlapping label pairs (old -> new)');
table.forEach((r) => console.log('info: ' + r));
ok('no span or width draws touching labels', !table.some((r) => !/after 0$/.test(r)));
ok('every case keeps at least one label', table.every((r) => !/labels\s+0 /.test(r)));

// The old code could overlap: prove the repro (months on a two-year span at a normal panel width).
const two = { min: START, max: START + 729 * DAY };
const twoTicks = PVT.prototype._scaleTicks.call({}, two);
ok('repro: 729 days at 900 px used to overlap (a label per month)', overlaps(twoTicks, new Set(twoTicks.map((_, i) => i)), two, 900) > 0);
const twoLab = PVT.labelledTicks(twoTicks, two, 900);
ok('fix: it now labels a calendar-friendly subset that includes January', twoLab.size < twoTicks.length
  && [...twoLab].some((i) => twoTicks[i].label.startsWith('Jan')), [...twoLab].map((i) => twoTicks[i].label));
const stride = [...twoLab].sort((a, b) => a - b);
ok('... at a regular stride', stride.every((v, i) => i < 2 || v - stride[i - 1] === stride[1] - stride[0]));

// Room to spare: every tick labelled, as before.
const tb = { min: START, max: START + 61 * DAY };
const tbTicks = PVT.prototype._scaleTicks.call({}, tb);
ok('Tension Biped (61 days) at 900 px: every tick still labelled', PVT.labelledTicks(tbTicks, tb, 900).size === tbTicks.length, tbTicks.map((t) => t.label));
ok('not laid out yet (width 0): label everything, as before', PVT.labelledTicks(twoTicks, two, 0).size === twoTicks.length);

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exitCode = failed ? 1 : 0;
