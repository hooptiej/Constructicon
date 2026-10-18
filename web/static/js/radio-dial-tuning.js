// Dev-only live tuning panel for the home page radio dial (#775). Loaded only when home.html decides
// tuning is allowed (CONSTRUCTICON_ENV=dev, or ?tune=1 for an admin), so prod viewers never get it.
// One slider per RadioDial.TUNABLES entry, applied live through dial.setTuning. Values are remembered
// in this browser only (localStorage, best effort) and "Copy values" puts them on the clipboard as
// JSON, so the owner can hand the numbers back to bake into DIAL_DEFAULTS in radio-dial.js.
(function () {
  const STORE_KEY = 'constructicon.radioDial.tuning';

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (e) { return {}; }
  }
  function save(values) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(values)); } catch (e) { /* private window: not remembered */ }
  }
  function clear() {
    try { localStorage.removeItem(STORE_KEY); } catch (e) { /* nothing stored */ }
  }

  // panel: the empty #radio-dial-tune element; dial: the RadioDial instance.
  function mount(panel, dial) {
    const defaults = window.RadioDial.DEFAULTS;
    const saved = load();
    dial.setTuning(saved);
    panel.innerHTML = `
      <details class="radio-dial-tune" open>
        <summary>Dial tuning (dev only)</summary>
        <div class="radio-dial-tune-rows"></div>
        <div class="radio-dial-tune-actions">
          <button type="button" class="btn-secondary" data-act="copy">Copy values</button>
          <button type="button" class="btn-secondary" data-act="reset">Reset to defaults</button>
          <span class="radio-dial-tune-status" role="status"></span>
        </div>
      </details>`;
    const rows = panel.querySelector('.radio-dial-tune-rows');
    const status = panel.querySelector('.radio-dial-tune-status');
    const inputs = {};
    window.RadioDial.TUNABLES.forEach((t) => {
      const row = document.createElement('label');
      row.className = 'radio-dial-tune-row';
      const name = document.createElement('span');
      name.textContent = t.label;
      const input = document.createElement('input');
      Object.assign(input, { type: 'range', min: t.min, max: t.max, step: t.step });
      input.value = String(dial.tuning[t.key]);
      const out = document.createElement('output');
      out.textContent = input.value;
      input.addEventListener('input', () => {
        out.textContent = input.value;
        dial.setTuning({ [t.key]: input.value });
        save(dial.tuning);
      });
      row.append(name, input, out);
      rows.appendChild(row);
      inputs[t.key] = { input, out };
    });
    panel.querySelector('[data-act="reset"]').addEventListener('click', () => {
      clear();
      dial.setTuning(defaults);
      Object.keys(inputs).forEach((k) => { inputs[k].input.value = String(defaults[k]); inputs[k].out.textContent = String(defaults[k]); });
      status.textContent = 'Back to the built-in defaults.';
    });
    panel.querySelector('[data-act="copy"]').addEventListener('click', async () => {
      const text = JSON.stringify(dial.tuning, null, 2);
      try {
        await navigator.clipboard.writeText(text);
        status.textContent = 'Copied to the clipboard.';
      } catch (e) {
        // No clipboard access (plain-HTTP origin): show the JSON so it can be copied by hand.
        status.textContent = `Clipboard unavailable here; values: ${JSON.stringify(dial.tuning)}`;
      }
    });
  }

  window.RadioDialTuning = { mount };
})();
