// The one Sort for the item grids (#622): home's Files panel, /unfiled, the user gallery and the
// hobby page's loose objects. Each used to carry its own copy of the same three comparators.
//
// Sorts (same names, same order as web/files_feed.py SORTS and templates/_item_sort_options.html):
//   date_newest  Date: newest    the date the card shows (card_date_epoch), newest first. DEFAULT.
//   date_oldest  Date: oldest    the same, oldest first.
//   newest       Uploaded: newest   upload time (uploaded_at), the pre-#622 "Newest".
//   oldest       Uploaded: oldest   upload time, oldest first.
//   az           A-Z            by title (display_name), String.localeCompare.
//
// card_date_epoch comes from the server (web/shapes.py _to_public, core/timeline.py resolve_item_date),
// the same value the card's date label is made from, so the order and the dates on the cards can't
// disagree. An item with no content date is not set apart: the resolver gives it its file's own
// modified time, else its upload time, and that is also the date its card shows, so it sorts where its
// card's date says. Ties (same card date) fall back to the upload time in the same direction, then keep
// the list's own order (Array.prototype.sort is stable), the same rule the server uses for home.
//
// The grids that hold their whole list sort it here (ItemSort.sorted); home's paged panel passes the
// sort name to GET /api/home/files and the server sorts with the same keys. ItemSort.wire() sets up a
// grid's <select class="filter-select"> either way.
(function (root) {
  'use strict';

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
  function uploaded(it) { const u = num(it.uploaded_at); return u === null ? 0 : u; }
  // Defensive: an item without card_date_epoch (an older cached payload) uses its upload time.
  function cardDate(it) { const d = num(it.card_date_epoch); return d === null ? uploaded(it) : d; }
  function title(it) { return String(it.display_name || ''); }

  const SORTS = {
    date_newest: (a, b) => (cardDate(b) - cardDate(a)) || (uploaded(b) - uploaded(a)),
    date_oldest: (a, b) => (cardDate(a) - cardDate(b)) || (uploaded(a) - uploaded(b)),
    newest: (a, b) => uploaded(b) - uploaded(a),
    oldest: (a, b) => uploaded(a) - uploaded(b),
    az: (a, b) => title(a).localeCompare(title(b)),
  };
  const NAMES = Object.keys(SORTS);
  const DEFAULT = 'date_newest';

  function has(name) { return Object.prototype.hasOwnProperty.call(SORTS, name); }
  function comparator(name) { return has(name) ? SORTS[name] : SORTS[DEFAULT]; }
  // A sorted COPY; the caller's list is untouched. An unknown name sorts by the default.
  function sorted(items, name) { return (items || []).slice().sort(comparator(name)); }

  // Wire a grid's Sort <select>: an unknown or empty value becomes the default, a change calls
  // onChange(name). Returns a getter for the current sort name. A missing select (a grid with one
  // item renders none) reads as the default.
  function wire(select, onChange) {
    if (select && !has(select.value)) select.value = DEFAULT;
    if (select && onChange) select.addEventListener('change', () => onChange(current()));
    function current() { return select && has(select.value) ? select.value : DEFAULT; }
    return current;
  }

  const api = { SORTS: SORTS, NAMES: NAMES, DEFAULT: DEFAULT, has: has, comparator: comparator, sorted: sorted, wire: wire };
  root.ItemSort = api;
  if (typeof module === 'object' && module && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
