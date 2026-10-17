/* One file grid (#553): the controls around the item cards, the same on every page that lists files
   (home Files panel, /unfiled, an uploader's gallery, a hobby's loose objects).

   The page includes templates/_item_grid.html, which draws the controls and embeds the grid's config
   (web/item_grid.py build()) as a JSON data block (type application/json, id "<id>-config"). This
   file reads every such config on load and mounts it; there is no per-page grid script any more.

   What a grid does, the same everywhere:
     * Server paging (#624): the page embeds the first batch of the default view (All types, All,
       Date: newest) plus per-type counts; every other view and every later page comes from
       GET /api/items/grid?source=<home|unfiled|user:<label>|hobby:<slug>>&type&filed&sort&cursor&limit&rev.
       One ItemCards.feed per view, kept, so going back to a view needs no request.
     * Sort (static/js/item-sort.js names; the server sorts), the type filter (one <select> with counts),
       the All / Unfiled / Filed toggle (where the page asks for it; the choice is remembered).
     * Multi-select and bulk actions (editors+; the config's `bulk` is empty for a viewer): add to
       card, turn into a new card, tag, provenance, mark sensitive, delete. Each is one POST to the
       existing endpoint, one change-log batch, then the Undo bar (static/js/undo-bar.js) and a
       refresh of the grid from the server (the counts come back with the page).
     * Grid / List (#714, every grid): Grid draws the item cards, List one compact row per file
       (ItemCards.row: thumb, name, type, card date, the card it's on or the unfiled lamp, the
       revision badge, the select checkbox). Everything above works the same in both; the choice is
       remembered per grid id (localStorage "constructicon.grid.<id>.layout", default Grid).
   Grid elements are found by id: <id>-grid, -sort, -type, -filed, -empty, -select, -bar, -selcount,
   -clear, -card, -new-card, -tag, -tag-suggestions, -provenance, -provenance-apply, -sensitive,
   -delete, -layout; and <id>-count, a count line the page may place anywhere. A missing element
   just switches that control off (node tests mount it against a small fake DOM).

   ItemGrid.mount(cfg, env) -> the grid. `env` (all optional, for tests): doc, fetch, ItemCards,
   storage, showUndoBar, confirm, prompt, alert, projectTree, projectIndent. */
(function (root) {
  'use strict';

  var instances = [];

  function mount(cfg, env) {
    env = env || {};
    var doc = env.doc || root.document;
    var IC = env.ItemCards || root.ItemCards;
    var doFetch = env.fetch || function (u, o) { return root.fetch(u, o); };
    var storage = env.storage !== undefined ? env.storage : safeStorage();
    var undoBar = env.showUndoBar || function (m, b, d) { if (root.showUndoBar) root.showUndoBar(m, b, d); };
    var ask = env.confirm || function (m) { return root.confirm(m); };
    var askText = env.prompt || function (m) { return root.prompt(m); };
    var warn = env.alert || function (m) { root.alert(m); };
    var esc = IC.esc;

    var meta = cfg.meta || {};
    var batch = meta.batch || 120;
    var bulk = cfg.bulk || [];
    var filters = cfg.filters || [];
    function $(part) { return doc.getElementById(cfg.id + '-' + part); }
    var box = doc.getElementById(cfg.id);
    var gridEl = $('grid');
    var sortEl = $('sort');
    var typeEl = filters.indexOf('type') >= 0 ? $('type') : null;
    var filedEl = filters.indexOf('filed') >= 0 ? $('filed') : null;
    var emptyEl = $('empty');
    var countEl = $('count');

    var counts = meta.counts || {};
    var labels = meta.labels || {};
    var unfiledSlugs = new Set(meta.unfiled_slugs || []);
    var selected = new Set();
    var seedValid = true;
    var feeds = new Map();
    var tab = 'all';
    var filed = 'all';
    var layout = 'grid';
    if (filedEl && storage) {
      try {
        var saved = storage.getItem(cfg.filed_key);
        if (saved === 'unfiled' || saved === 'filed') filed = saved;
      } catch (e) { /* no storage: default All */ }
    }

    // ---- views and feeds ------------------------------------------------------------------
    function types() { return Object.keys(counts).sort(); }
    function viewCount(t, f) {
      var n = 0;
      (t === 'all' ? types() : [t]).forEach(function (type) { n += (counts[type] || {})[f] || 0; });
      return n;
    }
    function grandTotal() { return viewCount('all', 'all'); }
    function url(t, f, sort, cursor) {
      var q = new URLSearchParams({ source: meta.source || 'home', type: t, filed: f, sort: sort, limit: String(batch) });
      if (meta.rev) q.set('rev', meta.rev);
      if (cursor) q.set('cursor', cursor);
      return (cfg.endpoint || '/api/items/grid') + '?' + q.toString();
    }
    function onPage(page) {
      (page.unfiled_slugs || []).forEach(function (s) { unfiledSlugs.add(s); });
      if (page.counts) { counts = page.counts; labels = page.labels || labels; }
    }
    function feedFor(t, f, sort) {
      var key = [t, f, sort].join('|');
      if (feeds.has(key)) return feeds.get(key);
      var seeded = seedValid && t === 'all' && meta.seed && f === meta.seed.filed && sort === meta.seed.sort;
      var feed = IC.feed({
        items: seeded ? (cfg.seed || []).slice() : [],
        cursor: seeded ? (meta.cursor || null) : (viewCount(t, f) || !seedValid ? '' : null),
        total: viewCount(t, f),
        fetchPage: function (cursor) { return IC.getJson(url(t, f, sort, cursor), env.fetch); },
        onPage: onPage,
      });
      feeds.set(key, feed);
      return feed;
    }

    // ---- cards (Grid) or rows (List, #714) ----------------------------------------------------
    function cardHtml(item) {
      var opts = { selected: selected };
      if (cfg.href_from) opts.href = '/object/' + item.slug + '?from=' + encodeURIComponent(cfg.href_from);
      if (cfg.unfiled_lamp) opts.unfiledSlugs = unfiledSlugs;
      if (bulk.length) opts.selectable = 'row';
      return layout === 'list' ? IC.row(item, opts) : IC.html(item, opts);
    }
    var current = null;
    var pager = IC.pager(gridEl, cardHtml, {
      batch: batch,
      moreRole: function () { return layout === 'list' ? 'listitem' : ''; },
      loadMore: function () {
        var feed = current;
        return feed.next().then(function (fresh) { return { items: fresh, total: feed.total, done: feed.done }; });
      },
    });
    var sortName = root.ItemSort ? root.ItemSort.wire(sortEl, function () { render(); }) : function () { return 'date_newest'; };

    function showStatus(html) {
      gridEl.innerHTML = '<div class="cx-status"' + (layout === 'list' ? ' role="listitem"' : '') +
        ' style="grid-column:1/-1;text-align:center;padding:8px 0">' + html + '</div>';
    }

    // ---- the controls ---------------------------------------------------------------------------
    function renderControls() {
      if (typeEl) {
        if (tab !== 'all' && !counts[tab]) tab = 'all';
        typeEl.innerHTML = '<option value="all">' + esc((cfg.labels && cfg.labels.all_types) || 'All types') +
          ' (' + viewCount('all', filed) + ')</option>' +
          types().map(function (t) {
            return '<option value="' + esc(t) + '">' + esc(labels[t] || t) + ' (' + viewCount(t, filed) + ')</option>';
          }).join('');
        typeEl.value = tab;
      }
      if (filedEl) {
        filedEl.querySelectorAll('.filed-seg-btn').forEach(function (btn) {
          var mode = btn.dataset.filed;
          var on = mode === filed;
          btn.classList.toggle('active', on);
          btn.setAttribute('aria-pressed', on ? 'true' : 'false');
          btn.querySelector('.filed-count').textContent = '(' + viewCount(tab, mode) + ')';
        });
      }
      if (countEl) {
        var c = cfg.count || {};
        var all = grandTotal();
        var noun = all === 1 ? (c.one || 'file') : (c.other || 'files');
        var n = viewCount(tab, filed);
        countEl.textContent = (tab === 'all' && filed === 'all')
          ? all + ' ' + noun + (c.suffix || '')
          : n + ' of ' + all + ' ' + noun + ' shown';
      }
    }

    function render() {
      renderControls();
      var feed = feedFor(tab, filed, sortName());
      current = feed;
      if (feed.items.length || feed.done) {
        // a copy: the pager appends pages to its own list. keep=false: back to the first batch
        pager.set(feed.items.slice(), false, feed.total);
        if (emptyEl) emptyEl.style.display = feed.items.length ? 'none' : 'block';
        return Promise.resolve();
      }
      // Not fetched yet: ask for the first page (the cards on screen would be the wrong view).
      showStatus('Loading files…');
      return feed.next().then(function () {
        if (current !== feed) return;
        renderControls();
        pager.set(feed.items.slice(), false, feed.total);
        if (emptyEl) emptyEl.style.display = feed.items.length ? 'none' : 'block';
      }, function (err) {
        if (current !== feed) return;
        showStatus('<button type="button" class="btn-secondary cx-retry-btn">Could not load files: ' + esc(err.message) + ' (click to retry)</button>');
        var retry = gridEl.querySelector('.cx-retry-btn');
        if (retry) retry.addEventListener('click', render);
      });
    }

    // Whatever a bulk action changed (filed, tagged, deleted, an upload) is read back from the server:
    // drop every cached view (and the embedded seed) and fetch the current view's first page again.
    function refresh() {
      feeds.clear();
      seedValid = false;
      return render();
    }

    if (typeEl) typeEl.addEventListener('change', function () { tab = typeEl.value || 'all'; render(); });
    if (filedEl) filedEl.addEventListener('click', function (e) {
      var btn = e.target.closest('.filed-seg-btn');
      if (!btn) return;
      filed = btn.dataset.filed;
      try { if (storage) storage.setItem(cfg.filed_key, filed); } catch (err) { /* ignore */ }
      render();
    });

    // Grid / List (#714). List is a real list: one row per file (ItemCards.row), the grid element
    // becomes role="list" with the cx-item-list class instead of the card grid's classes (anything
    // else in grid_class, e.g. home's files-scroll, stays). The choice is remembered per grid id;
    // storage can be missing or throw (private mode, blocked site data): default Grid.
    var layoutEl = cfg.layouts ? $('layout') : null;
    var LAYOUT_KEY = 'constructicon.grid.' + cfg.id + '.layout';
    function readLayout() {
      try { return (storage && storage.getItem(LAYOUT_KEY)) === 'list' ? 'list' : 'grid'; } catch (e) { return 'grid'; }
    }
    function listClass() {
      var rest = String(cfg.grid_class || '').split(/\s+/).filter(function (c) { return c && c !== 'cx-item-grid' && c !== 'cx-item-grid-roomy'; });
      return ['cx-item-list'].concat(rest).join(' ');
    }
    function applyLayout(next, save) {
      layout = next === 'list' ? 'list' : 'grid';
      gridEl.className = layout === 'list' ? listClass() : (cfg.grid_class || 'cx-item-grid');
      if (layout === 'list') gridEl.setAttribute('role', 'list');
      else if (gridEl.removeAttribute) gridEl.removeAttribute('role');
      if (box) box.classList.toggle('is-list', layout === 'list');
      if (layoutEl) layoutEl.querySelectorAll('.layout-toggle-btn').forEach(function (b) {
        var on = b.dataset.layout === layout;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      if (save) { try { if (storage) storage.setItem(LAYOUT_KEY, layout); } catch (e) { /* layout just won't persist */ } }
    }
    if (layoutEl) {
      applyLayout(readLayout(), false);
      layoutEl.addEventListener('click', function (e) {
        var b = e.target.closest('.layout-toggle-btn');
        if (b && b.dataset.layout !== layout) {
          applyLayout(b.dataset.layout, true);
          // Redraw the same view in the other layout, keeping as many files as were showing
          // (the feed holds every page fetched so far; keep=true redraws max(batch, shown)).
          if (current && (current.items.length || current.done)) pager.set(current.items.slice(), true, current.total);
          else render();
        }
      });
    }

    // ---- selection ------------------------------------------------------------------------------------
    var bar = bulk.length ? $('bar') : null;
    var selectBtn = bulk.length ? $('select') : null;
    function selecting() { return !!(box && box.classList.contains('is-selecting')); }
    function updateBar() {
      if (!bar) return;
      var n = selected.size;
      var sc = $('selcount');
      if (sc) sc.textContent = n + ' selected';
      ['card', 'new-card', 'tag', 'provenance-apply', 'sensitive', 'delete'].forEach(function (part) {
        var el = $(part);
        if (el) el.disabled = !n;
      });
    }
    function clearSelection() {
      selected.clear();
      if (gridEl.querySelectorAll) gridEl.querySelectorAll('.card-select').forEach(function (cb) { cb.checked = false; });
      updateBar();
    }
    function toggle(cb) {
      if (cb.checked) selected.add(cb.dataset.slug); else selected.delete(cb.dataset.slug);
      updateBar();
    }
    if (selectBtn) selectBtn.addEventListener('click', function () {
      var on = !selecting();
      box.classList.toggle('is-selecting', on);
      selectBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (!on) clearSelection();
    });
    var clearBtn = bulk.length ? $('clear') : null;
    if (clearBtn) clearBtn.addEventListener('click', clearSelection);

    // ---- bulk actions -----------------------------------------------------------------------------------
    function plural(n, one, other) { return n + ' ' + (n === 1 ? one : other); }
    function form(slugs, extra) {
      var fd = new FormData();
      slugs.forEach(function (s) { fd.append('slugs', s); });
      Object.keys(extra || {}).forEach(function (k) { fd.append(k, extra[k]); });
      return fd;
    }
    function post(path, fd) {
      return Promise.resolve(doFetch(path, { method: 'POST', body: fd })).then(function (res) {
        return Promise.resolve(res.json ? res.json() : {}).catch(function () { return {}; }).then(function (body) {
          if (res.ok) return body || {};
          throw new Error((body && body.error && body.error.message) || (body && body.detail) || ('HTTP ' + res.status));
        });
      });
    }
    // One bulk action: POST, then the Undo bar, a fresh grid and an empty selection.
    function run(path, extra, message, failure) {
      var slugs = Array.from(selected);
      if (!slugs.length) return Promise.resolve(null);
      return post(path, form(slugs, extra)).then(function (body) {
        clearSelection();
        undoBar(message(body, slugs.length), body.batch_id || null, function (how) { if (how === 'undone') refresh(); });
        return refresh().then(function () { return body; });
      }, function (err) {
        warn(failure + ': ' + err.message);
        return null;
      });
    }
    var files = function (n) { return plural(n, 'file', 'files'); };

    var cardEl = bulk.indexOf('add_to_card') >= 0 ? $('card') : null;
    if (cardEl) {
      IC.getJson('/api/projects', env.fetch).then(function (projects) {
        var tree = (env.projectTree || root.projectTree || function (ps) { return ps.map(function (p) { return Object.assign({ depth: 0 }, p); }); })(projects);
        var indent = env.projectIndent || root.projectIndent || function () { return ''; };
        cardEl.insertAdjacentHTML('beforeend', tree.map(function (p) {
          return '<option value="' + esc(p.id) + '" data-project>' + esc(indent(p.depth) + p.title) + '</option>';
        }).join(''));
      }, function (err) { cardEl.title = 'Could not load the cards: ' + err.message; });
      cardEl.addEventListener('change', function () {
        var id = cardEl.value;
        cardEl.value = '';
        if (!id) return;
        run('/api/bulk/add-to-project', { project_id: id },
          function (b, n) { return 'Added ' + files(b.count == null ? n : b.count) + ' to the card.'; }, 'Could not add to the card');
      });
    }

    var newCardEl = bulk.indexOf('new_card') >= 0 ? $('new-card') : null;
    if (newCardEl) newCardEl.addEventListener('click', function () {
      if (!selected.size) return;
      var title = askText((cfg.labels && cfg.labels.new_card_prompt) || 'New card name:');
      if (!title || !title.trim()) return;
      run('/api/projects/from-selection', { title: title.trim() },
        function (b, n) { return 'Made the card "' + (b.title || title.trim()) + '" from ' + files(n) + '.'; }, 'Could not make the card');
    });

    var tagEl = bulk.indexOf('tag') >= 0 ? $('tag') : null;
    if (tagEl) {
      var sugEl = $('tag-suggestions');
      var allTags = [];
      var matches = [];
      var active = -1;
      IC.getJson('/api/tags', env.fetch).then(function (data) { allTags = data || []; }, function () { /* suggestions only */ });
      var hide = function () { if (!sugEl) return; sugEl.style.display = 'none'; sugEl.innerHTML = ''; matches = []; active = -1; };
      var apply = function (value) {
        var name = String(value || '').trim();
        if (!name || !selected.size) return Promise.resolve(null);
        tagEl.value = '';
        hide();
        return run('/api/bulk/attach-tags', { tag_names: name },
          function (b) { return 'Tagged ' + files(b.count || 0) + ' "' + name + '".'; }, 'Could not apply the tag');
      };
      var show = function () {
        if (!sugEl) return;
        sugEl.innerHTML = matches.map(function (t, i) {
          return '<div class="tag-suggestion-item' + (i === active ? ' active' : '') + '" data-name="' + esc(t.name) + '">' + esc(t.path) + '</div>';
        }).join('') || '<div class="tag-suggestion-empty">No matching tags</div>';
        sugEl.style.display = 'block';
      };
      if (sugEl) sugEl.addEventListener('mousedown', function (e) {
        var it = e.target.closest('.tag-suggestion-item');
        if (it) { e.preventDefault(); apply(it.dataset.name); }
      });
      tagEl.addEventListener('input', function () {
        var v = tagEl.value.trim().toLowerCase();
        if (!v) { hide(); return; }
        matches = allTags.filter(function (t) { return t.name.toLowerCase().indexOf(v) >= 0 || String(t.path).toLowerCase().indexOf(v) >= 0; }).slice(0, 10);
        active = -1;
        show();
      });
      tagEl.addEventListener('blur', hide);
      tagEl.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown' && matches.length) { e.preventDefault(); active = Math.min(active + 1, matches.length - 1); show(); }
        else if (e.key === 'ArrowUp' && matches.length) { e.preventDefault(); active = Math.max(active - 1, 0); show(); }
        else if (e.key === 'Enter' && tagEl.value.trim()) {
          e.preventDefault();
          apply(active >= 0 && matches[active] ? matches[active].name : tagEl.value);
        } else if (e.key === 'Escape') hide();
      });
    }

    var provEl = bulk.indexOf('provenance') >= 0 ? $('provenance') : null;
    var provBtn = provEl ? $('provenance-apply') : null;
    if (provEl && provBtn) provBtn.addEventListener('click', function () {
      var key = provEl.value;
      if (!key || !selected.size) return;
      run('/api/bulk/provenance', { provenance: key },
        function (b) { return 'Set the provenance of ' + files(b.count || 0) + '.'; }, 'Could not set the provenance')
        .then(function (b) { if (b) provEl.value = ''; });
    });

    var sensEl = bulk.indexOf('sensitive') >= 0 ? $('sensitive') : null;
    if (sensEl) sensEl.addEventListener('click', function () {
      var n = selected.size;
      if (!n) return;
      if (!ask('Mark ' + plural(n, 'selected item', 'selected items') + ' sensitive? Only each item\'s uploader and admins will see them. Only an admin can clear it.')) return;
      run('/api/bulk/sensitive', { sensitive: 'true' },
        function (b) { return 'Marked ' + plural(b.count || 0, 'item', 'items') + ' sensitive.'; }, 'Mark sensitive failed');
    });

    var delEl = bulk.indexOf('delete') >= 0 ? $('delete') : null;
    if (delEl) delEl.addEventListener('click', function () {
      var n = selected.size;
      if (!n) return;
      if (!ask('Delete ' + plural(n, 'selected item', 'selected items') + '? They move to trash for 7 days, and you can undo until then.')) return;
      run('/api/delete', {},
        function (b) { return 'Deleted ' + plural(b.deleted || 0, 'item', 'items') + '. In the trash for 7 days.'; }, 'Delete failed');
    });

    if (doc.addEventListener) doc.addEventListener('constructicon:upload-complete', function (e) {
      if (!(e.detail && e.detail.failures && e.detail.failures.length)) refresh();
    });

    var grid = {
      id: cfg.id, box: box, render: render, refresh: refresh, toggle: toggle, selected: selected,
      get tab() { return tab; }, get filed() { return filed; }, get counts() { return counts; },
      get layout() { return layout; },
    };
    instances.push(grid);
    updateBar();
    render();
    return grid;
  }

  function safeStorage() {
    try { return root.localStorage || null; } catch (e) { return null; }
  }

  // cards.js's one delegated listener calls window.toggleCardSelect(cb) (#645: no inline handlers);
  // hand the checkbox to the grid that holds it.
  root.toggleCardSelect = function (cb) {
    for (var i = 0; i < instances.length; i++) {
      var g = instances[i];
      if (!g.box || !g.box.contains || g.box.contains(cb)) { g.toggle(cb); return; }
    }
  };

  function mountAll(doc) {
    doc = doc || root.document;
    if (!doc || !doc.querySelectorAll) return [];
    var out = [];
    doc.querySelectorAll('[data-item-grid]').forEach(function (box) {
      if (box.dataset.itemGridMounted) return;
      var src = doc.getElementById(box.dataset.itemGrid + '-config');
      if (!src) return;
      box.dataset.itemGridMounted = '1';
      out.push(mount(JSON.parse(src.textContent)));
    });
    return out;
  }

  var api = { mount: mount, mountAll: mountAll, instances: instances };
  root.ItemGrid = api;
  if (typeof module === 'object' && module && module.exports) module.exports = api;
  // The include puts this script right after the grid's markup, so the grid is already in the DOM.
  if (root.document && root.document.querySelectorAll) mountAll(root.document);
})(typeof window !== 'undefined' ? window : this);
