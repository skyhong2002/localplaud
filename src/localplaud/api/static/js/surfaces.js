/* localplaud library surfaces (Ask, Search, Templates, Discover, Settings).
 * Loaded by each surface template; safe to execute repeatedly across HTMX swaps:
 * the first run installs window.lpSurfaces, every run (re)initialises the
 * surfaces present in the document. All user-visible strings go through
 * window.localplaudT so the zh-TW catalog applies. */
(() => {
  'use strict';
  if (window.lpSurfaces) { window.lpSurfaces.init(); return; }

  const tr = (message) => (window.localplaudT ? window.localplaudT(message) : message);
  const $ = (root, selector) => root.querySelector(selector);
  const $$ = (root, selector) => [...root.querySelectorAll(selector)];
  const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const isPhone = () => window.matchMedia('(max-width: 820px)').matches;
  const initialisers = {};
  let pageController = null;

  function toast(message, { link = null, timeout = 4200 } = {}) {
    document.querySelector('.sf-toast')?.remove();
    const node = document.createElement('div');
    node.className = 'sf-toast';
    node.setAttribute('role', 'status');
    node.setAttribute('aria-live', 'polite');
    node.textContent = message;
    if (link) {
      const anchor = document.createElement('a');
      anchor.href = link.href;
      anchor.textContent = ` ${link.label}`;
      node.append(anchor);
    }
    document.body.append(node);
    setTimeout(() => node.remove(), timeout);
  }

  async function requestJson(url, options = {}, fallback = 'Request failed') {
    let response;
    try { response = await fetch(url, options); } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error(tr(fallback));
    }
    let data = {};
    try { data = await response.json(); } catch (_error) { /* empty body */ }
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
      throw new Error(tr(typeof detail === 'string' ? detail : fallback));
    }
    return data;
  }

  function trapFocus(container, signal) {
    container.addEventListener('keydown', (event) => {
      if (event.key !== 'Tab') return;
      const items = $$(container, 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')
        .filter((item) => !item.closest('[hidden]') && item.offsetParent !== null);
      if (!items.length) return;
      const first = items[0], last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }, { signal });
  }

  /* Anchored popover menu with arrow-key navigation. */
  function openMenu(menu, anchor, signal) {
    const rect = anchor.getBoundingClientRect();
    menu.hidden = false;
    const width = menu.offsetWidth, height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
    menu.style.top = `${rect.bottom + height + 8 > window.innerHeight ? rect.top - height - 4 : rect.bottom + 4}px`;
    anchor.setAttribute('aria-expanded', 'true');
    const items = $$(menu, '[role="menuitem"]');
    items[0]?.focus();
    const close = (restore = true) => {
      menu.hidden = true; anchor.setAttribute('aria-expanded', 'false');
      document.removeEventListener('pointerdown', outside, true);
      menu.removeEventListener('keydown', keys);
      if (restore) anchor.focus();
    };
    const outside = (event) => { if (!menu.contains(event.target) && event.target !== anchor) close(false); };
    const keys = (event) => {
      const index = items.indexOf(document.activeElement);
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      else if (event.key === 'ArrowDown') { event.preventDefault(); items[(index + 1) % items.length].focus(); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); items[(index - 1 + items.length) % items.length].focus(); }
      else if (event.key === 'Tab') close(false);
    };
    document.addEventListener('pointerdown', outside, true);
    menu.addEventListener('keydown', keys);
    signal.addEventListener('abort', () => close(false), { once: true });
    return close;
  }

  function trackVisualViewport(signal) {
    const update = () => {
      const height = window.visualViewport ? window.visualViewport.height : window.innerHeight;
      document.documentElement.style.setProperty('--sf-vvh', `${Math.round(height)}px`);
    };
    update();
    window.visualViewport?.addEventListener('resize', update, { signal });
    window.addEventListener('resize', update, { signal });
  }

  /* ============================== ASK ============================== */
  initialisers.ask = (root, signal) => {
    const form = $(root, '[data-sf-ask-form]');
    const input = $(form, 'textarea');
    const threadField = $(form, '[data-sf-thread-field]');
    const threadBox = $(root, '[data-sf-thread]');
    const scroller = $(root, '[data-sf-scroll]');
    const status = $(root, '[data-sf-status]');
    const sendButton = $(form, '.sf-send');
    const sources = $(root, '[data-sf-sources]');
    const sourcesBody = $(root, '[data-sf-sources-body]');
    const sheetScrim = $(root, '.sf-sheet-scrim');
    const scopeDialog = $(root, '[data-sf-scope-dialog]');
    const scopeForm = $(root, '[data-sf-scope-form]');
    const scopeLabel = $(root, '[data-sf-scope-label]');
    const scrollBottom = $(root, '[data-sf-scroll-bottom]');
    const selectedFiles = new Map();
    let busy = false;
    let sourcesOpener = null;

    trackVisualViewport(signal);

    /* --- composer --- */
    const autosize = () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; };
    input.addEventListener('input', autosize, { signal });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault(); form.requestSubmit();
      }
    }, { signal });
    autosize();

    const setBusy = (value) => {
      busy = value;
      sendButton.disabled = value;
      $$(root, '[data-sf-skill]').forEach((button) => { button.disabled = value; });
      form.setAttribute('aria-busy', String(value));
    };
    const scrollToEnd = () => { scroller.scrollTop = scroller.scrollHeight; };
    const chat = () => {
      let node = $(threadBox, '[data-sf-chat]');
      if (!node) {
        threadBox.replaceChildren();
        node = document.createElement('div');
        node.className = 'sf-chat';
        node.dataset.sfChat = '';
        threadBox.append(node);
      }
      return node;
    };

    function progressNode() {
      const article = document.createElement('article');
      article.className = 'sf-msg-ai';
      article.dataset.sfPending = '';
      article.setAttribute('aria-busy', 'true');
      const steps = document.createElement('div');
      steps.className = 'sf-progress';
      const labels = ['Searching your library', 'Reading the most relevant passages', 'Writing a grounded answer'];
      labels.forEach((label, index) => {
        const step = document.createElement('div');
        step.className = `sf-progress-step${index === 0 ? ' active' : ''}`;
        step.textContent = tr(label);
        steps.append(step);
      });
      const skeleton = document.createElement('div');
      skeleton.className = 'sf-skeleton';
      skeleton.innerHTML = '<span></span><span></span><span></span>';
      article.append(steps, skeleton);
      const timers = [700, 2200].map((delay, index) => setTimeout(() => {
        const nodes = steps.children;
        nodes[index].className = 'sf-progress-step done';
        nodes[index + 1].className = 'sf-progress-step active';
      }, delay));
      article.stopTimers = () => timers.forEach(clearTimeout);
      return article;
    }

    function errorNode(message, question) {
      const article = document.createElement('article');
      article.className = 'sf-msg-ai sf-msg-error';
      const card = document.createElement('div');
      card.className = 'sf-error-card';
      card.setAttribute('role', 'alert');
      const icon = document.createElement('span');
      icon.className = 'nav-icon';
      icon.style.setProperty('--nav-icon', "url('/static/lucide/circle-alert.svg')");
      const body = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = tr('Could not get an answer');
      const text = document.createElement('p');
      text.textContent = message;
      const actions = document.createElement('div');
      actions.className = 'sf-error-actions';
      const retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'btn sec'; retry.dataset.sfRetry = question;
      retry.textContent = tr('Retry');
      const history = document.createElement('a');
      history.className = 'btn sec'; history.href = '/ask';
      history.textContent = tr('Check conversation history');
      actions.append(retry, history);
      body.append(title, text, actions);
      card.append(icon, body);
      article.append(card);
      return article;
    }

    function scopeEntries() {
      if (!scopeForm || form.closest('.sf-ask').dataset.locked === 'true') return [];
      const kind = $(scopeForm, 'input[name="sf_scope_kind"]:checked')?.value || 'all';
      const entries = [];
      const value = (name) => $(root, `[name="${name}"]`)?.value || '';
      if (kind === 'folder' && value('ask_folder_id')) entries.push(['ask_folder_id', value('ask_folder_id')]);
      if (kind === 'dates') {
        if (value('ask_date_from')) entries.push(['ask_date_from', value('ask_date_from')]);
        if (value('ask_date_to')) entries.push(['ask_date_to', value('ask_date_to')]);
      }
      if (kind === 'files') selectedFiles.forEach((_title, id) => entries.push(['ask_file_ids', id]));
      for (const name of ['ask_tag_id', 'ask_origin', 'ask_speaker_name']) if (value(name)) entries.push([name, value(name)]);
      return entries;
    }

    function lockScope() {
      root.dataset.locked = 'true';
      if (scopeForm) {
        $$(scopeForm, 'input,select,button').forEach((field) => { if (!field.matches('[data-sf-dialog-close]')) field.disabled = true; });
        const note = $(scopeForm, '[data-sf-scope-locked-note]');
        if (note) note.hidden = false;
      }
      input.placeholder = tr('Ask a follow-up…');
    }

    function rememberThread(threadId, title) {
      if (!threadId) return;
      threadField.value = threadId;
      root.dataset.threadId = threadId;
      const url = new URL(location.href);
      url.search = ''; url.searchParams.set('thread', threadId);
      history.replaceState(history.state, '', `${url.pathname}${url.search}`);
      const list = $(root, '[data-sf-thread-list]');
      $$(list, '.sf-thread-item').forEach((item) => {
        item.classList.toggle('on', item.dataset.threadId === threadId);
        $(item, 'a')?.removeAttribute('aria-current');
      });
      let item = $$(list, '.sf-thread-item').find((candidate) => candidate.dataset.threadId === threadId);
      if (!item) {
        list.querySelector('.sf-rail-empty')?.remove();
        item = threadRow({ thread_id: threadId, title, question_count: 1, saved_note_count: 0 });
        list.prepend(item);
        item.classList.add('on');
      }
      $(item, 'a')?.setAttribute('aria-current', 'page');
    }

    async function ask({ question, skill = null, label = null }) {
      if (busy) return;
      const text = (question || '').trim();
      if (!skill && !text) { input.focus(); return; }
      setBusy(true);
      status.textContent = ''; status.classList.remove('error');
      const box = chat();
      $(threadBox, '[data-sf-empty]')?.remove();
      $(box, '[data-sf-keep]')?.remove();
      const bubble = document.createElement('div');
      bubble.className = 'sf-msg-user';
      bubble.textContent = label || text;
      const pending = progressNode();
      box.append(bubble, pending);
      const previous = input.value;
      if (!skill) { input.value = ''; autosize(); }
      scrollToEnd();
      status.textContent = tr('Searching your library');

      const body = new URLSearchParams();
      body.set('ui', 'chat');
      let url = '/ask';
      if (skill) {
        url = '/ask/skill';
        body.set('skill_key', skill);
        scopeEntries().forEach(([key, value]) => body.append(key, value));
      } else {
        body.set('q', text);
        if (threadField.value) body.set('thread_id', threadField.value);
        else scopeEntries().forEach(([key, value]) => body.append(key, value));
      }
      try {
        let response;
        try {
          response = await fetch(url, { method: 'POST', body, headers: { 'content-type': 'application/x-www-form-urlencoded' }, signal });
        } catch (error) {
          if (error.name === 'AbortError') return;
          throw new Error(tr('The connection was interrupted. Check History before retrying — the question may still be answered.'));
        }
        const html = await response.text();
        if (!response.ok) {
          const detail = html.length <= 300 && !html.includes('<') ? tr(html) : tr('Could not get an answer');
          throw new Error(detail);
        }
        pending.stopTimers();
        const template = document.createElement('template');
        template.innerHTML = html;
        const fresh = template.content.querySelector('[data-sf-chat]');
        if (!fresh) throw new Error(tr('Could not get an answer'));
        threadBox.replaceChildren(fresh);
        const answers = $$(fresh, '.sf-msg-ai');
        const last = answers.at(-1);
        if (last && !reducedMotion()) last.classList.add('sf-reveal');
        const keep = $(fresh, '[data-sf-keep]');
        if (keep && !reducedMotion()) keep.classList.add('sf-reveal');
        if (fresh.dataset.threadId) { rememberThread(fresh.dataset.threadId, fresh.dataset.threadTitle || label || text); lockScope(); }
        status.textContent = last?.matches('[data-sf-unavailable]') ? tr('Ask is unavailable right now') : tr('Answer ready');
        scrollToEnd();
      } catch (error) {
        pending.stopTimers();
        pending.replaceWith(errorNode(error.message, label ? '' : text));
        if (!skill && !input.value) { input.value = previous; autosize(); }
        status.textContent = error.message; status.classList.add('error');
        scrollToEnd();
      } finally {
        setBusy(false);
      }
    }

    form.addEventListener('submit', (event) => { event.preventDefault(); ask({ question: input.value }); }, { signal });
    root.addEventListener('click', (event) => {
      const suggestion = event.target.closest('[data-sf-ask]');
      if (suggestion) { input.value = suggestion.dataset.sfAsk; autosize(); ask({ question: suggestion.dataset.sfAsk }); return; }
      const retry = event.target.closest('[data-sf-retry]');
      if (retry) {
        if (retry.dataset.sfRetry) { retry.closest('.sf-msg-ai')?.previousElementSibling?.remove(); retry.closest('.sf-msg-ai')?.remove(); ask({ question: retry.dataset.sfRetry }); }
        else input.focus();
        return;
      }
      const skill = event.target.closest('[data-sf-skill]');
      if (skill) { ask({ skill: skill.dataset.sfSkill, label: skill.dataset.sfSkillName }); return; }
      const cite = event.target.closest('.sf-cite');
      if (cite) { openSources(cite.closest('.sf-msg-ai'), Number(cite.dataset.cite), cite); return; }
      const openAll = event.target.closest('[data-sf-open-sources]');
      if (openAll) { openSources(openAll.closest('.sf-msg-ai'), null, openAll); return; }
      if (event.target.closest('[data-sf-sources-close]')) { closeSources(); return; }
      const copy = event.target.closest('[data-sf-copy]');
      if (copy) { copyAnswer(copy); return; }
      const save = event.target.closest('[data-sf-save-note]');
      if (save) { saveNote(save); return; }
      if (event.target.closest('[data-sf-scope-open]')) { openScope(event.target.closest('[data-sf-scope-open]')); return; }
      if (event.target.closest('[data-sf-dialog-close]')) { scopeDialog.close(); return; }
      if (event.target.closest('[data-sf-drawer-open]')) { openDrawer(); return; }
      if (event.target.closest('[data-sf-drawer-close]')) { closeDrawer(); return; }
      const more = event.target.closest('[data-sf-thread-menu]');
      if (more) { threadMenu(more); }
    }, { signal });

    /* --- sources panel / sheet --- */
    function openSources(article, index, opener) {
      if (!article) return;
      const template = $(article, 'template[data-sf-source-list]');
      if (!template) return;
      sourcesBody.replaceChildren(template.content.cloneNode(true));
      $$(root, '.sf-cite.on').forEach((pill) => pill.classList.remove('on'));
      if (index) {
        $$(article, `.sf-cite[data-cite="${index}"]`).forEach((pill) => pill.classList.add('on'));
        const target = $(sourcesBody, `[data-source-index="${index}"]`);
        target?.classList.add('on');
        requestAnimationFrame(() => target?.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' }));
      }
      sourcesOpener = opener;
      sources.hidden = false;
      if (isPhone()) { sheetScrim.hidden = false; sources.setAttribute('aria-modal', 'true'); }
      else sources.setAttribute('aria-modal', 'false');
      $(sources, '[data-sf-sources-close]').focus({ preventScroll: true });
    }
    function closeSources() {
      if (sources.hidden) return;
      sources.hidden = true; sheetScrim.hidden = true;
      $$(root, '.sf-cite.on').forEach((pill) => pill.classList.remove('on'));
      sourcesOpener?.focus({ preventScroll: true }); sourcesOpener = null;
    }
    trapFocus(sources, signal);
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (!sources.hidden) { event.preventDefault(); closeSources(); }
      else if (root.classList.contains('drawer-open')) { event.preventDefault(); closeDrawer(); }
    }, { signal });

    async function copyAnswer(button) {
      const answer = $(button.closest('.sf-msg-ai'), '[data-sf-answer]');
      const clone = answer.cloneNode(true);
      $$(clone, '.sf-cite').forEach((pill) => pill.replaceWith(`[${pill.dataset.cite}]`));
      try { await navigator.clipboard.writeText(clone.innerText.trim()); toast(tr('Answer copied')); }
      catch (_error) { toast(tr('Could not copy')); }
    }

    async function saveNote(button) {
      if (button.disabled) return;
      const article = button.closest('.sf-msg-ai');
      const out = $(article, '[data-sf-save-status]');
      const label = $(button, '[data-sf-save-label]');
      button.disabled = true; label.textContent = tr('Saving…');
      try {
        const data = await requestJson(`/api/ask/messages/${button.dataset.sfSaveNote}/save-note`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, 'Could not save');
        if (!Number.isInteger(data.id)) throw new Error(tr('Could not save'));
        label.textContent = tr('Saved');
        out.replaceChildren(document.createTextNode(`${tr('Saved to Notes')} · ${data.title} `));
        const link = document.createElement('a'); link.href = '/notes'; link.textContent = tr('Open Saved notes');
        out.append(link);
      } catch (error) {
        label.textContent = tr('Save as note'); button.disabled = false;
        out.textContent = error.message || tr('Could not save');
      }
    }

    /* --- scroll-to-latest --- */
    scroller.addEventListener('scroll', () => {
      scrollBottom.hidden = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 220;
    }, { signal, passive: true });
    scrollBottom.addEventListener('click', () => { scroller.scrollTo({ top: scroller.scrollHeight, behavior: reducedMotion() ? 'auto' : 'smooth' }); input.focus({ preventScroll: true }); }, { signal });

    /* --- history drawer (phone) --- */
    const drawer = $(root, '[data-sf-drawer]');
    const drawerScrim = $(root, '.sf-drawer-scrim');
    const drawerOpener = $(root, '[data-sf-drawer-open]');
    function openDrawer() { root.classList.add('drawer-open'); drawerScrim.hidden = false; drawerOpener?.setAttribute('aria-expanded', 'true'); requestAnimationFrame(() => $(drawer, 'input')?.focus()); }
    function closeDrawer() { root.classList.remove('drawer-open'); drawerScrim.hidden = true; drawerOpener?.setAttribute('aria-expanded', 'false'); drawerOpener?.focus(); }
    trapFocus(drawer, signal);

    /* --- thread list: search, rename, delete --- */
    const list = $(root, '[data-sf-thread-list]');
    const menu = $(root, '[data-sf-thread-popover]');
    function threadRow(item) {
      const row = document.createElement('div');
      row.className = 'sf-thread-item';
      row.dataset.threadId = item.thread_id;
      const link = document.createElement('a');
      link.href = `/ask?thread=${encodeURIComponent(item.thread_id)}`;
      const title = document.createElement('span'); title.className = 'sf-thread-title'; title.textContent = item.title;
      const meta = document.createElement('span'); meta.className = 'sf-thread-meta';
      const parts = [`${item.question_count} ${tr(item.question_count === 1 ? 'question' : 'questions')}`];
      if (item.saved_note_count) parts.push(`${item.saved_note_count} ${tr(item.saved_note_count === 1 ? 'saved note' : 'saved notes')}`);
      meta.textContent = parts.join(' · ');
      link.append(title, meta);
      const more = document.createElement('button');
      more.type = 'button'; more.className = 'sf-icon-btn sf-thread-more'; more.dataset.sfThreadMenu = '';
      more.setAttribute('aria-haspopup', 'menu'); more.setAttribute('aria-label', tr('Conversation actions'));
      const glyph = document.createElement('span'); glyph.className = 'nav-icon'; glyph.style.setProperty('--nav-icon', "url('/static/lucide/ellipsis.svg')");
      more.append(glyph);
      row.append(link, more);
      if (item.thread_id === root.dataset.threadId) { row.classList.add('on'); link.setAttribute('aria-current', 'page'); }
      return row;
    }
    let searchTimer = null, searchController = null;
    $(root, '[data-sf-thread-search]').addEventListener('input', (event) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        searchController?.abort(); searchController = new AbortController();
        const url = new URL('/api/ask/threads', location.origin);
        if (event.target.value.trim()) url.searchParams.set('q', event.target.value.trim());
        url.searchParams.set('page_size', '40');
        try {
          const data = await requestJson(url, { signal: searchController.signal }, 'Could not load Ask history');
          list.replaceChildren(...data.threads.map(threadRow));
          if (!data.threads.length) {
            const empty = document.createElement('p'); empty.className = 'sf-rail-empty';
            empty.textContent = tr(event.target.value.trim() ? 'No conversations match this search.' : 'No conversations yet.');
            list.append(empty);
          }
        } catch (error) { if (error.name !== 'AbortError') toast(error.message); }
      }, 220);
    }, { signal });

    let closeMenu = null;
    function threadMenu(button) {
      const row = button.closest('.sf-thread-item');
      menu.dataset.threadId = row.dataset.threadId;
      closeMenu = openMenu(menu, button, signal);
    }
    $(menu, '[data-sf-thread-rename]').addEventListener('click', () => {
      const row = $$(list, '.sf-thread-item').find((item) => item.dataset.threadId === menu.dataset.threadId);
      closeMenu?.(false);
      if (!row) return;
      const link = $(row, 'a'), more = $(row, '[data-sf-thread-menu]');
      const editor = document.createElement('form'); editor.className = 'sf-thread-rename';
      const field = document.createElement('input'); field.maxLength = 200; field.required = true; field.value = $(row, '.sf-thread-title').textContent;
      field.setAttribute('aria-label', tr('Rename'));
      const save = document.createElement('button'); save.type = 'submit'; save.className = 'btn'; save.textContent = tr('Save');
      editor.append(field, save);
      link.hidden = true; more.hidden = true; row.append(editor); field.focus(); field.select();
      const restore = () => { editor.remove(); link.hidden = false; more.hidden = false; more.focus(); };
      field.addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); restore(); } });
      editor.addEventListener('submit', async (event) => {
        event.preventDefault(); save.disabled = true;
        try {
          const data = await requestJson(`/api/ask/threads/${encodeURIComponent(row.dataset.threadId)}`, { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ title: field.value }) }, 'Could not rename conversation');
          $(row, '.sf-thread-title').textContent = data.title; restore();
        } catch (error) { save.disabled = false; toast(error.message); }
      });
    }, { signal });
    $(menu, '[data-sf-thread-delete]').addEventListener('click', async () => {
      const threadId = menu.dataset.threadId;
      closeMenu?.(false);
      if (!confirm(`${tr('Delete this conversation?')} ${tr('Saved notes will remain.')}`)) return;
      try {
        await requestJson(`/api/ask/threads/${encodeURIComponent(threadId)}`, { method: 'DELETE' }, 'Could not delete conversation');
        if (threadId === root.dataset.threadId) { location.href = '/ask'; return; }
        $$(list, '.sf-thread-item').find((item) => item.dataset.threadId === threadId)?.remove();
        toast(tr('Conversation deleted'));
      } catch (error) { toast(error.message); }
    }, { signal });

    /* --- scope dialog --- */
    function describeScope() {
      if (!scopeForm) return;
      const kind = $(scopeForm, 'input[name="sf_scope_kind"]:checked')?.value || 'all';
      const parts = [];
      const select = (name) => $(root, `[name="${name}"]`);
      if (kind === 'folder' && select('ask_folder_id').value) parts.push(select('ask_folder_id').selectedOptions[0].textContent.split(' · ')[0]);
      if (kind === 'files' && selectedFiles.size) parts.push(`${selectedFiles.size} ${tr(selectedFiles.size === 1 ? 'recording' : 'recordings')}`);
      if (kind === 'dates' && (select('ask_date_from').value || select('ask_date_to').value)) parts.push(`${select('ask_date_from').value || '…'} – ${select('ask_date_to').value || '…'}`);
      const extra = ['ask_tag_id', 'ask_origin', 'ask_speaker_name'].filter((name) => select(name)?.value).length;
      if (extra) parts.push(`+${extra} ${tr(extra === 1 ? 'filter' : 'filters')}`);
      scopeLabel.textContent = parts.length ? parts.join(' · ') : tr('All files');
    }
    function syncScopePanes() {
      const kind = $(scopeForm, 'input[name="sf_scope_kind"]:checked')?.value || 'all';
      $$(scopeForm, '[data-sf-scope-pane]').forEach((pane) => {
        const on = pane.dataset.sfScopePane === kind;
        pane.hidden = !on;
        $$(pane, 'input[name^="ask_"],select').forEach((field) => { field.disabled = !on || root.dataset.locked === 'true'; });
      });
      if (kind === 'files') loadPicker('');
      describeScope();
    }
    let pickerController = null, pickerTimer = null;
    async function loadPicker(query) {
      const picker = $(scopeForm, '[data-sf-picker]');
      pickerController?.abort(); pickerController = new AbortController();
      const url = new URL('/api/files/picker', location.origin);
      url.searchParams.set('limit', '50');
      if (query) url.searchParams.set('q', query);
      try {
        const data = await requestJson(url, { signal: pickerController.signal }, 'Could not load recordings');
        picker.replaceChildren();
        if (!data.recordings.length) {
          const empty = document.createElement('p'); empty.className = 'sf-muted'; empty.textContent = tr('No recordings match.'); picker.append(empty);
        }
        for (const item of data.recordings) {
          const row = document.createElement('label');
          const box = document.createElement('input'); box.type = 'checkbox'; box.value = item.id; box.checked = selectedFiles.has(item.id);
          box.disabled = root.dataset.locked === 'true';
          box.addEventListener('change', () => { if (box.checked) selectedFiles.set(item.id, item.title); else selectedFiles.delete(item.id); updatePickerCount(); describeScope(); });
          const title = document.createElement('span'); title.textContent = item.title;
          const when = document.createElement('small'); when.textContent = item.recorded_at ? item.recorded_at.slice(0, 10) : '';
          row.append(box, title, when); picker.append(row);
        }
        updatePickerCount();
      } catch (error) { if (error.name !== 'AbortError') { picker.textContent = error.message; } }
    }
    function updatePickerCount() {
      const count = $(scopeForm, '[data-sf-picker-count]');
      count.textContent = selectedFiles.size ? `${selectedFiles.size} ${tr('selected')}` : tr('Select one or more recordings.');
    }
    if (scopeForm) {
      scopeForm.addEventListener('change', (event) => {
        if (event.target.name === 'sf_scope_kind') syncScopePanes(); else describeScope();
      }, { signal });
      $(scopeForm, '[data-sf-picker-search]').addEventListener('input', (event) => {
        clearTimeout(pickerTimer); pickerTimer = setTimeout(() => loadPicker(event.target.value.trim()), 200);
      }, { signal });
      $(scopeForm, '[data-sf-scope-reset]').addEventListener('click', () => {
        $(scopeForm, 'input[value="all"]').checked = true;
        $$(root, '[name^="ask_"]').forEach((field) => { field.value = ''; });
        selectedFiles.clear(); syncScopePanes();
      }, { signal });
      syncScopePanes();
    }
    let scopeOpener = null;
    function openScope(button) { scopeOpener = button; scopeDialog.showModal(); }
    scopeDialog.addEventListener('close', () => { describeScope(); scopeOpener?.focus(); }, { signal });
    scopeDialog.addEventListener('click', (event) => { if (event.target === scopeDialog) scopeDialog.close(); }, { signal });

    /* --- readiness (degraded state) --- */
    (async () => {
      const banner = $(root, '[data-sf-readiness]');
      try {
        const data = await requestJson('/api/ask/readiness', { signal }, 'Could not check Ask readiness');
        const messages = [];
        if (!data.llm.ok) messages.push(`${tr('No language model is reachable for Ask')}${data.llm.provider ? ` (${data.llm.provider})` : ''}${data.llm.detail ? ` — ${data.llm.detail}` : ''}. ${tr('Answers will show as unavailable until a provider is configured.')}`);
        if (!data.indexed_recordings) messages.push(tr('No recordings are indexed yet. Ask can answer once processing has built the search index.'));
        if (!messages.length) return;
        const icon = document.createElement('span'); icon.className = 'nav-icon'; icon.style.setProperty('--nav-icon', "url('/static/lucide/triangle-alert.svg')");
        const text = document.createElement('div');
        const title = document.createElement('strong'); title.textContent = tr('Ask is running in a limited state');
        const detail = document.createElement('div'); detail.className = 'sf-banner-detail'; detail.textContent = messages.join(' ');
        const link = document.createElement('a'); link.href = '/settings#connections'; link.textContent = tr('Open provider settings');
        text.append(title, detail, link);
        banner.replaceChildren(icon, text); banner.hidden = false;
      } catch (_error) { /* readiness is advisory only */ }
    })();

    /* --- entry from Search: /ask?q=…&send=1 --- */
    const params = new URLSearchParams(location.search);
    if (input.value.trim() && params.get('send') === '1' && !threadField.value) {
      const url = new URL(location.href); url.searchParams.delete('send');
      history.replaceState(history.state, '', `${url.pathname}${url.search}`);
      ask({ question: input.value });
    } else if (!isPhone() || input.value) {
      input.focus({ preventScroll: true });
    }
    if (threadField.value) requestAnimationFrame(scrollToEnd);
  };

  function init() {
    pageController?.abort();
    pageController = new AbortController();
    const { signal } = pageController;
    for (const root of document.querySelectorAll('[data-surface]')) {
      const name = root.dataset.surface;
      if (!initialisers[name] || root.dataset.sfReady === 'true') continue;
      root.dataset.sfReady = 'true';
      try { initialisers[name](root, signal); } catch (error) { console.error('localplaud surface init failed', name, error); }
    }
  }

  window.lpSurfaces = { init, toast, tr, requestJson, openMenu, trapFocus, initialisers };
  document.addEventListener('htmx:beforeSwap', (event) => { if (event.detail?.target?.id === 'app-view') pageController?.abort(); });
  init();
})();
