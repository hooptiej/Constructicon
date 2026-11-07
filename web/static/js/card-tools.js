// #556: the card page's "Explain this card…" and "Export this card…" (the More menu, project_detail.html).
// Config: the page's JSON block #card-tools-config, {id, slug, title, formats}; formats come
// from core/project_export.FORMATS, each with `locked` = {need, message} when the user's role is below the
// format's door (#779: the format is drawn greyed and a click says why, never hidden).
// - Explain: GET /api/projects/{id}/explain, shown in plain words: the status, the five pips (each with
//   its reason), what the card needs, open questions, warnings, its files and the caption check, and the
//   last changes. Read-only.
// - Export: pick a format (zip / HTML) and, for HTML, a template (one today; the picker is built so more
//   slot in), then download. The file is fetched here and handed to the browser only when the answer is
//   OK, so a refusal (a private card, a role, a bad template) shows the server's own words instead of a
//   broken download.
// No inline handlers (CSP, #645/#660): everything is wired here. textContent only for server text.
(function () {
  'use strict';
  const cfgEl = document.getElementById('card-tools-config');
  if (!cfgEl) return;
  let cfg;
  try { cfg = JSON.parse(cfgEl.textContent); } catch (e) {
    console.error('card-tools (#556): the page config is unreadable; Explain and Export will say so.', e);
    cfg = null;
  }

  const PIPS = [['cover', 'Cover'], ['dates', 'Dates'], ['writeup', 'Write-up'],
                ['owner_words', "Owner's words"], ['stack', 'Stack']];

  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'className') n.className = v; else if (k === 'style') n.style.cssText = v; else n.setAttribute(k, v);
    });
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function errText(body, res) {
    return (body && body.error && body.error.message) || (body && body.detail) ||
      `The server answered ${res ? res.status : '?'} with no reason.`;
  }

  function dialog(id, title) {
    const backdrop = el('div', { className: 'modal-backdrop', id: id + '-backdrop' });
    const modal = el('div', { className: 'modal', style: 'width:640px;max-width:calc(100vw - 32px);max-height:85vh;overflow:auto',
      role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': id + '-title' });
    const header = el('div', { className: 'modal-header' });
    header.appendChild(el('h2', { id: id + '-title' }, title));
    const close = el('button', { type: 'button', className: 'modal-close', 'aria-label': 'Close' }, '×');
    header.appendChild(close);
    modal.appendChild(header);
    const body = el('div', { id: id + '-body' });
    modal.appendChild(body);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    const hide = () => backdrop.classList.remove('active');
    close.addEventListener('click', hide);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) hide(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && backdrop.classList.contains('active')) hide(); });
    return { body, show: () => backdrop.classList.add('active') };
  }

  function section(parent, title) {
    parent.appendChild(el('h3', { style: 'margin:14px 0 4px;font-size:14px' }, title));
    const ul = el('ul', { style: 'margin:0 0 0 18px;padding:0;font-size:13px;line-height:1.45' });
    parent.appendChild(ul);
    return ul;
  }

  function fmtDate(epoch) {
    if (!epoch) return '';
    try { return new Date(epoch * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); }
    catch (e) { return String(epoch); }
  }

  // ---------------------------------------------------------------- Explain
  let explain = null;
  function renderExplain(box, x) {
    box.replaceChildren();
    const st = [x.kind_label, x.stage_label].filter(Boolean).join(', ');
    box.appendChild(el('p', {}, `${x.title} is ${st ? 'a ' + st.toLowerCase() : 'a card'}` +
      (x.stop_reason_label ? ` (stopped: ${x.stop_reason_label.toLowerCase()})` : '') + '.' +
      (x.provisional ? ' Its status is still provisional (carried over from before cards had stages): confirm it in the Status group.' : '')));
    const lvl = x.level || {};
    const pips = section(box, `Pips: ${lvl.score || 0} of ${PIPS.length}`);
    PIPS.forEach(([k, label]) => {
      const ok = lvl.checks && lvl.checks[k];
      const li = el('li', {}, `${ok ? '✓' : '✗'} ${label}: ${(lvl.reasons && lvl.reasons[k]) || ''}`);
      if (!ok) li.style.color = 'var(--text-2)';
      pips.appendChild(li);
    });
    const needs = (x.needs || []);
    const decisions = (x.open_decisions || []);
    const wants = section(box, needs.length || decisions.length ? 'What it needs' : 'What it needs: nothing right now');
    needs.forEach(n => wants.appendChild(el('li', {}, n.detail || n.need)));
    decisions.forEach(d => wants.appendChild(el('li', {}, `Question waiting in the Curator: ${d.question || d.need}`)));
    if ((x.warnings || []).length) {
      const w = section(box, 'Warnings');
      x.warnings.forEach(t => w.appendChild(el('li', {}, t)));
    }
    const f = x.files || {};
    const files = section(box, `Files: ${(f.total || 0).toLocaleString()}`);
    const kinds = Object.entries(f.by_type || {}).sort((a, b) => b[1] - a[1]).map(([t, n]) => `${n} ${t}`).join(', ');
    if (kinds) files.appendChild(el('li', {}, `By type: ${kinds}`));
    if (f.earliest) files.appendChild(el('li', {}, `Dated ${fmtDate(f.earliest)}${f.latest && f.latest !== f.earliest ? ' to ' + fmtDate(f.latest) : ''}`));
    if (f.undated) files.appendChild(el('li', {}, `${f.undated} undated (no real date of their own)`));
    const cc = x.caption_check;
    if (cc) {
      // The reason already names the files it doesn't count and why (core/curator.py _captions_reason).
      files.appendChild(el('li', {}, `Captions: ${cc.passed ? 'enough of the files have one' : 'not enough of the files have one yet'}` +
        (cc.reason ? `. ${cc.reason}` : '')));
    }
    if ((x.recent_changes || []).length) {
      const ch = section(box, 'Last changes');
      x.recent_changes.forEach(r => ch.appendChild(el('li', {},
        `${fmtDate(r.timestamp)}: ${String(r.op || '').replace(/_/g, ' ')} by ${r.actor}${r.via ? ' (' + r.via + ')' : ''}${r.undone ? ', undone' : ''}`)));
    }
  }

  async function openExplain() {
    if (!explain) explain = dialog('explain-card', 'Explain this card');
    explain.show();
    const box = explain.body;
    if (!cfg) { box.replaceChildren(el('p', {}, 'This page is missing its config, so the card can\'t be explained here. Reload the page; if it persists, the MCP tool constructicon_explain_card answers the same.')); return; }
    box.replaceChildren(el('p', { role: 'status' }, 'Reading the card…'));
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(cfg.id)}/explain`);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(errText(body, res));
      renderExplain(box, body);
    } catch (e) {
      box.replaceChildren(el('p', { role: 'alert', style: 'color:var(--danger, #e06c5a)' }, `Couldn't explain the card: ${e.message}`));
    }
  }

  // ---------------------------------------------------------------- Export
  let exporter = null;
  function buildExport() {
    const d = dialog('export-card', 'Export this card');
    const box = d.body;
    if (!cfg || !(cfg.formats || []).length) {
      box.appendChild(el('p', {}, 'This page is missing its export formats, so nothing can be exported from here. Reload the page.'));
      return d;
    }
    box.appendChild(el('p', { className: 'dp-note' }, 'Pick a format. Sensitive files never leave the install, and anything left out is listed inside the download.'));
    const fs = el('fieldset', { style: 'border:0;padding:0;margin:8px 0' });
    fs.appendChild(el('legend', {}, 'Format'));
    let firstOpen = null;
    cfg.formats.forEach(f => {
      const row = el('div', { style: 'margin:6px 0' });
      const id = `export-format-${f.key}`;
      const r = el('input', { type: 'radio', name: 'export-format', id, value: f.key });
      const lab = el('label', { for: id, style: 'margin-left:6px' }, f.label);
      row.append(r, lab);
      row.appendChild(el('div', { className: 'dp-muted', style: 'margin-left:24px;font-size:12px' }, f.description));
      if (f.locked) {
        // The whole row is the locked control (a disabled radio gets no clicks of its own).
        r.disabled = true;
        const need = f.locked.need === 'admin' ? 'admins only' : f.locked.need === 'editor' ? 'editors and admins' : f.locked.need;
        if (window.RoleLock) window.RoleLock.decorate(row, f.locked.need, f.locked.message, `${f.label} (${need})`);
        else { row.classList.add('role-locked'); row.title = f.locked.message; }
      } else if (!firstOpen) { firstOpen = r; }
      fs.appendChild(row);
    });
    box.appendChild(fs);
    const tplRow = el('div', { style: 'margin:4px 0 8px 0', hidden: '' });
    tplRow.appendChild(el('label', { for: 'export-template' }, 'Template: '));
    const tpl = el('select', { id: 'export-template' });
    tplRow.appendChild(tpl);
    box.appendChild(tplRow);
    const go = el('button', { type: 'button', className: 'dp-btn' }, 'Download');
    const status = el('p', { role: 'status', 'aria-live': 'polite', style: 'margin:6px 0' });
    box.append(go, status);

    const chosen = () => cfg.formats.find(f => f.key === ((box.querySelector('input[name="export-format"]:checked') || {}).value));
    function sync() {
      const f = chosen();
      const ts = (f && f.templates) || [];
      tpl.replaceChildren(...ts.map(t => el('option', { value: t.key }, t.label)));
      tpl.disabled = ts.length < 2;
      tplRow.hidden = !ts.length;
      go.disabled = !f || !!f.locked;
    }
    box.querySelectorAll('input[name="export-format"]').forEach(r => r.addEventListener('change', sync));
    if (firstOpen) firstOpen.checked = true;
    sync();
    if (!firstOpen) status.textContent = 'Your role can\'t use any of these formats; click one to see who can.';

    go.addEventListener('click', async () => {
      const f = chosen();
      if (!f || f.locked) return;
      const url = f.url + (f.templates && f.templates.length ? `?template=${encodeURIComponent(tpl.value)}` : '');
      go.disabled = true;
      status.style.color = '';
      status.textContent = f.key === 'html' ? 'Building the site…' : 'Packing the files…';
      try {
        const res = await fetch(url);
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(errText(body, res));
        }
        const blob = await res.blob();
        const cd = res.headers.get('content-disposition') || '';
        const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
        const name = m ? decodeURIComponent(m[1]) : `${cfg.slug}-${f.key}.zip`;
        const a = el('a', { href: URL.createObjectURL(blob), download: name });
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        status.textContent = `Downloaded ${name} (${(blob.size / 1048576).toFixed(1)} MB). What was left out, if anything, is listed inside.`;
      } catch (e) {
        status.style.color = 'var(--danger, #e06c5a)';
        status.textContent = `Export failed: ${e.message}`;
      } finally { go.disabled = false; sync(); }
    });
    return d;
  }

  function openExport() {
    if (!exporter) exporter = buildExport();
    exporter.show();
  }

  const eb = document.getElementById('explain-card-btn');
  const xb = document.getElementById('export-card-btn');
  if (eb) eb.addEventListener('click', openExplain);
  if (xb) xb.addEventListener('click', openExport);
})();
