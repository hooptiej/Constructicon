// Shared project-dropdown helper (#413): order a flat project list as a
// parent -> child tree so <select> menus reflect projects.parent_id nesting.
// Loaded globally from base.html; each consumer maps projectTree() into its own
// <option> markup (keeping its own filtering / placeholder / extra options).
(function () {
  // Flat [{id, title, parent_id}] -> same objects ordered parent-then-children
  // (siblings A–Z), each with a `depth` field. Orphans (parent_id not present in
  // the list) are emitted at root depth so nothing is ever dropped.
  window.projectTree = function (projects) {
    const byParent = new Map();
    projects.forEach(p => {
      const k = (p.parent_id == null) ? 'root' : p.parent_id;
      if (!byParent.has(k)) byParent.set(k, []);
      byParent.get(k).push(p);
    });
    byParent.forEach(list => list.sort((a, b) => String(a.title || '').localeCompare(String(b.title || ''))));
    const out = [];
    (function walk(key, depth) {
      (byParent.get(key) || []).forEach(p => { out.push({ ...p, depth }); walk(p.id, depth + 1); });
    })('root', 0);
    const emitted = new Set(out.map(p => p.id));
    projects.forEach(p => { if (!emitted.has(p.id)) out.push({ ...p, depth: 0 }); });
    return out;
  };

  // Indent prefix for a nested <option> label, e.g. "  ↳ ".
  window.projectIndent = function (depth) {
    return depth ? '  '.repeat(depth) + '↳ ' : '';
  };
})();
