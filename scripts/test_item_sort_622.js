// The shared item-grid Sort (#622): web/static/js/item-sort.js. No browser, no dependencies.
//   node scripts/test_item_sort_622.js
// Covers the comparators (card date, upload time, A-Z; ties; items with no card date), sorted() and
// wire(), that the module loads both as a browser <script> (window.ItemSort) and from node (require),
// that the server's sort registry (web/files_feed.py SORTS), this module and the shared <option> list
// (templates/_item_sort_options.html) name the same sorts in the same order, and that each of the four
// grid templates loads the module before the inline script that uses it and keeps no sorter of its own.
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

let fails = 0;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (!cond && extra !== undefined ? '  [' + JSON.stringify(extra) + ']' : ''));
  if (!cond) fails++;
}

const ItemSort = require(path.join(ROOT, 'web/static/js/item-sort.js'));
const slugs = (list) => list.map((x) => x.slug).join(',');

// ---- loading -------------------------------------------------------------------------------------
{
  const ctx = vm.createContext({ window: {}, console });
  vm.runInContext(read('web/static/js/item-sort.js'), ctx);
  check('as a browser script it sets window.ItemSort', ctx.window.ItemSort && typeof ctx.window.ItemSort.sorted === 'function');
  check('from node, require() returns the same API', typeof ItemSort.sorted === 'function' && ItemSort.DEFAULT === 'date_newest');
  check('the sorts, in menu order', ItemSort.NAMES.join() === 'date_newest,date_oldest,newest,oldest,az', ItemSort.NAMES);
}

// ---- the comparators -------------------------------------------------------------------------------
// card_date_epoch is the date the card shows; uploaded_at is the upload time. Upload order differs on purpose.
const DAY = 86400;
const items = [
  { slug: 'content', display_name: 'Desk build', card_date_epoch: 1000 * DAY, uploaded_at: 5000 * DAY },
  { slug: 'override', display_name: 'apple', card_date_epoch: 200 * DAY, uploaded_at: 5001 * DAY },
  { slug: 'none', display_name: 'Zed', card_date_epoch: 4000 * DAY, uploaded_at: 4000 * DAY },  // no content date: card shows the upload
  { slug: 'tieA', display_name: 'Étude', card_date_epoch: 700 * DAY, uploaded_at: 4500 * DAY },
  { slug: 'tieB', display_name: 'etude', card_date_epoch: 700 * DAY, uploaded_at: 4600 * DAY },
  { slug: 'tieC1', display_name: 'beta', card_date_epoch: 300 * DAY, uploaded_at: 4100 * DAY },
  { slug: 'tieC2', display_name: 'Beta', card_date_epoch: 300 * DAY, uploaded_at: 4100 * DAY },  // full tie with tieC1
  { slug: 'oldpayload', display_name: '10th', uploaded_at: 3000 * DAY },  // no card_date_epoch at all
];
const before = slugs(items);
check('date_newest: by the card date, newest first; same date -> later upload first; full tie keeps list order',
  slugs(ItemSort.sorted(items, 'date_newest')) === 'none,oldpayload,content,tieB,tieA,tieC1,tieC2,override',
  slugs(ItemSort.sorted(items, 'date_newest')));
check('date_oldest: by the card date, oldest first; same date -> earlier upload first; full tie keeps list order',
  slugs(ItemSort.sorted(items, 'date_oldest')) === 'override,tieC1,tieC2,tieA,tieB,content,oldpayload,none',
  slugs(ItemSort.sorted(items, 'date_oldest')));
check('an item with no card_date_epoch sorts by its upload time (where its card date would be)',
  ItemSort.sorted(items, 'date_newest').findIndex((x) => x.slug === 'oldpayload') === 1);
check('newest: upload time, newest first (the pre-#622 Newest)',
  slugs(ItemSort.sorted(items, 'newest')) === 'override,content,tieB,tieA,tieC1,tieC2,none,oldpayload', slugs(ItemSort.sorted(items, 'newest')));
check('oldest: upload time, oldest first',
  slugs(ItemSort.sorted(items, 'oldest')) === 'oldpayload,none,tieC1,tieC2,tieA,tieB,content,override', slugs(ItemSort.sorted(items, 'oldest')));
const az = ItemSort.sorted(items, 'az').map((x) => x.display_name);
check('az: String.localeCompare on the title (case and accents second)',
  az.join('|') === items.map((x) => x.display_name).sort((a, b) => a.localeCompare(b)).join('|') && az.indexOf('apple') < az.indexOf('Beta'), az);
check('az: a missing title sorts as empty, no crash',
  ItemSort.sorted([{ slug: 'b', display_name: 'b' }, { slug: 'x' }], 'az')[0].slug === 'x');
check('sorted() returns a copy: the caller\'s list is untouched', slugs(items) === before);
check('an unknown sort name sorts by the default (date_newest)', slugs(ItemSort.sorted(items, 'bogus')) === slugs(ItemSort.sorted(items, 'date_newest')));
check('sorted() of nothing is an empty list', Array.isArray(ItemSort.sorted(null, 'az')) && ItemSort.sorted(null, 'az').length === 0);
check('a non-number card_date_epoch (null) falls back to the upload time',
  slugs(ItemSort.sorted([{ slug: 'a', card_date_epoch: null, uploaded_at: 5 }, { slug: 'b', card_date_epoch: 3, uploaded_at: 9 }], 'date_newest')) === 'a,b');

// ---- wire() ---------------------------------------------------------------------------------------
{
  const mkSelect = (value) => ({ value, listeners: {}, addEventListener(t, f) { this.listeners[t] = f; } });
  let sel = mkSelect('');
  const seen = [];
  const get = ItemSort.wire(sel, (name) => seen.push(name));
  check('wire: an empty select starts on the default', sel.value === 'date_newest' && get() === 'date_newest');
  sel.value = 'az'; sel.listeners.change();
  check('wire: a change calls back with the chosen sort', seen.join() === 'az' && get() === 'az');
  sel.value = 'nope';
  check('wire: an unknown value reads as the default', get() === 'date_newest');
  sel = mkSelect('oldest');
  check('wire: a valid value is kept', ItemSort.wire(sel)() === 'oldest' && sel.value === 'oldest');
  check('wire: no select (a one-item grid) reads as the default', ItemSort.wire(null, () => {})() === 'date_newest');
}

// ---- one list of sorts: server, module, menu --------------------------------------------------------------
{
  const py = read('web/files_feed.py');
  const block = py.slice(py.indexOf('SORTS = {'), py.indexOf('}', py.indexOf('SORTS = {')));
  const pyNames = [...block.matchAll(/^\s+"(\w+)": \(/gm)].map((m) => m[1]);
  const optNames = [...read('web/templates/_item_sort_options.html').matchAll(/<option value="(\w+)"/g)].map((m) => m[1]);
  check('files_feed.SORTS == ItemSort.NAMES == the shared <option> list, same order',
    pyNames.join() === ItemSort.NAMES.join() && optNames.join() === ItemSort.NAMES.join(), { pyNames, optNames });
  check('the server default is the module default', new RegExp('DEFAULT_SORT = "' + ItemSort.DEFAULT + '"').test(py));
  check('the menu preselects the default', /<option value="date_newest" selected>/.test(read('web/templates/_item_sort_options.html')));
}

// ---- the four grids use it ------------------------------------------------------------------------------------
for (const tpl of ['home.html', 'unfiled.html', 'user_gallery.html', 'hobby.html']) {
  const src = read('web/templates/' + tpl);
  const tag = src.indexOf('<script src="/static/js/item-sort.js?v={{ static_version(\'js/item-sort.js\') }}"></script>');
  const use = src.indexOf('ItemSort.wire(');
  check(tpl + ': loads item-sort.js (cache-busted) before the inline script that uses it', tag >= 0 && use > tag, { tag, use });
  check(tpl + ': the Sort menu is the shared include', src.includes('{% include "_item_sort_options.html" %}'));
  check(tpl + ': no sorter of its own (no uploaded_at comparator, no FILES_/GALLERY_/LOOSE_ sorter table)',
    !/uploaded_at\s*-\s*\w+\.uploaded_at/.test(src) && !/(FILES_SORTERS|GALLERY_SORTERS|LOOSE_SORT)\b/.test(src));
}
for (const tpl of ['unfiled.html', 'user_gallery.html', 'hobby.html']) {
  check(tpl + ': sorts its whole list with ItemSort.sorted', read('web/templates/' + tpl).includes('ItemSort.sorted('));
}

console.log('\n' + fails + ' failure(s)');
if (fails) { console.log('FAILED: ' + fails + ' check(s) above'); process.exit(1); }
console.log('ALL PASS');
