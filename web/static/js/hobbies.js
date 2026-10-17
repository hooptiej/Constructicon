/* #686: the hobbies index page (/hobbies) and the Hobbies drawer (_hobbies_drawer.html).
   The cards themselves are server-rendered with the shared card component (_hobby_cards.html ->
   _card.html): the page renders them in place, the drawer fetches the same fragment from
   GET /api/hobbies/html on first open. This file only wires the "+ New hobby" buttons and the
   drawer's open / close / lazy load. Loaded with `defer` by both; safe to load twice. */
(function () {
  if (window.HobbyCards) return;
  window.HobbyCards = {};

  async function createHobby() {
    const name = prompt('New hobby name:');
    if (!name || !name.trim()) return;
    const fd = new FormData();
    fd.append('name', name.trim());
    const res = await fetch('/api/hobbies', { method: 'POST', body: fd });
    if (res.ok) {
      const hobby = await res.json();
      window.location.href = `/hobby/${encodeURIComponent(hobby.slug)}`;
    } else {
      const body = await res.json().catch(() => ({}));
      alert((body.error && body.error.message) || body.detail || 'Failed to create hobby.');
    }
  }

  ['new-hobby-btn', 'hobbies-drawer-new'].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', createHobby);
  });

  // The drawer (only present when base.html includes it: not in ?embed=1 mode).
  const drawer = document.getElementById('hobbies-drawer');
  if (!drawer) return;
  const backdrop = document.getElementById('hobbies-drawer-backdrop');
  const trigger = document.getElementById('hobbies-drawer-trigger');
  const closeBtn = document.getElementById('hobbies-drawer-close');
  const grid = document.getElementById('hobbies-drawer-grid');
  const status = document.getElementById('hobbies-drawer-status');
  let loaded = false;

  async function loadCards() {
    status.textContent = 'Loading…';
    status.hidden = false;
    try {
      const res = await fetch('/api/hobbies/html');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      grid.innerHTML = await res.text();   // autoescaped server-side Jinja, no scripts
      status.hidden = true;
    } catch (err) {
      loaded = false;   // let the next open try again
      status.textContent = `Couldn't load the list (${err.message}). Close and reopen to retry.`;
    }
  }

  function openDrawer() {
    drawer.classList.add('open');
    backdrop.classList.add('open');
    if (!loaded) { loaded = true; loadCards(); }
  }
  function closeDrawer() {
    drawer.classList.remove('open');
    backdrop.classList.remove('open');
  }

  if (trigger) trigger.addEventListener('click', openDrawer);
  closeBtn.addEventListener('click', closeDrawer);
  backdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && drawer.classList.contains('open')) closeDrawer();
  });
})();
