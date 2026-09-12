// Shared timeline rail: a vertical list, one row per entry in chronological
// order, with a small tick per entry and a larger tick + year label at each
// year boundary -- fit to the full window height (flex space-between, no
// scrollbar), fixed to the viewport. Hovering magnifies nearby entries
// dock-style, growing rightward from their left edge. Used on both the
// gallery page (project spans) and a project detail page (item points).
// See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// The magnification here is deliberately transform-only: mousemove sets
// each node's `transform: scale()` and nothing else. transform is a
// compositor property -- it doesn't affect layout, so it can't push
// siblings around or drift positions across repeated calls the way an
// earlier version's margin/absolute-position recalculation did. Position
// is set exactly once, by the static flex column below, and never
// touched again.

class TimelineRail {
  constructor(container, entries, options = {}) {
    this.container = container;
    this.entries = entries;
    this.onOpen = options.onOpen || function () {};
    this._entryNodes = [];
    this._render();
    this._bindEvents();
  }

  _sorted() {
    return [...this.entries].sort((a, b) => b.date - a.date);
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');
    this._entryNodes = [];

    this._track = document.createElement('div');
    this._track.className = 'timeline-track';
    this.container.appendChild(this._track);

    const sorted = this._sorted();
    let lastYear = null;

    sorted.forEach((entry) => {
      const year = new Date(entry.date * 1000).getFullYear();
      if (year !== lastYear) {
        const marker = document.createElement('div');
        marker.className = 'timeline-year-marker';
        marker.textContent = year;
        this._track.appendChild(marker);
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
      this._track.appendChild(row);
      this._entryNodes.push(node);
    });
  }

  _bindEvents() {
    this.container.addEventListener('mousemove', (e) => this._onMouseMove(e));
    this.container.addEventListener('mouseleave', () => this._resetScale());
  }

  _onMouseMove(e) {
    // Discrete tiers, not a continuous falloff: the closest entry to the
    // cursor gets the full HOVER_SCALE, its immediate neighbors (one on
    // each side) get NEIGHBOR_SCALE, everything else stays at 1. A
    // continuous distance-based falloff (tried first) always ties how far
    // the effect reaches to the peak scale -- turning up the peak
    // inevitably turns up how much neighbors move too.
    const HOVER_SCALE = 6.6;
    const NEIGHBOR_SCALE = HOVER_SCALE / 2;
    const cursorY = e.clientY;

    let closestIndex = -1;
    let closestDistance = Infinity;
    this._entryNodes.forEach((node, i) => {
      const rect = node.getBoundingClientRect();
      const centerY = rect.top + rect.height / 2;
      const distance = Math.abs(cursorY - centerY);
      if (distance < closestDistance) {
        closestDistance = distance;
        closestIndex = i;
      }
    });

    this._entryNodes.forEach((node, i) => {
      const offset = Math.abs(i - closestIndex);
      const scale = offset === 0 ? HOVER_SCALE : offset === 1 ? NEIGHBOR_SCALE : 1;
      node.style.transform = scale > 1 ? `scale(${scale.toFixed(2)})` : '';
      node.style.zIndex = scale > 1 ? 10 : '';
    });
  }

  _resetScale() {
    this._entryNodes.forEach((node) => {
      node.style.transform = '';
      node.style.zIndex = '';
    });
  }
}
