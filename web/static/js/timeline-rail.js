// Shared timeline rail: a vertical list, one row per entry in chronological
// order, with a small tick per entry and a larger tick + year/month label
// at each boundary -- fit to the full window height (flex space-between,
// no scrollbar), fixed to the viewport. Used on both the gallery page
// (project spans) and a project detail page (item points). See
// docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// Time is the primary content here, not the project/item itself: each row
// shows a tick + a day-of-month label (the month/year is already carried
// by the marker above it), not a thumbnail. Click opens a popup (the
// shared card-preview modal) with the actual item -- thumbnail, title,
// date, a link to the real page. An earlier version showed thumbnails
// inline with dock-style hover magnification; dropped in favor of this
// once real project data made clear the list needed to read as a
// chronology first, with the "what" available on demand rather than
// competing for space with the "when."

class TimelineRail {
  constructor(container, entries, options = {}) {
    this.container = container;
    this.entries = entries;
    this.onOpen = options.onOpen || function () {};
    this._render();
  }

  _sorted() {
    return [...this.entries].sort((a, b) => b.date - a.date);
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');

    this._track = document.createElement('div');
    this._track.className = 'timeline-track';
    this.container.appendChild(this._track);

    const sorted = this._sorted();
    let lastYear = null;
    let lastMonthKey = null;
    const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    sorted.forEach((entry) => {
      const d = new Date(entry.date * 1000);
      const year = d.getFullYear();
      const monthKey = `${year}-${d.getMonth()}`;
      if (year !== lastYear) {
        const marker = document.createElement('div');
        marker.className = 'timeline-year-marker';
        marker.textContent = year;
        this._track.appendChild(marker);
        lastYear = year;
        lastMonthKey = monthKey;
      } else if (monthKey !== lastMonthKey) {
        // Between year boundaries, a month marker is the only other
        // chronology cue -- without it, real data spanning just one or
        // two years (common early on) shows almost no temporal texture at
        // all beyond a couple of year labels at the top of the list.
        const marker = document.createElement('div');
        marker.className = 'timeline-month-marker';
        marker.textContent = MONTH_NAMES[d.getMonth()];
        this._track.appendChild(marker);
        lastMonthKey = monthKey;
      }

      const row = document.createElement('div');
      row.className = 'timeline-entry-row';

      const node = document.createElement('button');
      const isSpan = entry.endDate !== undefined && entry.endDate !== entry.date;
      node.type = 'button';
      node.className = 'timeline-entry'
        + (entry.isChild ? ' timeline-entry-child' : '')
        + (isSpan ? ' timeline-entry-span' : '');
      node.title = entry.label || '';
      node.textContent = String(d.getDate());

      node.addEventListener('click', () => this.onOpen(entry));
      row.appendChild(node);
      this._track.appendChild(row);
    });
  }
}
