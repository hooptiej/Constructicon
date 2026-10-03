/* Project detail stacks (docs/design/v2-cards.md 8.3).
   Progressive: without this script the piles are inert and the full file grid is still one
   click away (the <details id="all-files"> summary). With it:
     - a pile is a real <button>; Enter/Space/click fans it out (aria-expanded tracks it);
     - clicking the open pile again, or "Show all N", opens the full grid filtered to that type;
     - Esc or a click outside collapses the fan.
   All file actions (open, hotlink, remove from project...) live in the full grid, untouched. */
(function () {
  'use strict';
  var section = document.getElementById('stacks');
  if (!section) return;
  var piles = Array.prototype.slice.call(section.querySelectorAll('.stack-pile'));
  var details = document.getElementById('all-files');
  var filterNote = document.getElementById('all-files-filter');
  var clearBtn = document.getElementById('all-files-clear');

  function setOpen(pile, open) {
    var btn = pile.querySelector('.stack-pile-btn');
    var fan = pile.querySelector('.stack-fan');
    if (!btn || !fan) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    fan.hidden = !open;
    pile.classList.toggle('is-open', open);
  }

  function closeAll(except) {
    piles.forEach(function (p) { if (p !== except) setOpen(p, false); });
  }

  function filterGrid(mediaType) {
    if (!details) return;
    var wrappers = details.querySelectorAll('.project-item-card-wrapper');
    Array.prototype.forEach.call(wrappers, function (w) {
      var hide = mediaType && w.getAttribute('data-media-type') !== mediaType;
      w.classList.toggle('is-filtered-out', !!hide);
    });
    if (filterNote) filterNote.hidden = !mediaType;
  }

  function showAll(mediaType) {
    if (!details) return;
    filterGrid(mediaType || '');
    details.open = true;
    details.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
    var sum = details.querySelector('summary');
    if (sum) sum.focus({ preventScroll: true });
  }

  piles.forEach(function (pile) {
    var btn = pile.querySelector('.stack-pile-btn');
    if (btn) {
      btn.addEventListener('click', function () {
        var open = btn.getAttribute('aria-expanded') === 'true';
        if (open) { showAll(pile.getAttribute('data-media-type')); return; }
        closeAll(pile);
        setOpen(pile, true);
      });
    }
    var all = pile.querySelector('.stack-showall');
    if (all) all.addEventListener('click', function () { showAll(all.getAttribute('data-media-type')); });
  });

  if (clearBtn) clearBtn.addEventListener('click', function () { filterGrid(''); });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var openPile = section.querySelector('.stack-pile.is-open');
    if (!openPile) return;
    setOpen(openPile, false);
    var btn = openPile.querySelector('.stack-pile-btn');
    if (btn) btn.focus();
  });

  document.addEventListener('click', function (e) {
    if (!section.contains(e.target)) closeAll(null);
  });
})();
