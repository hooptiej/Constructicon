// Unit test for web/static/js/mountain-time.js and its users in timeline-drag-math.js (#544).
//   node scripts/test_mountain_time_544.js
// The helper must give Mountain answers whatever zone the browser is in, so this re-runs itself under
// a zone east of UTC (Pacific/Auckland), a zone west of Mountain (Pacific/Honolulu) and UTC itself.
const zones = ['America/Denver', 'Pacific/Auckland', 'Pacific/Honolulu', 'UTC'];
if (!process.env.MT_TEST_ZONE) {
  let bad = 0;
  for (const tz of zones) {
    console.log(`--- TZ=${tz}`);
    const r = require('child_process').spawnSync(process.execPath, [__filename], {
      stdio: 'inherit', env: { ...process.env, TZ: tz, MT_TEST_ZONE: tz } });
    if (r.status !== 0) { bad++; console.log(`run under TZ=${tz} failed (status ${r.status}, signal ${r.signal})`); }
  }
  console.log(bad ? `\n${bad} zone run(s) FAILED` : '\nall zone runs passed');
  process.exitCode = bad ? 1 : 0;
} else {
  const path = require('path');
  const MT = require(path.join(__dirname, '..', 'web', 'static', 'js', 'mountain-time.js'));
  const M = require(path.join(__dirname, '..', 'web', 'static', 'js', 'timeline-drag-math.js'));
  let failed = 0;
  const eq = (name, got, want) => {
    const ok = JSON.stringify(got) === JSON.stringify(want);
    console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : `: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
    if (!ok) failed++;
  };
  const utc = (y, mo, d, h = 0, mi = 0, s = 0) => Date.UTC(y, mo - 1, d, h, mi, s) / 1000;
  const wall = (e) => { const p = MT.parts(e); return [p.year, p.month, p.day, p.hour, p.minute]; };

  // the key case: 2026-01-01T03:00Z is still Dec 31, 2025 in Mountain
  const NEW_YEAR = utc(2026, 1, 1, 3, 0);
  eq('Jan 1 03:00Z is Dec 31 20:00 Mountain', wall(NEW_YEAR), [2025, 12, 31, 20, 0]);
  eq('formatDate shows Dec 31, not Jan 1', MT.formatDate(NEW_YEAR, 'en-US'), '12/31/2025');
  eq('formatTime shows the Mountain clock', MT.formatTime(NEW_YEAR, 'en-US').replace(/\s/g, ' '), '8:00 PM');
  eq('formatDateTime', MT.formatDateTime(NEW_YEAR, 'en-US').replace(/\s/g, ' '), '12/31/2025, 8:00:00 PM');
  eq('summer is MDT (UTC-6)', wall(utc(2026, 10, 1, 1, 0)), [2026, 9, 30, 19, 0]);
  eq('midnight is hour 0, not 24', wall(utc(2026, 7, 1, 6, 0)), [2026, 7, 1, 0, 0]);

  // wall clock -> epoch, including the daylight-saving edges (2026: Mar 8 spring-forward, Nov 1 fall-back)
  eq('toEpoch winter', MT.toEpoch(2026, 1, 15, 12, 0), utc(2026, 1, 15, 19, 0));
  eq('toEpoch summer', MT.toEpoch(2026, 7, 15, 12, 0), utc(2026, 7, 15, 18, 0));
  eq('toEpoch Jan 1 00:00 Mountain', MT.toEpoch(2026, 1, 1), utc(2026, 1, 1, 7, 0));
  eq('toEpoch the day after spring-forward keeps noon', wall(MT.toEpoch(2026, 3, 9, 12, 0)), [2026, 3, 9, 12, 0]);
  eq('toEpoch rolls day 0 back to the last day of the month', wall(MT.toEpoch(2026, 3, 0, 12, 0)), [2026, 2, 28, 12, 0]);
  eq('toEpoch rolls month 13 into next January', wall(MT.toEpoch(2026, 13, 1, 12, 0)), [2027, 1, 1, 12, 0]);

  // the drag maths lands on Mountain days, whatever the browser's zone
  const orig = MT.toEpoch(2026, 10, 6, 18, 39, 12);
  eq('snapDay moves the Mountain day, keeps 6:39 PM', wall(M.snapDay(MT.toEpoch(2025, 3, 4, 3, 0), orig)), [2025, 3, 4, 18, 39]);
  eq('snapDay of a drop that is already the next day in UTC stays on the Mountain day', wall(M.snapDay(utc(2025, 3, 5, 3, 0), orig)), [2025, 3, 4, 18, 39]);
  eq('snapDay across DST keeps wall-clock time', wall(M.snapDay(MT.toEpoch(2025, 3, 9, 12), orig)), [2025, 3, 9, 18, 39]);
  eq('addDays over spring-forward keeps the wall clock', wall(M.keyStep(MT.toEpoch(2026, 3, 7, 18, 39), 'ArrowRight', false)), [2026, 3, 8, 18, 39]);
  eq('addDays over fall-back keeps the wall clock', wall(M.keyStep(MT.toEpoch(2026, 10, 31, 18, 39), 'ArrowRight', false)), [2026, 11, 1, 18, 39]);
  eq('addMonths clamps Jan 31 to Feb 28', wall(M.addMonths(MT.toEpoch(2026, 1, 31, 9, 0), 1)), [2026, 2, 28, 9, 0]);
  eq('addMonths backwards across a year', wall(M.addMonths(MT.toEpoch(2026, 1, 15, 9, 0), -1)), [2025, 12, 15, 9, 0]);
  eq('addMonths forward across a year', wall(M.addMonths(MT.toEpoch(2025, 12, 15, 9, 0), 1)), [2026, 1, 15, 9, 0]);
  eq('Shift+Left is a month back', wall(M.keyStep(MT.toEpoch(2026, 3, 15, 9, 0), 'ArrowLeft', true)), [2026, 2, 15, 9, 0]);
  const ro = M.formatReadout(MT.toEpoch(2025, 3, 4, 18, 39), 'en-US');
  eq('readout is the Mountain weekday, date and time', /Tue/.test(ro) && /Mar 4, 2025/.test(ro) && /6:39\sPM/.test(ro), true);

  console.log(failed ? `\n${failed} FAILED` : '\nall passed');
  process.exitCode = failed ? 1 : 0;
}
