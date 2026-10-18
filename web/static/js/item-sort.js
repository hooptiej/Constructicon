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
// card_date_epoch comes from the server (web/shapes.py _to_public, core/item_dates.py), the same value
// the card's date label is made from, so the order and the dates on the cards can't disagree. #725: it
// is the item's REAL date, else the start of the range it inherits from its card ("≈ ... · from
// <card>"), else null: UNDATED, and an undated item goes LAST in both Date sorts (by upload time among
// themselves). The upload time is never an item's date. Ties (same card date) fall back to the upload
// time in the same direction, then keep the list's own order (Array.prototype.sort is stable), the same
// rule the server uses (web/files_feed.py SORTS).
//
// The grids that hold their whole list sort it here (ItemSort.sorted); home's paged panel passes the
// sort name to GET /api/home/files and the server sorts with the same keys. ItemSort.wire() sets up a
// grid's <select class="filter-select"> either way.
(function (root) {
  'use strict';

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
  function uploaded(it) { const u = num(it.uploaded_at); return u === null ? 0 : u; }
  // #725: null (or any non-number) = undated. Defensive: an item with NO card_date_epoch key at all
  // (a payload cached from before #622) still sorts by its upload time.
  function cardDate(it) {
    if (!Object.prototype.hasOwnProperty.call(it, 'card_date_epoch')) return uploaded(it);
    return num(it.card_date_epoch);
  }
  // Undated last in either direction, then `cmp` on the dates, then the upload time (`dir` 1 = oldest first).
  function byDate(dir) {
    return (a, b) => {
      const da = cardDate(a), db = cardDate(b);
      if (da === null || db === null) {
        if (da !== db) return da === null ? 1 : -1;
        return dir * (uploaded(a) - uploaded(b));
      }
      return dir * (da - db) || dir * (uploaded(a) - uploaded(b));
    };
  }
  function title(it) { return String(it.display_name || ''); }

  const SORTS = {
    date_newest: byDate(-1),
    date_oldest: byDate(1),
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
