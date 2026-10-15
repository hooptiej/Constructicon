// #621: a calendar day picked in the blog builder is stored as MOUNTAIN midnight, not UTC midnight
// (which is the previous evening in Mountain, so "Oct 1" showed as Sep 30).
//   node scripts/test_blog_date_621.js
// Checks web/static/js/mountain-time.js's dayToEpoch / epochToDay under several browser zones, and that
// web/templates/_blog_builder.html uses them (and loads mountain-time.js) instead of the old UTC parse.
const zones = ['America/Denver', 'Pacific/Auckland', 'Pacific/Honolulu', 'UTC'];
const path = require('path');
const fs = require('fs');

if (!process.env.MT_TEST_ZONE) {
  const failedZones = [];
  for (const tz of zones) {
    console.log(`--- TZ=${tz}`);
    const r = require('child_process').spawnSync(process.execPath, [__filename], {
      stdio: 'inherit', env: { ...process.env, TZ: tz, MT_TEST_ZONE: tz } });
    if (r.status !== 0) failedZones.push(tz);
  }

  // The template: static checks, once.
  const failed = [];
  const check = (name, ok) => { console.log((ok ? 'PASS ' : 'FAIL ') + name); if (!ok) failed.push(name); };
  const tpl = fs.readFileSync(path.join(__dirname, '..', 'web', 'templates', '_blog_builder.html'), 'utf8');
  check('builder no longer parses the picked day as UTC midnight', !tpl.includes("T00:00:00Z"));
  check('builder no longer reads the stored date back with toISOString', !tpl.includes('toISOString'));
  check('builder converts with MountainTime.dayToEpoch', tpl.includes('MountainTime.dayToEpoch('));
  check('builder shows the date with MountainTime.epochToDay', tpl.includes('MountainTime.epochToDay('));
  const loadAt = tpl.indexOf('/static/js/mountain-time.js');
  const useAt = tpl.indexOf('MountainTime.dayToEpoch(');
  check('builder loads mountain-time.js before its own script uses it', loadAt !== -1 && loadAt < useAt);
  check('builder leaves an untouched date out of an update', /dateInput\.value !== loadedDateValue/.test(tpl));

  for (const tz of failedZones) failed.push(`zone run TZ=${tz}`);
  console.log(failed.length ? `FAILED: ${failed.join(', ')}` : 'ALL PASS');
  process.exitCode = failed.length ? 1 : 0;
} else {
  const MT = require(path.join(__dirname, '..', 'web', 'static', 'js', 'mountain-time.js'));
  const failed = [];
  const eq = (name, got, want) => {
    const ok = JSON.stringify(got) === JSON.stringify(want);
    console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : `: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`));
    if (!ok) failed.push(name);
  };
  const utc = (y, mo, d, h = 0) => Date.UTC(y, mo - 1, d, h) / 1000;

  // The bug: Oct 1 picked -> stored at UTC midnight -> shown as Sep 30 in Mountain.
  eq('Oct 1 is stored at Mountain midnight (06:00Z in MDT)', MT.dayToEpoch('2026-10-01'), utc(2026, 10, 1, 6));
  eq('Jan 15 is stored at Mountain midnight (07:00Z in MST)', MT.dayToEpoch('2026-01-15'), utc(2026, 1, 15, 7));
  eq('the stored Oct 1 shows as Oct 1 in Mountain', MT.formatDate(MT.dayToEpoch('2026-10-01'), 'en-US'), '10/1/2026');
  eq('the old UTC-midnight value is what showed a day early', MT.epochToDay(utc(2026, 10, 1)), '2026-09-30');

  // Round trips, including both daylight-saving change days and a leap day.
  for (const day of ['2026-10-01', '2026-03-08', '2026-11-01', '2024-02-29', '1999-12-31', '2026-12-31', '1970-01-02']) {
    eq(`round trip ${day}`, MT.epochToDay(MT.dayToEpoch(day)), day);
  }
  eq('DST start day is local midnight (07:00Z, still MST)', MT.dayToEpoch('2026-03-08'), utc(2026, 3, 8, 7));
  eq('DST end day is local midnight (06:00Z, still MDT)', MT.dayToEpoch('2026-11-01'), utc(2026, 11, 1, 6));
  // Matches the server's rule: core/timeline.py source_datetime_to_epoch(datetime(2017, 7, 31)) (naive
  // date = Mountain) is 2017-07-31T06:00:00Z.
  eq('same instant the server gives a bare date', MT.dayToEpoch('2017-07-31'), utc(2017, 7, 31, 6));

  // Empty and malformed input.
  eq('empty -> null', MT.dayToEpoch(''), null);
  eq('null -> null', MT.dayToEpoch(null), null);
  eq('garbage -> null', MT.dayToEpoch('Oct 1'), null);
  eq('month 13 -> null', MT.dayToEpoch('2026-13-01'), null);
  eq('Feb 30 -> null', MT.dayToEpoch('2026-02-30'), null);
  eq('epochToDay of null -> ""', MT.epochToDay(null), '');
  eq('epochToDay of "" -> ""', MT.epochToDay(''), '');
  eq('epochToDay of a numeric string', MT.epochToDay(String(utc(2026, 10, 1, 6))), '2026-10-01');
  eq('epochToDay of junk -> ""', MT.epochToDay('abc'), '');

  console.log(failed.length ? `FAILED: ${failed.join(', ')}` : 'ALL PASS');
  process.exitCode = failed.length ? 1 : 0;
}
