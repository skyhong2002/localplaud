// Voice-similarity name suggestions in the Name speakers dialog.
// Suggestions never apply themselves: "Use" only fills the field, and the
// user's Save goes through the normal batch naming path (workspace.js).
(() => {
  const dialog = document.getElementById('name-speakers-backdrop');
  const hint = document.getElementById('speaker-suggestion-hint');
  if (!dialog) return;
  const tr = window.localplaudT || window.lp?.t || (message => message);
  const form = document.getElementById('name-speakers-form');
  const status = document.getElementById('name-speakers-status');
  const fieldFor = key => form?.querySelector(`input[name="${CSS.escape(key)}"]`);
  const rows = () => Array.from(dialog.querySelectorAll('[data-suggestion-for]'));

  function refresh() {
    let open = 0;
    for (const row of rows()) {
      const field = fieldFor(row.dataset.suggestionFor);
      const named = row.dataset.named === 'true';
      const used = Boolean(field) && field.value.trim() === row.dataset.suggestionName;
      row.hidden = named;
      const button = row.querySelector('[data-use-suggestion]');
      if (button) button.disabled = used;
      if (!named) open += 1;
    }
    const note = dialog.querySelector('[data-suggestion-note]');
    if (note) note.hidden = open === 0;
    if (hint) {
      hint.hidden = open === 0;
      const label = hint.querySelector('[data-suggestion-count]');
      if (label) label.textContent = open === 1 ? tr('1 suggested name') : tr('{count} suggested names').replace('{count}', String(open));
    }
  }

  // Runs after workspace.js has reset the fields from saved names.
  document.getElementById('menu-name-speakers')?.addEventListener('click', () => {
    for (const row of rows()) {
      const field = fieldFor(row.dataset.suggestionFor);
      row.dataset.named = String(Boolean(field?.value.trim()));
    }
    if (status) status.textContent = '';
    refresh();
  });

  let openedFromHint = false;
  hint?.addEventListener('click', () => {
    openedFromHint = true;
    document.getElementById('menu-name-speakers')?.click();
  });
  dialog.addEventListener('close', () => {
    if (openedFromHint && hint && !hint.hidden) requestAnimationFrame(() => hint.focus());
    openedFromHint = false;
  });

  dialog.addEventListener('click', event => {
    const button = event.target.closest('[data-use-suggestion]');
    if (!button) return;
    const row = button.closest('[data-suggestion-for]');
    const field = fieldFor(button.dataset.useSuggestion);
    if (!row || !field) return;
    field.value = row.dataset.suggestionName;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.focus();
    if (status) status.textContent = tr('Suggested name filled in. Press Save to confirm.');
    refresh();
  });

  form?.addEventListener('input', refresh);

  document.addEventListener('localplaud:speakers-renamed', event => {
    for (const [key, name] of Object.entries(event.detail?.names || {})) {
      const row = dialog.querySelector(`[data-suggestion-for="${CSS.escape(key)}"]`);
      if (row) row.dataset.named = String(Boolean(name));
    }
    refresh();
  });
})();
