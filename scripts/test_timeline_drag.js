// Unit test for web/static/js/timeline-drag-math.js (#593). No dependencies:
//   TZ=America/Denver node scripts/test_timeline_drag.js
// (the maths is on the local calendar, so the test pins a zone; it sets TZ itself when run bare).
if (!process.env.TZ) {
  const r = require('child_process').spawnSync(process.execPath, [__filename], {
    stdio: 'inherit', env: { ...process.env, TZ: 'America/Denver' } });
  process.exit(r.status === null ? 1 : r.status);
}
const path = require('path');
const M = require(path.join(__dirname, '..', 'web', 'static', 'js', 'timeline-drag-math.js'));

let failed = 0;
function eq(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : `: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
  if (!ok) failed++;
}
const ep = (y, mo, d, h = 0, mi = 0, s = 0) => new Date(y, mo - 1, d, h, mi, s).getTime() / 1000;
const parts = (e) => { const d = new Date(e * 1000); return [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes()]; };

// pixel <-> date
eq('px 0 is min', M.pxToEpoch(0, 1000, 100, 200), 100);
eq('px width is max', M.pxToEpoch(1000, 1000, 100, 200), 200);
eq('px middle', M.pxToEpoch(500, 1000, 100, 200), 150);
eq('px clamps left', M.pxToEpoch(-50, 1000, 100, 200), 100);
eq('px clamps right', M.pxToEpoch(5000, 1000, 100, 200), 200);
eq('px zero width', M.pxToEpoch(10, 0, 100, 200), 100);
eq('percent', M.epochToPercent(150, 100, 200), 50);
eq('percent unclamped past the end', M.epochToPercent(300, 100, 200), 200);
eq('percent of a flat axis', M.epochToPercent(5, 5, 5), 50);

// day snap keeps the item's time of day
const orig = ep(2026, 10, 6, 18, 39, 12);
eq('snapDay moves the day, keeps 6:39 PM', parts(M.snapDay(ep(2025, 3, 4, 3, 0), orig)), [2025, 3, 4, 18, 39]);
eq('snapDay late-evening drop stays on that day', parts(M.snapDay(ep(2025, 3, 4, 23, 59), orig)), [2025, 3, 4, 18, 39]);
eq('snapDay seconds kept', new Date(M.snapDay(ep(2025, 3, 4, 9), orig) * 1000).getSeconds(), 12);
eq('snapDay across DST (Mar 9 2025) keeps wall-clock time', parts(M.snapDay(ep(2025, 3, 9, 12), orig)), [2025, 3, 9, 18, 39]);
// fine snap
eq('snapFine rounds to 15 min', M.snapFine(ep(2025, 3, 4, 10, 7, 29)) % 900, 0);
eq('snapFine nearest', parts(M.snapFine(ep(2025, 3, 4, 10, 8))), [2025, 3, 4, 10, 15]);
eq('snap() fine', M.snap(ep(2025, 3, 4, 10, 8), orig, true), M.snapFine(ep(2025, 3, 4, 10, 8)));
eq('snap() day', M.snap(ep(2025, 3, 4, 10, 8), orig, false), M.snapDay(ep(2025, 3, 4, 10, 8), orig));

// keyboard steps
const k0 = ep(2025, 1, 31, 18, 39);
eq('ArrowRight +1 day', parts(M.keyStep(k0, 'ArrowRight', false)), [2025, 2, 1, 18, 39]);
eq('ArrowLeft -1 day', parts(M.keyStep(k0, 'ArrowLeft', false)), [2025, 1, 30, 18, 39]);
eq('Shift+Right +1 month clamps Jan 31 -> Feb 28', parts(M.keyStep(k0, 'ArrowRight', true)), [2025, 2, 28, 18, 39]);
eq('Shift+Left -1 month', parts(M.keyStep(ep(2025, 3, 15, 8, 0), 'ArrowLeft', true)), [2025, 2, 15, 8, 0]);
eq('leap year clamp 2024-01-31 +1 month', parts(M.keyStep(ep(2024, 1, 31, 8, 0), 'ArrowRight', true)), [2024, 2, 29, 8, 0]);
eq('across year end', parts(M.keyStep(ep(2025, 12, 31, 8, 0), 'ArrowRight', false)), [2026, 1, 1, 8, 0]);
eq('across year end by month', parts(M.keyStep(ep(2025, 12, 15, 8, 0), 'ArrowRight', true)), [2026, 1, 15, 8, 0]);
eq('PageUp is a month', parts(M.keyStep(ep(2025, 3, 15, 8, 0), 'PageUp', false)), [2025, 4, 15, 8, 0]);
eq('Up/Down also step', parts(M.keyStep(ep(2025, 3, 15, 8, 0), 'ArrowDown', false)), [2025, 3, 14, 8, 0]);
eq('DST day step keeps wall-clock time', parts(M.keyStep(ep(2025, 3, 8, 18, 39), 'ArrowRight', false)), [2025, 3, 9, 18, 39]);
eq('other keys are not moves', M.keyStep(k0, 'a', false), null);
eq('clamped at 1970', M.keyStep(0, 'ArrowLeft', false) >= 0, true);
eq('clamped at 2100', M.keyStep(M.MAX_EPOCH, 'ArrowRight', false) <= M.MAX_EPOCH, true);

// stacks
const items = [{ id: 'a', date: 0 }, { id: 'b', date: 5 }, { id: 'c', date: 500 }, { id: 'd', date: 1000 }, { id: 'e', date: 1004 }];
eq('stackGroups chains neighbours within 10px (1000 wide, 0..1000)', M.stackGroups(items, 0, 1000, 1000, 10),
  [['a', 'b'], ['c'], ['d', 'e']]);
eq('stackGroups unsorted input', M.stackGroups([items[3], items[0], items[4]], 0, 1000, 1000, 10), [['a'], ['d', 'e']]);
eq('stackGroups on a flat axis stacks everything', M.stackGroups([{ id: 'x', date: 7 }, { id: 'y', date: 7 }], 7, 7, 800, 10), [['x', 'y']]);
eq('stackGroups empty', M.stackGroups([], 0, 10, 100, 10), []);

// readout
const ro = M.formatReadout(ep(2025, 3, 4, 18, 39), 'en-US');
eq('readout names weekday, date and time', /Tue/.test(ro) && /Mar 4, 2025/.test(ro) && /6:39\sPM/.test(ro), true);

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exit(failed ? 1 : 0);
