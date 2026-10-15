// The one place the browser turns a stored date into text or calendar fields (#544). Every stored date
// is UTC unix SECONDS; every date SHOWN is Mountain Time (America/Denver, DST-aware), whatever timezone
// the browser is in -- the same rule core/datefmt.py applies server-side, so a card face, an item page
// and a timeline rail can't disagree about which day or month a moment falls in.
//
// Pure: no DOM, so scripts/test_mountain_time_544.js can run it under node. The browser gets it as
// window.MountainTime; node gets it from require().
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MountainTime = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  const TIME_ZONE = 'America/Denver';

  const partsFormat = new Intl.DateTimeFormat('en-US', {
    timeZone: TIME_ZONE, hourCycle: 'h23',
    year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric',
  });

  // unix seconds -> the Mountain wall clock {year, month (1-12), day, hour, minute, second}.
  function parts(epoch) {
    const out = {};
    partsFormat.formatToParts(new Date(epoch * 1000)).forEach((p) => {
      if (p.type !== 'literal') out[p.type] = parseInt(p.value, 10);
    });
    return out;
  }

  // Seconds Mountain is behind UTC at `epoch` (negative: -21600 in winter, -18000 in summer).
  function offsetAt(epoch) {
    const p = parts(epoch);
    return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) / 1000 - Math.floor(epoch);
  }

  // A Mountain wall clock -> unix seconds. Out-of-range fields roll over like Date.UTC (day 0 = last day
  // of the previous month, month 13 = January next year). The second pass settles the daylight-saving
  // edge, where the first guess uses the wrong side's offset.
  function toEpoch(year, month, day, hour, minute, second) {
    const asUtc = Date.UTC(year, month - 1, day, hour || 0, minute || 0, second || 0) / 1000;
    return asUtc - offsetAt(asUtc - offsetAt(asUtc));
  }

  // A picked calendar day ("YYYY-MM-DD", an <input type=date> value) -> unix seconds at MOUNTAIN midnight
  // that day (#621), the same instant core/timeline.py's source_datetime_to_epoch gives a bare date
  // server-side. Not UTC midnight: that is the previous evening in Mountain, so Oct 1 showed as Sep 30.
  // null for an empty or malformed value.
  function dayToEpoch(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value == null ? '' : value).trim());
    if (!m) return null;
    const year = +m[1], month = +m[2], day = +m[3];
    if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return null;
    return toEpoch(year, month, day, 0, 0, 0);
  }

  // unix seconds -> the Mountain calendar day it falls on, as "YYYY-MM-DD" (an <input type=date> value).
  // The inverse of dayToEpoch; '' for a missing or non-numeric epoch.
  function epochToDay(epoch) {
    if (epoch === null || epoch === undefined || epoch === '' || !isFinite(Number(epoch))) return '';
    const p = parts(Number(epoch));
    const pad = (n, w) => String(n).padStart(w, '0');
    return `${pad(p.year, 4)}-${pad(p.month, 2)}-${pad(p.day, 2)}`;
  }

  // "9/30/2026"-style (the browser locale's short date), in Mountain Time.
  function formatDate(epoch, locale) {
    return new Intl.DateTimeFormat(locale || undefined, { timeZone: TIME_ZONE }).format(new Date(epoch * 1000));
  }

  // "6:39 PM"-style, in Mountain Time.
  function formatTime(epoch, locale) {
    return new Intl.DateTimeFormat(locale || undefined, {
      timeZone: TIME_ZONE, hour: 'numeric', minute: '2-digit' }).format(new Date(epoch * 1000));
  }

  // The browser locale's full date and time (the admin page's "Oldest deleted ..."), in Mountain Time.
  function formatDateTime(epoch, locale) {
    return new Intl.DateTimeFormat(locale || undefined, {
      timeZone: TIME_ZONE, year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(new Date(epoch * 1000));
  }

  // "Tue, Mar 4, 2025, 6:39 PM" (the drag readout), in Mountain Time.
  function formatLong(epoch, locale) {
    return new Intl.DateTimeFormat(locale || undefined, {
      timeZone: TIME_ZONE, weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit' }).format(new Date(epoch * 1000));
  }

  return { TIME_ZONE, parts, toEpoch, dayToEpoch, epochToDay, formatDate, formatTime, formatDateTime, formatLong };
});
