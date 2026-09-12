// Shared timeline rail: a vertical list, one row per entry in chronological
// order, with a small tick per entry and a larger tick + year/month label
// at each boundary -- fit to the full window height (flex space-between,
// no scrollbar), fixed to the viewport. Used on both the gallery page
// (project spans) and a project detail page (item points). See
// docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// Every year between the earliest and latest entry gets its own marker,
// even years with zero entries in them -- a real chronology has to show
// the gaps, not just the years something happened. Hovering an entry
// shows a lightweight popover (no backdrop -- meant to be glanced at while
// moving through the list, not a modal dialog); clicking navigates
// straight to the real page. An earlier version reversed this (click
// opened a popup, no hover) and rendered nothing before a year that
// happened to have no entries, both fixed here.

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

class TimelineRail {
  constructor(container, entries, options = {}) {
    this.container = container;
    this.entries = entries;
    this.onOpen = options.onOpen || function () {};
    this._popover = null;
    this._rows = [];
    this._render();
    this._onResize = () => this._layout();
    window.addEventListener('resize', this._onResize);
  }

  _sorted() {
    return [...this.entries].sort((a, b) => b.date - a.date);
  }

  _ensurePopover() {
    if (this._popover) return this._popover;
    const el = document.createElement('div');
    el.className = 'timeline-popover';
    el.innerHTML = `
      <img class="timeline-popover-cover" alt="">
      <div class="timeline-popover-title"></div>
      <div class="timeline-popover-date"></div>
    `;
    document.body.appendChild(el);
    this._popover = el;
    return el;
  }

  _showPopover(entry, node) {
    const popover = this._ensurePopover();
    const cover = popover.querySelector('.timeline-popover-cover');
    if (entry.thumbUrl) {
      cover.src = entry.thumbUrl;
      cover.style.display = '';
    } else {
      cover.style.display = 'none';
    }
    popover.querySelector('.timeline-popover-title').textContent = entry.label || '';
    popover.querySelector('.timeline-popover-date').textContent =
      entry.dateLabel || new Date(entry.date * 1000).toLocaleDateString();

    popover.classList.add('visible');
    // Measure after making it visible (offsetHeight is 0 while display:none).
    const nodeRect = node.getBoundingClientRect();
    const popoverRect = popover.getBoundingClientRect();
    let top = nodeRect.top + nodeRect.height / 2 - popoverRect.height / 2;
    top = Math.max(8, Math.min(top, window.innerHeight - popoverRect.height - 8));
    popover.style.left = `${nodeRect.right + 10}px`;
    popover.style.top = `${top}px`;
  }

  _hidePopover() {
    if (this._popover) this._popover.classList.remove('visible');
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');

    this._track = document.createElement('div');
    this._track.className = 'timeline-track';
    this.container.appendChild(this._track);
    this._rows = [];

    const sorted = this._sorted();
    if (sorted.length === 0) return;

    const years = sorted.map((entry) => new Date(entry.date * 1000).getFullYear());
    const maxYear = Math.max(...years);
    const minYear = Math.min(...years);

    let entryIndex = 0;
    for (let year = maxYear; year >= minYear; year--) {
      const yearMarker = document.createElement('div');
      yearMarker.className = 'timeline-year-marker';
      yearMarker.textContent = year;
      this._track.appendChild(yearMarker);
      this._rows.push({ el: yearMarker, kind: 'year' });

      let lastMonth = null;
      while (entryIndex < sorted.length && new Date(sorted[entryIndex].date * 1000).getFullYear() === year) {
        const entry = sorted[entryIndex];
        const d = new Date(entry.date * 1000);
        if (d.getMonth() !== lastMonth) {
          const monthMarker = document.createElement('div');
          monthMarker.className = 'timeline-month-marker';
          monthMarker.textContent = MONTH_NAMES[d.getMonth()];
          this._track.appendChild(monthMarker);
          this._rows.push({ el: monthMarker, kind: 'month' });
          lastMonth = d.getMonth();
        }

        const row = document.createElement('div');
        row.className = 'timeline-entry-row';

        const node = document.createElement('button');
        const isSpan = entry.endDate !== undefined && entry.endDate !== entry.date;
        node.type = 'button';
        node.className = 'timeline-entry'
          + (entry.isChild ? ' timeline-entry-child' : '')
          + (isSpan ? ' timeline-entry-span' : '');
        node.textContent = `${d.getMonth() + 1}/${d.getDate()}`;

        node.addEventListener('mouseenter', () => this._showPopover(entry, node));
        node.addEventListener('mouseleave', () => this._hidePopover());
        node.addEventListener('click', () => this.onOpen(entry));

        row.appendChild(node);
        this._track.appendChild(row);
        this._rows.push({ el: row, kind: 'entry', button: node });
        entryIndex++;
      }
    }

    this._layout();
  }

  _layout() {
    // Fit every row (year/month markers + entry rows alike) into exactly
    // the rail's real available height, whatever that is -- no scrollbar,
    // ever (per owner's explicit call), and no reliance on flexbox's
    // natural content sizing + space-between, which only spaces existing
    // content out evenly and does nothing once there are enough rows to
    // overflow the container. Recomputed on every render (entry count can
    // grow between page loads) and on window resize (the viewport itself
    // can change height without a reload).
    const total = this._rows.length;
    if (total === 0) return;
    const availableHeight = this.container.clientHeight;
    const rowHeight = availableHeight / total;

    this._rows.forEach(({ el, kind, button }) => {
      el.style.height = `${rowHeight}px`;
      el.style.display = 'flex';
      el.style.alignItems = 'center';
      // Entry rows stay visible so the hover-magnify transform (#292, see
      // .timeline-entry:hover) isn't clipped by its own compressed row --
      // year/month markers keep hidden since their own text truncation
      // still matters and they don't magnify.
      el.style.overflow = kind === 'entry' ? 'visible' : 'hidden';
      if (kind === 'year') {
        el.style.fontSize = `${Math.max(7, Math.min(11, rowHeight * 0.85))}px`;
      } else if (kind === 'month') {
        el.style.fontSize = `${Math.max(6, Math.min(10, rowHeight * 0.75))}px`;
      } else {
        // Entry row: shrink the pill itself, not just its row, so it never
        // sticks out past a compressed row into its neighbors.
        const buttonHeight = Math.max(6, Math.min(18, rowHeight - 2));
        button.style.height = `${buttonHeight}px`;
        button.style.fontSize = `${Math.max(5, Math.min(10, buttonHeight * 0.55))}px`;
        button.style.minWidth = `${Math.max(14, Math.min(28, buttonHeight * 1.6))}px`;
      }
    });
  }
}
