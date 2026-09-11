// Shared timeline rail: dormant (small, evenly spaced) vs. interactive
// (dock-magnified, time-proportional spacing) states. Used on both the
// gallery page (project spans) and a project detail page (item points).
// See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.

class TimelineRail {
  constructor(container, entries, options = {}) {
    this.container = container;
    this.entries = entries;
    this.onOpen = options.onOpen || function () {};
    this.mode = 'dormant';
    this._idleTimer = null;
    this._render();
    this._bindEvents();
  }

  _sorted() {
    return [...this.entries].sort((a, b) => a.date - b.date);
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');
    this._entries = this._sorted();
    this._entries.forEach((entry) => {
      const node = document.createElement('button');
      node.type = 'button';
      node.className = 'timeline-entry' + (entry.isChild ? ' timeline-entry-child' : '');
      node.dataset.id = entry.id;

      const thumb = document.createElement('img');
      thumb.className = 'timeline-thumb';
      thumb.loading = 'lazy';
      thumb.src = entry.thumbUrl || '';
      thumb.alt = entry.label || '';
      node.appendChild(thumb);

      if (entry.endDate !== undefined && entry.endDate !== entry.date) {
        const bracket = document.createElement('span');
        bracket.className = 'timeline-bracket';
        node.appendChild(bracket);
      }

      node.addEventListener('click', () => this.onOpen(entry));
      this.container.appendChild(node);
      entry._node = node;
    });
  }

  _bindEvents() {
    this.container.addEventListener('mousemove', (e) => this._onMouseMove(e));
    this.container.addEventListener('mouseleave', () => this._scheduleDormant());
  }

  _onEngage() {
    if (this.mode !== 'interactive') {
      this.mode = 'interactive';
      this.container.classList.add('timeline-rail-interactive');
    }
    this._applyProportionalSpacing();
    this._scheduleDormant();
  }

  _scheduleDormant() {
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => this._toDormant(), 1200);
  }

  _toDormant() {
    this.mode = 'dormant';
    this.container.classList.remove('timeline-rail-interactive');
    this._entries.forEach((entry) => {
      entry._node.style.transform = '';
      entry._node.style.marginTop = '';
    });
  }

  _applyProportionalSpacing() {
    if (this._entries.length === 0) return;
    const dates = this._entries.map((e) => e.date);
    const min = Math.min(...dates);
    const max = Math.max(...dates);
    const span = max - min || 1;
    const railHeight = this.container.clientHeight || this._entries.length * 48;
    let prevPx = 0;
    this._entries.forEach((entry, i) => {
      const px = ((entry.date - min) / span) * railHeight;
      const gap = i === 0 ? 0 : Math.max(px - prevPx, 8);
      entry._node.style.marginTop = `${gap}px`;
      prevPx = px;
    });
  }

  _onMouseMove(e) {
    this._onEngage();
    const rect = this.container.getBoundingClientRect();
    const cursorY = e.clientY - rect.top;
    this._entries.forEach((entry) => {
      const node = entry._node;
      const nodeRect = node.getBoundingClientRect();
      const nodeCenterY = nodeRect.top - rect.top + nodeRect.height / 2;
      const distance = Math.abs(cursorY - nodeCenterY);
      const scale = Math.max(1, 1.8 - distance / 80);
      node.style.transform = `scale(${scale.toFixed(2)})`;
    });
  }
}
