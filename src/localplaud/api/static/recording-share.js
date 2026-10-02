/* Delegated once for full loads, progressive workspaces and HTMX history restores.
   Sharing must not depend on transcript/player initialization succeeding. */
(() => {
  if (window.localplaudRecordingShare === 3) return;
  // A retained HTMX shell may still have v2's anonymous bubble listeners.
  // Close its dialog before taking ownership; capture clicks below so only
  // this version handles the new markup, without reloading unsaved work.
  if (window.localplaudRecordingShare) document.getElementById('share-close')?.click();
  window.localplaudRecordingShare = 3;
  let active = null;
  const tr = message => window.localplaudT?.(message) || message;
  const visibleControls = root => [...root.querySelectorAll('button:not([disabled]),a[href],summary,input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]')].filter(el => el.getClientRects().length && !el.closest('[hidden]'));
  const byId = (state, id) => state.root.querySelector(`#${id}`);
  function close(restore = true) {
    if (!active) return;
    const state = active;
    active = null;
    state.controller.abort();
    state.root.hidden = true;
    state.inert.forEach((previous, el) => { el.inert = previous; });
    document.body.classList.remove('dialog-open');
    if (restore && state.opener.isConnected) state.opener.focus();
  }
  function showView(state, name, focus = true) {
    const panel = state.root.querySelector(`[data-share-panel="${name}"]`);
    if (!panel) return;
    state.root.querySelectorAll('[data-share-panel]').forEach(el => { el.hidden = el !== panel; });
    byId(state, 'share-title').textContent = panel.dataset.title;
    byId(state, 'share-back').hidden = name === 'menu';
    byId(state, 'export-result').textContent = '';
    state.root.querySelector('.lp-recording-actions-body').scrollTop = 0;
    state.view = name;
    if (name === 'link') loadLink(state);
    if (focus) (name === 'menu' ? state.root.querySelector('[data-share-view="link"]') : byId(state, 'share-back')).focus();
  }
  function open(opener) {
    close(false);
    const root = opener.closest('#app-view')?.querySelector('#share-backdrop');
    if (!root) return;
    const state = {root, opener, controller: new AbortController(), busy: false, link: null, inert: new Map()};
    active = state;
    busy(state, false);
    root.hidden = false;
    document.querySelectorAll('.recording-shell,.sidebar,.mobile-bar,.nav-scrim').forEach(el => { state.inert.set(el, el.inert); el.inert = true; });
    document.body.classList.add('dialog-open');
    byId(state, 'share-link').value = new URL(`/file/${encodeURIComponent(root.dataset.fileId)}`, location.origin).href;
    showView(state, 'menu', false);
    requestAnimationFrame(() => { if (active === state) byId(state, 'share-close').focus(); });
  }
  function busy(state, value) {
    state.busy = value;
    state.root.querySelector('.public-share-section').setAttribute('aria-busy', String(value));
    state.root.querySelectorAll('[data-share-option],#create-public-share,#revoke-public-share,#retry-public-share').forEach(el => { el.disabled = value; });
  }
  function renderLink(state, data) {
    state.link = data;
    byId(state, 'public-share-empty').hidden = !!data.active;
    byId(state, 'public-share-active').hidden = !data.active;
    byId(state, 'public-share-link').value = data.url || '';
    byId(state, 'system-share').hidden = !data.active || !navigator.share;
    byId(state, 'public-share-created').textContent = data.created_at ? `${tr('Created')} · ${new Intl.DateTimeFormat(document.documentElement.lang || undefined, {dateStyle:'medium',timeStyle:'short'}).format(new Date(data.created_at))}` : '';
    if (data.options) state.root.querySelectorAll('[data-share-option]').forEach(el => { el.checked = !!data.options[el.dataset.shareOption]; });
  }
  async function request(state, options = {}) {
    const response = await fetch(`/api/files/${encodeURIComponent(state.root.dataset.fileId)}/share-link`, {...options, signal: state.controller.signal});
    let data = {};
    try { data = await response.json(); } catch (_) { /* retain the generic error */ }
    if (!response.ok) throw new Error(tr(typeof data.detail === 'string' ? data.detail : 'Could not update public link'));
    return data;
  }
  async function linkAction(state, method) {
    if (state.busy) return;
    busy(state, true);
    const status = byId(state, 'public-share-result');
    byId(state, 'retry-public-share').hidden = true;
    status.textContent = tr(method === 'DELETE' ? 'Disabling public link…' : method === 'POST' ? 'Creating public link…' : 'Loading public link…');
    if (!method) { byId(state, 'public-share-empty').hidden = true; byId(state, 'public-share-active').hidden = true; }
    try {
      const options = Object.fromEntries([...state.root.querySelectorAll('[data-share-option]')].map(el => [el.dataset.shareOption, el.checked]));
      const data = await request(state, method ? {method, headers:{'Content-Type':'application/json'}, ...(method === 'POST' ? {body:JSON.stringify({options})} : {})} : {});
      if (active !== state) return;
      renderLink(state, method === 'DELETE' ? {active:false} : data);
      status.textContent = method === 'DELETE' ? tr('Public link disabled') : '';
    } catch (error) {
      if (error.name === 'AbortError' || active !== state) return;
      status.textContent = error.message || tr('Could not load public link');
      byId(state, 'retry-public-share').hidden = false;
      // On an uncertain write, reload server state before permitting another change.
      byId(state, 'public-share-empty').hidden = true;
      byId(state, 'public-share-active').hidden = true;
      state.link = null;
    } finally { if (active === state) busy(state, false); }
  }
  function loadLink(state) { return linkAction(state); }
  async function clipboard(text) {
    try { await navigator.clipboard.writeText(text); return; } catch (_) { /* HTTP/Safari fallback */ }
    const area = document.createElement('textarea'), prior = document.activeElement;
    area.value = text; area.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    (active?.root || document.body).append(area); area.select(); area.setSelectionRange(0, text.length);
    const copied = document.execCommand('copy'); area.remove(); prior?.focus();
    if (!copied) throw new Error(tr('Copy failed'));
  }
  async function copy(state, button, source, success) {
    button.disabled = true;
    const output = byId(state, button.id.includes('public') ? 'public-share-result' : button.id === 'copy-share-link' ? 'share-result' : 'export-result');
    output.textContent = tr('Copying…');
    try {
      // Safari requires clipboard.write to start during the click, even when
      // the text itself must first be fetched. Supply a promised Blob.
      const textPromise = Promise.resolve(typeof source === 'function' ? source() : source);
      textPromise.catch(() => {});
      if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
        try {
          await navigator.clipboard.write([new ClipboardItem({'text/plain': textPromise.then(text => new Blob([text], {type:'text/plain'}))})]);
        } catch (_) {
          const text = await textPromise;
          if (active !== state) return;
          await clipboard(text);
        }
      } else {
        const text = await textPromise;
        if (active !== state) return;
        await clipboard(text);
      }
      if (active === state) output.textContent = tr(success);
    } catch (error) { if (active === state && error.name !== 'AbortError') output.textContent = tr('Copy failed'); }
    finally { button.disabled = false; }
  }
  async function exportText(state, kind) {
    const params = kind === 'transcript.txt' ? `?timestamps=${byId(state,'export-timestamps').checked}&speakers=${byId(state,'export-speakers').checked}` : '';
    const response = await fetch(`/file/${encodeURIComponent(state.root.dataset.fileId)}/export/${kind}${params}`, {signal:state.controller.signal});
    if (!response.ok) throw new Error(tr('Copy failed'));
    return response.text();
  }
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-recording-share]');
    if (opener) { event.preventDefault(); event.stopImmediatePropagation(); open(opener); return; }
    const state = active;
    if (!state) return;
    if (event.target === state.root) { event.stopImmediatePropagation(); close(); return; }
    if (!state.root.contains(event.target)) return;
    event.stopImmediatePropagation();
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (button.dataset.shareView) { showView(state, button.dataset.shareView); return; }
    if (button.dataset.copyNote) {
      const node = [...state.root.querySelectorAll('[data-copy-note-text]')].find(el => el.dataset.copyNoteText === button.dataset.copyNote);
      if (node) copy(state, button, JSON.parse(node.textContent), 'Notes copied');
      return;
    }
    switch (button.id) {
      case 'share-close': close(); break;
      case 'share-back': showView(state, 'menu'); break;
      case 'retry-public-share': loadLink(state); break;
      case 'create-public-share': linkAction(state, 'POST'); break;
      case 'revoke-public-share': if (confirm(tr('Disable this public link? Anyone using it will immediately lose access.'))) linkAction(state, 'DELETE'); break;
      case 'copy-public-share': copy(state, button, byId(state,'public-share-link').value, 'Copied'); break;
      case 'copy-share-link': copy(state, button, byId(state,'share-link').value, 'Copied'); break;
      case 'copy-transcript': copy(state, button, () => exportText(state, 'transcript.txt'), 'Transcript copied'); break;
      case 'copy-notes': copy(state, button, () => exportText(state, 'notes.md'), 'Notes copied'); break;
      case 'system-share': navigator.share({url:byId(state,'public-share-link').value}).catch(error => { if(error.name !== 'AbortError') byId(state,'public-share-result').textContent = tr('Share failed'); }); break;
    }
  }, true);
  document.addEventListener('change', event => {
    const state = active;
    if (!state || !state.root.contains(event.target)) return;
    if (event.target.matches('[data-share-option]') && state.link?.active) linkAction(state,'POST');
    if (event.target.matches('#export-timestamps,#export-speakers')) state.root.querySelectorAll('[data-fmt]').forEach(link => {
      const url = new URL(link.href); url.searchParams.set('timestamps',byId(state,'export-timestamps').checked); url.searchParams.set('speakers',byId(state,'export-speakers').checked); link.href=url.href;
    });
  });
  document.addEventListener('keydown', event => {
    if (!active) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(); return; }
    if (event.key !== 'Tab') return;
    const items = visibleControls(active.root), first = items[0], last = items.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || !active.root.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }, true);
  document.addEventListener('htmx:beforeCleanupElement', event => { if (active && (event.target === active.root || event.target.contains?.(active.root))) close(false); });
  document.addEventListener('htmx:beforeHistorySave', () => close(false));
  window.addEventListener('pagehide', () => close(false));
})();
