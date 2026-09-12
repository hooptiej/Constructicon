// Horizontal, video-editor-style timeline for a project's detail page --
// distinct from the gallery page's vertical rail (web/static/js/timeline-rail.js).
// Sub-projects render as blocks on a track (they're spans, like clips);
// this project's own items render as point events below the track (like
// markers/keyframes). See
// docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.
//
// Position is continuous/proportional here (unlike the gallery rail's
// vertical list), which is what a video-editor timeline actually looks
// like -- workable at this scale because a single project's own blocks
// and events are one coherent time range, not the gallery's whole
// multi-decade, wildly-clustered spread where continuous positioning
// collapsed almost everything to two points.

const SCALE_MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

class ProjectVideoTimeline {
  constructor(container, { blocks = [], events = [] } = {}) {
    this.container = container;
    this.blocks = blocks;
    this.events = events;
    this._popover = null;
    this._render();
  }

  _range() {
    const dates = [
      ...this.blocks.flatMap((b) => [b.start, b.end]),
      ...this.events.map((e) => e.date),
    ];
    let min = Math.min(...dates);
    let max = Math.max(...dates);
    if (min === max) {
      // A single point in time -- widen artificially so it isn't a
      // divide-by-zero and renders as a visible sliver, not nothing.
      min -= 43200;
      max += 43200;
    }
    return { min, max };
  }

  _percent(date, range) {
    return ((date - range.min) / (range.max - range.min)) * 100;
  }

  _scaleTicks(range) {
    // Regular calendar-aligned ticks (a real ruler), independent of where
    // blocks/events actually fall -- yearly for a multi-year span, monthly
    // for a span of a few months to two years, weekly for anything
    // tighter than that.
    const spanDays = (range.max - range.min) / 86400;
    const minDate = new Date(range.min * 1000);
    const ticks = [];

    if (spanDays > 730) {
      let year = minDate.getFullYear();
      while (true) {
        const t = new Date(year, 0, 1).getTime() / 1000;
        if (t > range.max) break;
        if (t >= range.min) ticks.push({ date: t, label: String(year) });
        year++;
      }
    } else if (spanDays > 45) {
      let year = minDate.getFullYear();
      let month = minDate.getMonth();
      while (true) {
        const t = new Date(year, month, 1).getTime() / 1000;
        if (t > range.max) break;
        if (t >= range.min) ticks.push({ date: t, label: `${SCALE_MONTH_NAMES[month]} ${year}` });
        month++;
        if (month > 11) { month = 0; year++; }
      }
    } else {
      const dayMs = 7 * 86400;
      for (let t = range.min; t <= range.max; t += dayMs) {
        const d = new Date(t * 1000);
        ticks.push({ date: t, label: `${SCALE_MONTH_NAMES[d.getMonth()]} ${d.getDate()}` });
      }
    }
    return ticks;
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
    popover.querySelector('.timeline-popover-date').textContent = entry.dateLabel || '';

    popover.classList.add('visible');
    const nodeRect = node.getBoundingClientRect();
    const popoverRect = popover.getBoundingClientRect();
    let left = nodeRect.left + nodeRect.width / 2 - popoverRect.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - popoverRect.width - 8));
    popover.style.left = `${left}px`;
    popover.style.top = `${nodeRect.top - popoverRect.height - 10}px`;
  }

  _hidePopover() {
    if (this._popover) this._popover.classList.remove('visible');
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('project-video-timeline');
    if (this.blocks.length === 0 && this.events.length === 0) return;

    const range = this._range();

    const blockRow = document.createElement('div');
    blockRow.className = 'project-video-timeline-blocks';
    this.blocks.forEach((block) => {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'project-video-timeline-block';
      el.textContent = block.label;
      const left = this._percent(block.start, range);
      const width = Math.max(this._percent(block.end, range) - left, 1.5);
      el.style.left = `${left}%`;
      el.style.width = `${width}%`;
      el.addEventListener('mouseenter', () => this._showPopover(block, el));
      el.addEventListener('mouseleave', () => this._hidePopover());
      el.addEventListener('click', () => { window.location.href = block.openUrl; });
      blockRow.appendChild(el);
    });
    this.container.appendChild(blockRow);

    const scale = document.createElement('div');
    scale.className = 'project-video-timeline-scale';
    const scaleLine = document.createElement('div');
    scaleLine.className = 'project-video-timeline-scale-line';
    scale.appendChild(scaleLine);
    this._scaleTicks(range).forEach((tick) => {
      const tickEl = document.createElement('div');
      tickEl.className = 'project-video-timeline-scale-tick';
      tickEl.style.left = `${this._percent(tick.date, range)}%`;
      const mark = document.createElement('span');
      mark.className = 'project-video-timeline-scale-mark';
      const label = document.createElement('span');
      label.className = 'project-video-timeline-scale-label';
      label.textContent = tick.label;
      tickEl.appendChild(mark);
      tickEl.appendChild(label);
      scale.appendChild(tickEl);
    });
    this.container.appendChild(scale);

    const eventRow = document.createElement('div');
    eventRow.className = 'project-video-timeline-events';
    this.events.forEach((event) => {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'project-video-timeline-event';
      el.style.left = `${this._percent(event.date, range)}%`;
      el.addEventListener('mouseenter', () => this._showPopover(event, el));
      el.addEventListener('mouseleave', () => this._hidePopover());
      el.addEventListener('click', () => { window.location.href = event.openUrl; });
      eventRow.appendChild(el);
    });
    this.container.appendChild(eventRow);
  }
}
