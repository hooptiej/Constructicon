/* Item cards, client side (#514). One renderer for every grid of individual items (home
   Files panel, Unfiled, user gallery). It emits exactly the markup the Jinja macro
   templates/_card.html produces for an asset card at mini size, so static/css/cards.css
   styles both identically; scripts/check_item_cards.py compares the two structurally.
   There is no second card design: if the card face changes, change the macro and this file
   together. Everything interpolated goes through esc().

   ItemCards.html(item, opts) -> string
     item  a slim item record (core/card_payload.py: slug, display_name, type_label,
           card_date, thumb_url/has_thumbnail, redacted, codes, stacked, highlight, ...)
     opts  href       link target (default /object/<slug>)
           size       "mini" (default) | "small"
           width      optional --cx-w override in px (the Unfiled grid/list toggle)
           unfiledSlugs  Set of slugs not filed into a project (amber lamp)
           selectable "row" shows the select checkbox row (Unfiled, user gallery)
           selected   Set of selected slugs (re-checks boxes on re-render)
   The card is one real link. Lamps, badges, tags and the select checkbox sit in a tools
   strip directly under it (interactive controls can't live inside the link). */
(function () {
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Same asset glyph as the macro's kind_icon("asset").
  var ICON = '<svg class="cx-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M6 3h8l4 4v14H6V3Z"/><path d="M14 3v4h4"/></svg>';

  var OCR_LAMP_META = {
    pending: { color: '#BA7517', title: 'OCR in progress…' },
    done:    { color: '#7FA37C', title: null },
    failed:  { color: '#E24B4A', title: 'OCR failed' },
    none:    { color: '#E24B4A', title: 'OCR not run yet' }
  };
  var CAPTION_LAMP_META = {
    pending: { color: '#BA7517', title: 'Captioning…' },
    done:    { color: '#7FA37C', title: null },
    failed:  { color: '#E24B4A', title: 'Captioning failed' },
    none:    { color: '#6E6B57', title: 'No caption yet' }
  };
  var CLIENT_COLORS = {};
  var PALETTE = ['#B98B5E', '#8B7355', '#C9A66B', '#7FA37C', '#9CAD5E'];
  function colorFor(client) {
    if (!client) return '#6E6B57';
    if (!CLIENT_COLORS[client]) CLIENT_COLORS[client] = PALETTE[Object.keys(CLIENT_COLORS).length % PALETTE.length];
    return CLIENT_COLORS[client];
  }

  function lamp(color, title) {
    return '<span class="ocr-lamp" style="background:' + esc(color) + '" title="' + esc(title) + '"></span>';
  }
  function ocrLamp(item) {
    if (item.ocr_status == null) return '';
    var meta = OCR_LAMP_META[item.ocr_status || 'none'] || OCR_LAMP_META.none;
    var title = item.ocr_status === 'done' ? (item.extracted_text || 'OCR found no text') : meta.title;
    return lamp(meta.color, title);
  }
  function captionLamp(item) {
    if (!item.caption_capable) return '';
    var tm = item.type_metadata || {};
    var status = tm.auto_caption_status || 'none';
    var meta = CAPTION_LAMP_META[status] || CAPTION_LAMP_META.none;
    var title = status === 'done' ? (tm.auto_caption || 'Caption came back empty') : meta.title;
    return lamp(meta.color, title);
  }

  // The card itself: same classes, same nesting, same text slots as _card.html drawing
  // core.cards.file_face() (#596): status box "Stacked · <card>", footer "<code> · <card>", no
  // provenance. The grids carry no file text, so the text box stays empty (mini hides it anyway).
  function face(item, opts) {
    var size = opts.size === 'small' ? 'small' : 'mini';
    var href = opts.href || ('/object/' + item.slug);
    var title = item.display_name || item.slug;
    var typeLine = item.type_label || item.media_type || 'File';
    var stacked = item.stacked || '';
    var code = (item.codes || [])[0] || '';
    var zone = stacked ? 'Stacked · ' + stacked : '';
    var foot = [code, stacked].filter(Boolean).join(' · ');
    var codes = (item.codes || []).map(function (c) { return '<span class="cx-code">' + esc(c) + '</span>'; }).join('');
    var hl = item.highlight ? '<span class="cx-hl on" role="img" aria-label="Highlighted"></span>' : '<span class="cx-hl" aria-hidden="true"></span>';
    var art;
    if (item.has_thumbnail && item.thumb_url && !item.redacted) {
      var deg = item.type_metadata && item.type_metadata.rotation;
      var rot = deg ? ' style="transform:rotate(' + esc(parseInt(deg, 10) || 0) + 'deg)"' : '';
      art = '<img src="' + esc(item.thumb_url) + '" loading="lazy" alt="Cover image for ' + esc(title) + '"' + rot + '>';
    } else {
      art = '<span class="cx-art-empty">' + ICON + '<span>' + (item.redacted ? 'File removed' : 'No cover yet') + '</span></span>';
    }
    return '<a class="cx cx-' + size + ' cx-kind-asset" href="' + esc(href) + '" data-card-kind="asset" data-card-slug="' + esc(item.slug) + '">' +
      '<span class="cx-namebar"><span class="cx-name">' + esc(title) + '</span>' +
        '<span class="cx-kind" title="' + esc(typeLine) + '">' + ICON + '<span class="cx-sr">File: </span></span></span>' +
      '<span class="cx-dates-row"><span class="cx-dates">' + esc(item.card_date || '') + '</span></span>' +
      '<span class="cx-art">' + art + '</span>' +
      '<span class="cx-typeline"><span class="cx-type">' + esc(typeLine) + '</span>' +
        '<span class="cx-codes">' + codes + hl + '</span></span>' +
      '<span class="cx-body"></span>' +
      '<span class="cx-boxes">' + (zone ? '<span class="cx-box cx-status">' + esc(zone) + '</span>' : '') + '</span>' +
      '<span class="cx-foot">' + (size === 'mini' ? '' : '<span class="cx-num">' + esc(foot) + '</span>') + '</span>' +
    '</a>';
  }

  // Everything the old gallery tiles carried that the card face has no slot for.
  function tools(item, opts) {
    var parts = [];
    var lamps = '';
    if (opts.unfiledSlugs && opts.unfiledSlugs.has(item.slug)) lamps += lamp('#BA7517', 'Not filed into a project yet');
    lamps += ocrLamp(item) + captionLamp(item);
    if (lamps) parts.push('<span class="cx-item-lamps">' + lamps + '</span>');
    if (item.redacted) parts.push('<span class="cx-item-badge cx-item-redacted-badge" title="The file was removed; the info is kept">Redacted</span>');
    // #477: revision chain. A superseded file says so (and points at the current one); the current
    // revision of a chain shows its position, linking to the page's revision stack.
    if (item.superseded_by) parts.push('<a class="cx-item-badge cx-rev-badge cx-rev-old" href="/object/' + esc(item.superseded_by) + '" title="A newer revision replaces this file. Open the current revision.">Superseded</a>');
    else if (item.rev) parts.push('<a class="cx-item-badge cx-rev-badge" href="/object/' + esc(item.slug) + '#revisions" title="Revision ' + esc(item.rev) + ' (the current one). Older revisions are on its page.">rev ' + esc(item.rev) + '</a>');
    if (item.type_icon) parts.push('<span class="cx-item-badge" title="' + esc(item.type_label || item.type_badge || '') + '">' + esc(item.type_icon) + '</span>');
    // #563: the per-card client badge is gone (keyed on the dead `client` field, so it showed the
    // same empty-state text on every card); the amber unfiled lamp above is the real signal.
    var by = item.uploaded_by_display;
    if (by) parts.push('<span class="uploader-label" title="Uploaded by">' + esc(by) + '</span>');
    var tags = item.tags || [];
    if (tags.length) {
      var shown = tags.slice(0, 3).map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('');
      var more = tags.length > 3 ? '<span class="chip" title="' + esc(tags.slice(3).join(', ')) + '">+' + (tags.length - 3) + '</span>' : '';
      parts.push('<span class="cx-item-tags">' + shown + more + '</span>');
    }
    if (opts.selectable === 'row') {
      parts.push('<label class="card-select-row"><span class="card-select-label">Select</span>' +
        '<input type="checkbox" class="card-select" data-slug="' + esc(item.slug) + '" title="Select for bulk actions" onchange="toggleCardSelect(this)"' +
        (opts.selected && opts.selected.has(item.slug) ? ' checked' : '') + '></label>');
    }
    return '<div class="cx-item-tools">' + parts.join('') + '</div>';
  }

  function html(item, opts) {
    opts = opts || {};
    var cls = 'cx-item' + (opts.size === 'small' ? ' cx-item-small' : '') + (item.redacted ? ' cx-item-redacted' : '');
    var w = opts.width ? ' style="--cx-w:' + esc(parseInt(opts.width, 10)) + 'px"' : '';
    return '<div class="' + cls + '" data-slug="' + esc(item.slug) + '"' + w + '>' + face(item, opts) + tools(item, opts) + '</div>';
  }

  /* Incremental rendering (#517). A grid of ~1,700 cards is thousands of DOM nodes and
     image slots; this draws the first `batch` and appends more as the "Show more" row nears
     the viewport (or is clicked). Sorting and filtering stay over the FULL list: callers hand
     pager.set() the whole sorted/filtered array each time.
       var pager = ItemCards.pager(gridEl, cardFn, { batch: 120 });
       pager.set(list)        redraw from the top
       pager.set(list, true)  redraw but keep as many cards as were already showing
     The "Show more" row is a child of the grid (full-width), so it scrolls with the cards
     even when the grid itself is the scroll container (home Files panel). */
  function pager(grid, cardFn, opts) {
    var batch = (opts && opts.batch) || 120;
    var list = [];
    var shown = 0;
    var io = null;

    function moreRow() {
      var left = list.length - shown;
      return '<div class="cx-more" style="grid-column:1/-1;text-align:center;padding:8px 0">' +
        '<button type="button" class="btn-secondary cx-more-btn">Show ' + Math.min(batch, left) +
        ' more (' + left + ' left)</button></div>';
    }
    function watch() {
      if (io) { io.disconnect(); io = null; }
      var row = grid.querySelector('.cx-more');
      if (!row) return;
      row.querySelector('.cx-more-btn').addEventListener('click', more);
      if (typeof IntersectionObserver === 'function') {
        io = new IntersectionObserver(function (entries) {
          if (entries.some(function (e) { return e.isIntersecting; })) more();
        }, { rootMargin: '600px' });
        io.observe(row);
      }
    }
    function draw() {
      grid.innerHTML = list.slice(0, shown).map(cardFn).join('') + (shown < list.length ? moreRow() : '');
      watch();
    }
    function more() {
      var row = grid.querySelector('.cx-more');
      if (!row) return;
      var from = shown;
      shown = Math.min(list.length, shown + batch);
      row.remove();
      grid.insertAdjacentHTML('beforeend', list.slice(from, shown).map(cardFn).join('') + (shown < list.length ? moreRow() : ''));
      watch();
    }
    return {
      set: function (next, keep) {
        list = next;
        shown = Math.min(list.length, keep ? Math.max(batch, shown) : batch);
        draw();
      },
      get shown() { return shown; }
    };
  }

  window.ItemCards = { html: html, face: face, esc: esc, pager: pager };
})();
