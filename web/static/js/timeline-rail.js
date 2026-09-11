// Shared timeline rail: dormant (small, evenly spaced) vs. interactive
// (dock-magnified, time-proportional spacing) states. Used on both the
// gallery page (project spans) and a project detail page (item points).
// See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// Entries are absolutely positioned within a .timeline-track, each one's
// `top` computed independently from its own date/index every layout pass
// -- not accumulated from the previous entry's position. A margin-stacking
// approach was tried first and drifted entries out of view on repeated
// mousemove-triggered relayouts; independent absolute positions can't
// drift since nothing compounds across entries or across calls.

const ENTRY_HEIGHT = 40;
const DORMANT_SPACING = 48;

class TimelineRail {
  constructor(container, entries, options = {}) {
    this.container = container;
    this.entries = entries;
    this.onOpen = options.onOpen || function () {};
    this.mode = 'dormant';
    this._idleTimer = null;
    this._render();
    this._bindEvents();
    this._layoutDormant();
  }

  _sorted() {
    return [...this.entries].sort((a, b) => a.date - b.date);
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');
    this._entries = this._sorted();

    this._track = document.createElement('div');
    this._track.className = 'timeline-track';
    this.container.appendChild(this._track);

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
      this._track.appendChild(node);
      entry._node = node;
    });
  }

  _bindEvents() {
    this.container.addEventListener('mousemove', (e) => this._onMouseMove(e));
    this.container.addEventListener('mouseleave', () => this._scheduleDormant());
  }

  _railHeight() {
    // container.clientHeight is the *visible* scrollable area -- always
    // usable as the layout height even for a long list, since the track's
    // own height (set below) is what actually makes it scrollable.
    return Math.max(this.container.clientHeight, 200);
  }

  _layoutDormant() {
    this.mode = 'dormant';
    this.container.classList.remove('timeline-rail-interactive');
    this._entries.forEach((entry, i) => {
      entry._node.style.top = `${i * DORMANT_SPACING}px`;
      entry._node.style.transform = '';
    });
    this._track.style.height = `${Math.max(this._entries.length * DORMANT_SPACING, this._railHeight())}px`;
  }

  _layoutProportional() {
    if (this._entries.length === 0) return;
    this.mode = 'interactive';
    this.container.classList.add('timeline-rail-interactive');
    const dates = this._entries.map((e) => e.date);
    const min = Math.min(...dates);
    const max = Math.max(...dates);
    const span = max - min || 1;
    const trackHeight = this._railHeight();
    const usableHeight = Math.max(trackHeight - ENTRY_HEIGHT, 0);
    this._entries.forEach((entry) => {
      const top = ((entry.date - min) / span) * usableHeight;
      entry._node.style.top = `${top}px`;
    });
    this._track.style.height = `${trackHeight}px`;
  }

  _onEngage() {
    if (this.mode !== 'interactive') {
      this._layoutProportional();
    }
    this._scheduleDormant();
  }

  _scheduleDormant() {
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => this._layoutDormant(), 1200);
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
