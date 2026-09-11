// Shared timeline rail: a vertical list, one row per entry in chronological
// order, with a small tick per entry and a larger tick + year label at each
// year boundary. Used on both the gallery page (project spans) and a
// project detail page (item points). See
// docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// Earlier versions tried: (1) absolute-positioned entries animated on
// hover (dock magnification + spacing morph from even to time-
// proportional) -- dropped because real dates cluster heavily (linear
// date-to-pixel mapping collapsed almost everything to two points) and a
// scale() transform flush against the viewport edge had nowhere to expand
// but off-screen; (2) entries grouped by year into flex-wrap rows --
// dropped because packing entries into wrapped rows destroys the one cue
// that actually reads as "chronology": position down the page. A single
// vertical list restores that (top-to-bottom reading order = time order)
// without needing continuous date-to-pixel math at all.

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
    const sorted = this._sorted();
    let lastYear = null;

    sorted.forEach((entry) => {
      const year = new Date(entry.date * 1000).getFullYear();
      if (year !== lastYear) {
        const marker = document.createElement('div');
        marker.className = 'timeline-year-marker';
        marker.textContent = year;
        this.container.appendChild(marker);
        lastYear = year;
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

      const thumb = document.createElement('img');
      thumb.className = 'timeline-thumb';
      thumb.loading = 'lazy';
      thumb.src = entry.thumbUrl || '';
      thumb.alt = entry.label || '';
      node.appendChild(thumb);

      node.addEventListener('click', () => this.onOpen(entry));
      row.appendChild(node);
      this.container.appendChild(row);
    });
  }
}
