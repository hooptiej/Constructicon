// #541 phase B: the "Moved to trash ... Undo" bar shown after an item delete or redact.
// window.showUndoBar(message, batchId, done): a fixed bar at the bottom of the page with Undo and
// Close. Undo POSTs /api/changes/{batchId}/undo (the generic change-log undo); `done` is then
// called with "undone", or "closed" when the bar is dismissed (or after 30 s), so the page can
// reload or navigate. A failed undo (e.g. trash_expired) shows its message in the bar.
(function () {
  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    Object.assign(n, attrs || {});
    if (text) n.textContent = text;
    return n;
  }

  window.showUndoBar = function (message, batchId, done) {
    const old = document.getElementById('undo-bar');
    if (old) old.remove();
    const bar = el('div', { id: 'undo-bar', role: 'status' });
    bar.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:1000;' +
      'display:flex;gap:12px;align-items:center;max-width:min(640px,calc(100vw - 32px));padding:10px 14px;' +
      'background:var(--card,#201F17);color:var(--text,#ECE6D2);border:1px solid var(--border-stronger,#444);' +
      'border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.45);font-size:14px;';
    const msg = el('span', {}, message);
    msg.style.flex = '1';
    const undo = el('button', { type: 'button', className: 'btn' }, 'Undo');
    const close = el('button', { type: 'button', className: 'btn', title: 'Dismiss' }, '×');
    bar.append(msg, undo, close);
    document.body.appendChild(bar);

    let finished = false;
    const finish = (how) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      bar.remove();
      if (done) done(how);
    };
    const timer = setTimeout(() => finish('closed'), 30000);
    close.addEventListener('click', () => finish('closed'));
    undo.addEventListener('click', async () => {
      if (!batchId) return;
      undo.disabled = true;
      clearTimeout(timer);
      try {
        const res = await fetch(`/api/changes/${encodeURIComponent(batchId)}/undo`, { method: 'POST' });
        if (res.ok) { finish('undone'); return; }
        const body = await res.json().catch(() => ({}));
        msg.textContent = (body.error && body.error.message) || body.detail || 'Undo failed.';
      } catch (e) {
        msg.textContent = 'Undo failed: ' + e;
      }
      undo.remove();
    });
  };
})();
