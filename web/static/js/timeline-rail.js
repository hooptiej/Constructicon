// Shared timeline rail: entries grouped by year, in normal document flow.
// Used on both the gallery page (project spans) and a project detail page
// (item points). See
// docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// A prior version animated entries (dock-style magnification on hover,
// spacing morphing from even to time-proportional) using absolute
// positioning computed from each entry's raw date. Dropped entirely: (1)
// real project dates cluster heavily, so linear date-to-pixel mapping
// collapsed almost everything to two points at the extremes: no readable
// timeline resulted; (2) the rail sits flush against the viewport's left
// edge, so a scale() transform had nowhere to expand into but off-screen.
// Year grouping sidesteps both -- it's not a continuous axis, so clustered
// dates just mean a bigger group under one label, and normal flow can't
// clip or drift since there's no absolute positioning or transform at all.

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

  _groupByYear(sorted) {
    const groups = [];
    let current = null;
    sorted.forEach((entry) => {
      const year = new Date(entry.date * 1000).getFullYear();
      if (!current || current.year !== year) {
        current = { year, entries: [] };
        groups.push(current);
      }
      current.entries.push(entry);
    });
    return groups;
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');
    const sorted = this._sorted();
    const groups = this._groupByYear(sorted);

    groups.forEach((group) => {
      const groupEl = document.createElement('div');
      groupEl.className = 'timeline-year-group';

      const label = document.createElement('div');
      label.className = 'timeline-year-label';
      label.textContent = group.year;
      groupEl.appendChild(label);

      const entriesEl = document.createElement('div');
      entriesEl.className = 'timeline-year-entries';
      group.entries.forEach((entry) => {
        const node = document.createElement('button');
        const isSpan = entry.endDate !== undefined && entry.endDate !== entry.date;
        node.type = 'button';
        node.className = 'timeline-entry'
          + (entry.isChild ? ' timeline-entry-child' : '')
          + (isSpan ? ' timeline-entry-span' : '');
        node.title = entry.label || '';

        const thumb = document.createElement('img');
        thumb.className = 'timeline-thumb';
        thumb.loading = 'lazy';
        thumb.src = entry.thumbUrl || '';
        thumb.alt = entry.label || '';
        node.appendChild(thumb);

        node.addEventListener('click', () => this.onOpen(entry));
        entriesEl.appendChild(node);
      });
      groupEl.appendChild(entriesEl);
      this.container.appendChild(groupEl);
    });
  }
}
