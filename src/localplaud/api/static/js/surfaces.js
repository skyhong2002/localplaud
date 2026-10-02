/* localplaud library surfaces (Ask, Search, Templates, Discover, Settings).
 * Loaded once by the shell; safe to initialise repeatedly across HTMX swaps:
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
  let pageRoots = [];

  /* Icons come from the shell's inline sprite (<symbol id="i-NAME"> in _icons.html). */
  function iconNode(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'i'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    const use = document.createElementNS(ns, 'use'); use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }
  const iconHtml = (name) => iconNode(name).outerHTML;

  function toast(message, { link = null, timeout = 4200, type = 'info' } = {}) {
    if (window.lp?.toast) {
      return window.lp.toast(message, { type, timeout, action: link ? { label: link.label, onClick: () => { location.href = link.href; } } : undefined });
    }
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


  /* Streaming Ask: POST form data, read Server-Sent Events from the response.
   * Resolves {type:'done', payload} | {type:'cancelled'} | {type:'error', status, message}.
   * Shared with the recording workspace (window.lpSurfaces.streamAsk). */
  async function streamAsk(url, body, { signal, onStart, onSources, onDelta, onReset } = {}) {
    const response = await fetch(url, {
      method: 'POST', body, signal,
      headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'text/event-stream' },
    });
    if (!response.ok || !response.body) {
      const textBody = await response.text().catch(() => '');
      return { type: 'error', status: response.status, message: textBody.length <= 300 && !textBody.includes('<') ? textBody : '' };
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '', result = null;
    const handle = (event, data) => {
      if (event === 'start') onStart?.(data);
      else if (event === 'sources') onSources?.(data);
      else if (event === 'delta') onDelta?.(data);
      else if (event === 'reset') onReset?.(data);
      else if (event === 'done') result = { type: 'done', payload: data };
      else if (event === 'cancelled') result = { type: 'cancelled' };
      else if (event === 'error') result = { type: 'error', status: data.status, message: data.message };
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let index;
      while ((index = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        let event = 'message', data = '';
        for (const line of raw.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trimStart();
        }
        if (data) handle(event, JSON.parse(data));
      }
    }
    if (!result) throw Object.assign(new Error('stream ended early'), { name: 'TypeError' });
    return result;
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
        event.preventDefault(); if (!busy) form.requestSubmit();
      }
    }, { signal });
    autosize();

    const sendLabel = sendButton.getAttribute('aria-label');
    const setBusy = (value) => {
      busy = value;
      // While a request runs, the send button becomes Stop (never disabled).
      sendButton.dataset.mode = value ? 'stop' : 'send';
      sendButton.setAttribute('aria-label', value ? tr('Stop generating') : sendLabel);
      sendButton.title = value ? tr('Stop generating') : '';
      sendButton.replaceChildren(iconNode(value ? 'stop' : 'arrow-up'));
      $$(root, '[data-sf-skill]').forEach((button) => { button.disabled = value; });
      form.setAttribute('aria-busy', String(value));
    };
    let activeStream = null;
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
      const icon = iconNode('alert-circle');
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

    function stoppedNode(partial, question) {
      const article = document.createElement('article');
      article.className = 'sf-msg-ai sf-msg-stopped';
      if (partial) {
        const answer = document.createElement('div');
        answer.className = 'md sf-answer';
        answer.textContent = partial;
        article.append(answer);
      }
      const note = document.createElement('div');
      note.className = 'sf-stopped-note';
      note.textContent = tr('Stopped. This answer was not saved.');
      if (question) {
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'btn sec btn-sm'; retry.dataset.sfRetry = question;
        retry.textContent = tr('Ask again');
        note.append(' ', retry);
      }
      article.append(note);
      return article;
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
      if (skill) {
        body.set('skill_key', skill);
        scopeEntries().forEach(([key, value]) => body.append(key, value));
      } else {
        body.set('q', text);
        if (threadField.value) body.set('thread_id', threadField.value);
        else scopeEntries().forEach(([key, value]) => body.append(key, value));
      }
      let live = null, partial = '';
      const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
      const steps = () => $$(pending, '.sf-progress-step');
      const controller = new AbortController();
      signal.addEventListener('abort', () => controller.abort(), { once: true });
      activeStream = { controller, id: null };
      try {
        const outcome = await streamAsk('/ask/stream', body, {
          signal: controller.signal,
          onStart: (data) => { if (activeStream) activeStream.id = data.stream_id; },
          onSources: (data) => {
            pending.stopTimers();
            const [first, second, third] = steps();
            first.className = 'sf-progress-step done';
            second.className = 'sf-progress-step done';
            second.textContent = `${tr('Reading')} ${data.passages} ${tr(data.passages === 1 ? 'passage' : 'passages')} · ${data.recordings} ${tr(data.recordings === 1 ? 'recording' : 'recordings')}`;
            third.className = 'sf-progress-step active';
            status.textContent = tr('Writing a grounded answer');
          },
          onDelta: (data) => {
            const follow = nearBottom();
            if (!live) {
              pending.stopTimers();
              $(pending, '.sf-skeleton')?.remove();
              live = document.createElement('div');
              live.className = 'md sf-answer sf-live';
              live.setAttribute('aria-live', 'off');
              pending.append(live);
            }
            partial += data.text;
            live.textContent = partial;
            if (follow) scrollToEnd();
          },
          onReset: () => { partial = ''; if (live) live.textContent = ''; },
        });
        if (outcome.type === 'cancelled') {
          pending.stopTimers();
          pending.replaceWith(stoppedNode(partial, label ? '' : text));
          status.textContent = tr('Stopped. This answer was not saved.');
          return;
        }
        if (outcome.type === 'error') throw new Error(tr(outcome.message || 'Could not get an answer'));
        pending.stopTimers();
        const template = document.createElement('template');
        template.innerHTML = outcome.payload.html;
        const fresh = template.content.querySelector('[data-sf-chat]');
        if (!fresh) throw new Error(tr('Could not get an answer'));
        threadBox.replaceChildren(fresh);
        const last = $$(fresh, '.sf-msg-ai').at(-1);
        const keep = $(fresh, '[data-sf-keep]');
        if (keep && !reducedMotion()) keep.classList.add('sf-reveal');
        if (!partial && last && !reducedMotion()) last.classList.add('sf-reveal');
        if (fresh.dataset.threadId) { rememberThread(fresh.dataset.threadId, fresh.dataset.threadTitle || label || text); lockScope(); }
        status.textContent = last?.matches('[data-sf-unavailable]') ? tr('Ask is unavailable right now') : tr('Answer ready');
        scrollToEnd();
      } catch (error) {
        pending.stopTimers();
        if (error.name === 'AbortError' && signal.aborted) return;
        const message = error.name === 'AbortError' || error.name === 'TypeError'
          ? tr('The connection was interrupted. Check History before retrying — the question may still be answered.')
          : error.message;
        pending.replaceWith(errorNode(message, label ? '' : text));
        if (!skill && !input.value) { input.value = previous; autosize(); }
        status.textContent = message; status.classList.add('error');
        scrollToEnd();
      } finally {
        activeStream = null;
        setBusy(false);
      }
    }

    async function stopAsk() {
      const stream = activeStream;
      if (!stream) return;
      status.textContent = tr('Stopping…');
      if (stream.id) {
        try { await fetch(`/api/ask/streams/${encodeURIComponent(stream.id)}/cancel`, { method: 'POST' }); } catch (_error) { /* the abort below also stops it */ }
      } else {
        stream.controller.abort();
      }
    }

    form.addEventListener('submit', (event) => { event.preventDefault(); if (busy) stopAsk(); else ask({ question: input.value }); }, { signal });
    // The composer is `required`, so an empty textarea would block the submit
    // event; Stop must work regardless of what is typed.
    sendButton.addEventListener('click', (event) => { if (busy) { event.preventDefault(); stopAsk(); } }, { signal });
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
      const glyph = iconNode('more');
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
        const icon = iconNode('alert');
        const text = document.createElement('div');
        const title = document.createElement('strong'); title.textContent = tr('Ask is running in a limited state');
        const detail = document.createElement('div'); detail.className = 'sf-banner-detail'; detail.textContent = messages.join(' ');
        const actions = document.createElement('div'); actions.className = 'sf-banner-actions';
        if (!data.indexed_recordings) {
          const link = document.createElement('a'); link.href = '/status'; link.textContent = tr('Check processing and index status');
          actions.append(link);
        }
        if (!data.llm.ok) {
          const link = document.createElement('a'); link.href = '/settings#connections'; link.textContent = tr('Open provider settings');
          actions.append(link);
        }
        text.append(title, detail);
        if (actions.childElementCount) text.append(actions);
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

  /* ============================ SEARCH ============================ */
  const RECENT_KEY = 'localplaud:recent-searches';
  const readRecent = () => { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]').filter((item) => typeof item === 'string'); } catch (_error) { return []; } };
  const writeRecent = (items) => { try { localStorage.setItem(RECENT_KEY, JSON.stringify(items.slice(0, 8))); } catch (_error) { /* storage disabled */ } };
  function rememberSearch(query) {
    const value = (query || '').trim();
    if (!value) return;
    writeRecent([value, ...readRecent().filter((item) => item.toLowerCase() !== value.toLowerCase())]);
  }
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }
  function highlightText(text, query) {
    const escaped = escapeHtml(text);
    const terms = (query || '').trim().split(/\s+/).filter(Boolean).slice(0, 8).map(escapeHtml)
      .sort((a, b) => b.length - a.length).map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!terms.length) return escaped;
    return escaped.replace(new RegExp(terms.join('|'), 'gi'), (match) => `<mark>${match}</mark>`);
  }
  function formatWhen(ms) {
    if (!ms) return '';
    const date = new Date(ms), pad = (value) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  function moveActive(items, delta) {
    const visible = items.filter((item) => item.offsetParent !== null);
    if (!visible.length) return null;
    const current = visible.findIndex((item) => item.classList.contains('sf-active') || item === document.activeElement);
    const next = visible[current === -1 ? (delta > 0 ? 0 : visible.length - 1) : Math.max(0, Math.min(visible.length - 1, current + delta))];
    items.forEach((item) => item.classList.remove('sf-active'));
    next.classList.add('sf-active');
    return next;
  }

  initialisers.search = (root, signal) => {
    document.documentElement.classList.add('js-sf');
    const form = $(root, '[data-sf-search-form]');
    const input = $(root, '[data-sf-search-input]');
    const clear = $(root, '[data-sf-search-clear]');
    const toggle = $(root, '[data-sf-filter-toggle]');
    const filters = $(root, '#sf-search-filters');
    const preset = $(root, '[data-sf-date-preset]');
    const range = $(root, '[data-sf-date-range]');
    const syncRange = () => { range.hidden = preset.value !== 'custom'; };
    syncRange();

    toggle.addEventListener('click', () => {
      const open = filters.hidden;
      filters.hidden = !open; toggle.setAttribute('aria-expanded', String(open));
      if (open) $(filters, 'select')?.focus();
    }, { signal });
    input.addEventListener('input', () => { clear.hidden = !input.value; }, { signal });
    clear.addEventListener('click', () => { input.value = ''; clear.hidden = true; input.focus(); input.dispatchEvent(new Event('input', { bubbles: true })); }, { signal });
    preset.addEventListener('change', (event) => {
      event.stopPropagation();
      const from = $(range, '[name="date_from"]'), to = $(range, '[name="date_to"]');
      const day = (offset) => { const date = new Date(); date.setDate(date.getDate() - offset); return date.toISOString().slice(0, 10); };
      if (preset.value === 'today') { from.value = day(0); to.value = day(0); }
      else if (preset.value === '7') { from.value = day(6); to.value = day(0); }
      else if (preset.value === '30') { from.value = day(29); to.value = day(0); }
      else if (preset.value === '') { from.value = ''; to.value = ''; }
      syncRange();
      if (preset.value !== 'custom') from.dispatchEvent(new Event('change', { bubbles: true }));
    }, { signal });
    $(root, '[data-sf-back]')?.addEventListener('click', (event) => {
      if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) { event.preventDefault(); history.back(); }
    }, { signal });

    const renderRecent = () => {
      const results = $(root, '[data-sf-search-results]');
      if (!results) return;
      if (results.dataset.query) { rememberSearch(results.dataset.query); return; }
      const section = $(results, '[data-sf-recent]'), list = $(results, '[data-sf-recent-list]');
      if (!section) return;
      const items = readRecent();
      list.replaceChildren(...items.map((item) => {
        const link = document.createElement('a'); link.href = `/search?q=${encodeURIComponent(item)}`; link.textContent = item; link.dataset.sfResult = ''; return link;
      }));
      section.hidden = !items.length;
    };
    renderRecent();
    root.addEventListener('click', (event) => {
      if (event.target.closest('[data-sf-recent-clear]')) { writeRecent([]); renderRecent(); }
    }, { signal });
    root.addEventListener('change', (event) => {
      const radio = event.target.closest('[data-sf-kind-filter]');
      if (!radio) return;
      event.stopPropagation();
      const list = $(root, '.sf-search-list');
      if (list) list.dataset.kind = radio.value;
    }, { signal, capture: true });
    document.body.addEventListener('htmx:afterSwap', (event) => {
      if (event.detail?.target?.id === 'sf-search-results' || event.target?.id === 'sf-search-results') renderRecent();
    }, { signal });
    root.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      if (event.target.matches('select,input[type="date"],input[type="radio"]')) return;
      const items = $$(root, '[data-sf-result]');
      const next = moveActive(items, event.key === 'ArrowDown' ? 1 : -1);
      if (next) { event.preventDefault(); next.focus(); }
    }, { signal });
  };

  /* Command palette: global, opened with Ctrl/Cmd+K or "/" or the Search nav item. */
  let palette = null;
  function buildPalette() {
    const dialog = document.createElement('dialog');
    dialog.className = 'sf-palette';
    dialog.setAttribute('aria-label', tr('Search'));
    dialog.innerHTML = `
      <div class="sf-palette-top">
        <label class="sf-search-field">${iconHtml('search')}
          <input type="search" autocomplete="off" enterkeyhint="search" role="combobox" aria-expanded="true" aria-controls="sf-palette-list" aria-autocomplete="list"></label>
        <button class="sf-icon-btn" type="button" data-sf-palette-close></button>
      </div>
      <div class="sf-palette-body" id="sf-palette-list" role="listbox"></div>
      <div class="sf-palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd> <em></em></span><span><kbd>Enter</kbd> <em></em></span><span><kbd>Esc</kbd> <em></em></span><a href="/search"></a></div>`;
    const input = $(dialog, 'input');
    input.placeholder = tr('Search files by name or keyword');
    input.setAttribute('aria-label', tr('Search'));
    const close = $(dialog, '[data-sf-palette-close]');
    close.setAttribute('aria-label', tr('Close'));
    close.innerHTML = iconHtml('x');
    const hints = $$(dialog, '.sf-palette-foot em');
    hints[0].textContent = tr('to move'); hints[1].textContent = tr('to open'); hints[2].textContent = tr('to close');
    const advanced = $(dialog, '.sf-palette-foot a');
    advanced.textContent = tr('Advanced search');
    const body = $(dialog, '.sf-palette-body');
    let timer = null, controller = null;

    const row = ({ href, icon, title, snippet, meta = [], query = '' }) => {
      const link = document.createElement('a');
      link.className = 'sf-palette-row'; link.href = href; link.setAttribute('role', 'option');
      link.innerHTML = `${iconHtml(icon)}<span class="sf-row-main"><span class="sf-row-title">${highlightText(title, query)}</span>${snippet ? `<span class="sf-row-snippet">${highlightText(snippet, query)}</span>` : ''}</span><span class="sf-row-meta">${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join('')}</span>`;
      link.addEventListener('click', () => { rememberSearch(query); dialog.close(); });
      return link;
    };
    const caption = (text) => { const node = document.createElement('div'); node.className = 'sf-palette-caption'; node.textContent = text; return node; };

    async function renderEmpty() {
      body.replaceChildren();
      const recent = readRecent();
      if (recent.length) {
        const head = caption(tr('Search history'));
        const clearButton = document.createElement('button');
        clearButton.type = 'button'; clearButton.className = 'sf-icon-btn'; clearButton.setAttribute('aria-label', tr('Clear search history'));
        clearButton.innerHTML = iconHtml('trash');
        clearButton.addEventListener('click', () => { writeRecent([]); renderEmpty(); input.focus(); });
        head.append(clearButton);
        const chips = document.createElement('div'); chips.className = 'sf-palette-chips';
        recent.forEach((item) => { const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'sf-palette-chip'; chip.textContent = item; chip.addEventListener('click', () => { input.value = item; query(); input.focus(); }); chips.append(chip); });
        body.append(head, chips);
      }
      try {
        const data = await requestJson('/api/files/picker?limit=6', {}, 'Could not load recordings');
        if (input.value.trim() || !data.recordings.length) return;
        body.append(caption(tr('Recent recordings')));
        data.recordings.forEach((item) => body.append(row({ href: `/file/${encodeURIComponent(item.id)}`, icon: 'waveform', title: item.title, meta: [item.recorded_at ? formatWhen(Date.parse(item.recorded_at)) : ''] })));
      } catch (_error) { /* recent list is optional */ }
    }

    async function query() {
      const value = input.value.trim();
      advanced.href = value ? `/search?q=${encodeURIComponent(value)}` : '/search';
      clearTimeout(timer); controller?.abort();
      if (!value) { renderEmpty(); return; }
      timer = setTimeout(async () => {
        controller = new AbortController();
        body.setAttribute('aria-busy', 'true');
        try {
          const data = await requestJson(`/api/search?limit=8&q=${encodeURIComponent(value)}`, { signal: controller.signal }, 'Search failed');
          body.replaceChildren(caption(tr('Ask localplaud')));
          body.append(row({ href: `/ask?q=${encodeURIComponent(value)}&send=1`, icon: 'sparkles', title: `${tr('Search all sources for')} “${value}” ${tr('with Ask')}`, query: '' }));
          body.append(caption(tr('Best matches')));
          if (!data.results.length) {
            const empty = document.createElement('div'); empty.className = 'sf-palette-empty'; empty.textContent = `${tr('No matches for')} “${value}”`; body.append(empty);
          }
          data.results.forEach((item) => {
            const stamp = item.start != null ? `${Math.floor(item.start / 60)}:${String(Math.floor(item.start % 60)).padStart(2, '0')} · ` : '';
            body.append(row({ href: item.href, icon: item.start != null ? 'play' : 'file-text', title: item.title, snippet: item.snippet ? `${stamp}${item.speaker ? `${item.speaker}: ` : ''}${item.snippet}` : '', meta: [formatWhen(item.start_time_ms), item.kind], query: value }));
          });
          const first = $(body, '.sf-palette-row'); first?.classList.add('sf-active');
        } catch (error) {
          if (error.name === 'AbortError') return;
          body.replaceChildren(); const empty = document.createElement('div'); empty.className = 'sf-palette-empty'; empty.textContent = error.message; body.append(empty);
        } finally { body.removeAttribute('aria-busy'); }
      }, 250);
    }
    input.addEventListener('input', query);
    dialog.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const next = moveActive($$(body, '.sf-palette-row'), event.key === 'ArrowDown' ? 1 : -1);
        next?.scrollIntoView({ block: 'nearest' });
      } else if (event.key === 'Enter' && event.target === input) {
        event.preventDefault();
        const active = $(body, '.sf-palette-row.sf-active') || $(body, '.sf-palette-row');
        const value = input.value.trim();
        if (!active) { if (value) { rememberSearch(value); location.href = `/search?q=${encodeURIComponent(value)}`; } return; }
        rememberSearch(value);
        if (event.ctrlKey || event.metaKey) window.open(active.href, '_blank', 'noopener');
        else { dialog.close(); location.href = active.href; }
      }
    });
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => { controller?.abort(); });
    dialog.openPalette = (initial = '') => {
      if (!dialog.isConnected) document.body.append(dialog);
      input.value = initial;
      dialog.showModal();
      query();
      input.focus();
    };
    return dialog;
  }
  function openSearch(initial = '') {
    const pageInput = document.querySelector('[data-surface="search"] [data-sf-search-input]');
    if (pageInput) { pageInput.focus(); pageInput.select(); return; }
    palette ||= buildPalette();
    if (!palette.open) palette.openPalette(initial);
  }
  const editable = (node) => node && (node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName));
  document.addEventListener('keydown', (event) => {
    if ((event.key === 'k' || event.key === 'K') && (event.ctrlKey || event.metaKey) && !event.altKey) { event.preventDefault(); openSearch(); }
    else if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !editable(event.target) && !document.querySelector('dialog[open]')) { event.preventDefault(); openSearch(); }
  });
  document.addEventListener('click', (event) => {
    const link = event.target.closest?.('a[href="/search"]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (document.querySelector('[data-surface="search"]')) return;
    event.preventDefault(); event.stopImmediatePropagation();
    document.body.classList.remove('nav-open');
    openSearch();
  }, true);

  /* =========================== TEMPLATES =========================== */
  initialisers.templates = (root, signal) => {
    const dataNode = document.getElementById('sf-template-data');
    const items = dataNode ? JSON.parse(dataNode.textContent || '[]') : [];
    const byKey = Object.fromEntries(items.map((item) => [item.key, item]));
    const detail = document.querySelector('[data-sf-template-detail]');
    const editor = document.querySelector('[data-sf-template-editor]');
    const form = $(editor, 'form');
    const formStatus = $(editor, '#template-form-status');
    const detailStatus = $(detail, '[data-sf-detail-status]');
    let current = null, mode = 'create', dirty = false;

    const closeOnBackdrop = (dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }, { signal });
    closeOnBackdrop(detail);
    $$(detail, '[data-sf-dialog-close]').forEach((button) => button.addEventListener('click', () => detail.close(), { signal }));

    function openDetail(key, trigger) {
      const item = byKey[key];
      if (!item) return;
      current = item;
      $(detail, '[data-sf-detail-title]').textContent = item.name;
      const iconBox = $(detail, '[data-sf-detail-icon]');
      iconBox.replaceChildren(iconNode(item.glyph?.icon || 'template'));
      iconBox.style.setProperty('--tc', item.glyph?.color || '');
      $(detail, '[data-sf-detail-meta]').textContent = [tr(item.category), tr(item.scenario), tr(item.author)].filter(Boolean).join(' • ');
      $(detail, '[data-sf-detail-description]').textContent = item.description;
      const facts = $(detail, '[data-sf-detail-facts]');
      facts.replaceChildren();
      const fact = (label, value) => { const dt = document.createElement('dt'); dt.textContent = tr(label); const dd = document.createElement('dd'); dd.textContent = value; facts.append(dt, dd); };
      fact('Source', tr(item.provenance_label));
      fact('Author', tr(item.author));
      fact('Version', `v${item.version}`);
      fact('Key', item.key);
      fact('Output mode', tr(item.prompt_mode === 'direct' ? 'Direct' : 'Structured'));
      fact('Used for', `${item.usage_count || 0} ${tr(item.usage_count === 1 ? 'note' : 'notes')}`);
      $(detail, '[data-sf-detail-prompt]').textContent = item.instructions;
      const systemWrap = $(detail, '[data-sf-detail-system-wrap]');
      systemWrap.hidden = !item.system_prompt;
      $(detail, '[data-sf-detail-system]').textContent = item.system_prompt || '';
      $(detail, '[data-sf-template-copy]').hidden = !item.is_builtin;
      $(detail, '[data-sf-template-edit]').hidden = item.is_builtin;
      $(detail, '[data-sf-template-duplicate]').hidden = item.is_builtin;
      $(detail, '[data-sf-template-archive]').hidden = item.is_builtin;
      detailStatus.textContent = ''; detailStatus.classList.remove('error');
      detail.showModal();
      $(detail, '[data-sf-dialog-close]').focus();
      detail.dataset.opener = '';
      detail.openerNode = trigger || null;
    }
    detail.addEventListener('close', () => detail.openerNode?.focus?.(), { signal });

    function slugify(value) {
      const slug = value.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
      return slug || `template-${Date.now().toString(36)}`;
    }
    function uniqueKey(base) {
      let key = base.slice(0, 60), n = 2;
      while (byKey[key]) key = `${base.slice(0, 56)}-${n++}`;
      return key;
    }

    function openEditor(nextMode, item = null) {
      mode = nextMode; current = item; form.reset(); dirty = false;
      formStatus.textContent = ''; formStatus.classList.remove('error');
      const note = $(editor, '[data-sf-version-note]');
      const title = $(editor, '#template-modal-title');
      form.elements.key.disabled = nextMode === 'edit';
      if (item) {
        form.elements.name.value = nextMode === 'duplicate' ? `${item.name} ${tr('copy')}` : item.name;
        form.elements.instructions.value = item.instructions;
        form.elements.system_prompt.value = item.system_prompt || '';
        form.elements.prompt_mode.value = item.prompt_mode || 'structured';
        form.elements.key.value = nextMode === 'edit' ? item.key : uniqueKey(`my-${item.key}`);
      }
      title.textContent = tr(nextMode === 'edit' ? 'Edit template' : nextMode === 'duplicate' ? 'Duplicate template' : 'Create template');
      note.hidden = nextMode !== 'edit';
      if (nextMode === 'edit') note.textContent = `${tr('Saving creates version')} ${item.version + 1}. ${tr('Notes already generated keep the version they used.')}`;
      if (detail.open) detail.close();
      editor.showModal();
      form.elements.name.focus();
    }
    form.addEventListener('input', () => { dirty = true; }, { signal });
    const cancelEditor = () => { if (dirty && !confirm(tr('Discard changes to this template?'))) return; dirty = false; editor.close(); };
    $$(editor, '[data-sf-editor-cancel]').forEach((button) => button.addEventListener('click', cancelEditor, { signal }));
    editor.addEventListener('cancel', (event) => { if (dirty) { event.preventDefault(); cancelEditor(); } }, { signal });
    editor.addEventListener('click', (event) => { if (event.target === editor) cancelEditor(); }, { signal });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = form.elements.name.value.trim(), instructions = form.elements.instructions.value.trim();
      if (!name || !instructions) {
        formStatus.textContent = tr('Add a name and the note structure.'); formStatus.classList.add('error');
        (name ? form.elements.instructions : form.elements.name).focus(); return;
      }
      const body = { name, instructions, system_prompt: form.elements.system_prompt.value, prompt_mode: form.elements.prompt_mode.value };
      let url = '/api/note-templates', method = 'POST';
      if (mode === 'edit') { url = `/api/note-templates/${encodeURIComponent(current.key)}`; method = 'PUT'; }
      else body.key = (form.elements.key.value.trim() || uniqueKey(slugify(name))).toLowerCase();
      const save = $(editor, '[data-sf-editor-save]');
      save.disabled = true; formStatus.classList.remove('error'); formStatus.textContent = tr('Saving…');
      try {
        await requestJson(url, { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, 'Could not save template');
        dirty = false; formStatus.textContent = tr('Saved. Reloading…');
        location.href = '/templates?tab=my';
      } catch (error) {
        formStatus.textContent = error.message; formStatus.classList.add('error'); save.disabled = false;
      }
    }, { signal });

    async function copyToMine(button) {
      button.disabled = true; detailStatus.classList.remove('error'); detailStatus.textContent = tr('Adding…');
      try {
        let key = uniqueKey(`my-${current.key}`);
        for (let attempt = 0; attempt < 4; attempt += 1) {
          try {
            await requestJson(`/api/note-templates/${encodeURIComponent(current.key)}/copy`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ key, name: current.name }) }, 'Could not copy template');
            location.href = '/templates?tab=my'; return;
          } catch (error) {
            if (!/already exists/.test(error.message) && error.message !== tr('template key already exists')) throw error;
            key = `${key.slice(0, 56)}-${attempt + 2}`;
          }
        }
        throw new Error(tr('Could not copy template'));
      } catch (error) { detailStatus.textContent = error.message; detailStatus.classList.add('error'); button.disabled = false; }
    }
    async function archive(button) {
      if (!confirm(tr('Archive this template? Notes that used it keep their content and provenance.'))) return;
      button.disabled = true;
      try {
        await requestJson(`/api/note-templates/${encodeURIComponent(current.key)}`, { method: 'DELETE' }, 'Could not archive template');
        location.reload();
      } catch (error) { detailStatus.textContent = error.message; detailStatus.classList.add('error'); button.disabled = false; }
    }
    $(detail, '[data-sf-template-copy]').addEventListener('click', (event) => copyToMine(event.currentTarget), { signal });
    $(detail, '[data-sf-template-edit]').addEventListener('click', () => openEditor('edit', current), { signal });
    $(detail, '[data-sf-template-duplicate]').addEventListener('click', () => openEditor('duplicate', current), { signal });
    $(detail, '[data-sf-template-archive]').addEventListener('click', (event) => archive(event.currentTarget), { signal });

    root.addEventListener('click', (event) => {
      const open = event.target.closest('[data-sf-template-open]');
      if (open) { openDetail(open.dataset.sfTemplateOpen, open); return; }
      if (event.target.closest('[data-sf-template-create]')) openEditor('create');
    }, { signal });

    /* Carousels: prev/next buttons and edge fades reflect the scroll position. */
    const carouselSyncs = [];
    $$(root, '[data-sf-carousel-wrap]').forEach((wrap) => {
      const track = $(wrap, '[data-sf-carousel]');
      const prev = $(wrap, '[data-sf-carousel-step="-1"]'), next = $(wrap, '[data-sf-carousel-step="1"]');
      const sync = () => {
        const max = track.scrollWidth - track.clientWidth;
        const canPrev = track.scrollLeft > 2, canNext = track.scrollLeft < max - 2;
        wrap.classList.toggle('can-prev', canPrev); wrap.classList.toggle('can-next', canNext);
        const focused = document.activeElement;
        prev.hidden = !canPrev; next.hidden = !canNext;
        // Keep keyboard focus on the strip when the pressed button disappears at an end.
        if (focused === prev && !canPrev && canNext) next.focus();
        else if (focused === next && !canNext && canPrev) prev.focus();
      };
      carouselSyncs.push(sync);
      const step = (direction) => {
        const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
        track.scrollBy({ left: direction * Math.max(track.clientWidth * 0.8, 260), behavior: reduce ? 'auto' : 'smooth' });
      };
      [prev, next].forEach((button) => button.addEventListener('click', () => step(Number(button.dataset.sfCarouselStep)), { signal }));
      track.addEventListener('scroll', sync, { passive: true, signal });
      window.addEventListener('resize', sync, { signal });
      sync();
    });

    /* Instant client-side narrowing while typing; Enter runs the server search. */
    const filter = $(root, '[data-sf-template-filter]');
    filter?.addEventListener('input', () => {
      const needle = filter.value.trim().toLowerCase();
      $$(root, '.sf-tcard[data-template-key]').forEach((card) => {
        const item = byKey[card.dataset.templateKey];
        const hay = item ? [item.name, item.description, item.category, item.scenario, item.author].join(' ').toLowerCase() : '';
        card.hidden = Boolean(needle) && !hay.includes(needle);
      });
      carouselSyncs.forEach((sync) => sync());
    }, { signal });

    const params = new URLSearchParams(location.search);
    if (params.get('template') && byKey[params.get('template')]) openDetail(params.get('template'));
    if (params.get('create') === '1') openEditor('create');
  };

  /* ============================ DISCOVER / AUTOFLOW ============================ */
  initialisers.discover = (root, signal) => {
    const form = document.getElementById('rule-form');
    const backdrop = document.getElementById('rule-backdrop');
    const sentence = form && $(form, '[data-sf-rule-sentence]');
    const grid = $(root, '[data-sf-rule-grid]');
    const orderStatus = $(root, '#sf-order-status');

    /* Live rule-sentence preview, worded by the server exactly like saved rules. */
    if (form && sentence) {
      const emptyText = sentence.textContent;
      let timer = null, controller = null;
      const num = (data, name) => (data.get(name) ? Number(data.get(name)) : null);
      const payload = () => {
        const data = new FormData(form);
        return {
          trigger: {
            origin: data.get('origin') || null, title_contains: data.get('title_contains') || null,
            transcript_contains: data.get('transcript_contains') || null,
            min_duration_minutes: num(data, 'min_duration_minutes'), max_duration_minutes: num(data, 'max_duration_minutes'),
            folder_id: num(data, 'trigger_folder_id'), tag_id: num(data, 'trigger_tag_id'),
          },
          actions: {
            note_template_key: data.get('note_template_key') || null, profile_id: num(data, 'profile_id'),
            folder_id: num(data, 'action_folder_id'), add_tag_ids: num(data, 'add_tag_id') ? [num(data, 'add_tag_id')] : [],
            export_formats: data.getAll('export_formats'),
            webhook_integration_ids: num(data, 'webhook_integration_id') ? [num(data, 'webhook_integration_id')] : [],
            email_integration_ids: num(data, 'email_integration_id') ? [num(data, 'email_integration_id')] : [],
          },
          notify: data.get('notify') === 'on',
        };
      };
      const refresh = () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          controller?.abort(); controller = new AbortController();
          try {
            const data = await requestJson('/api/automations/sentence-preview', {
              method: 'POST', headers: { 'content-type': 'application/json' },
              body: JSON.stringify(payload()), signal: controller.signal,
            }, 'Could not preview this AutoFlow');
            sentence.textContent = data.sentence || emptyText;
          } catch (error) { if (error.name !== 'AbortError') sentence.textContent = error.message; }
        }, 220);
      };
      form.addEventListener('input', refresh, { signal });
      form.addEventListener('change', refresh, { signal });
      // The editor fills the form programmatically when it opens.
      new MutationObserver(() => { if (!backdrop.hidden) refresh(); })
        .observe(backdrop, { attributes: true, attributeFilter: ['hidden'] });
    }

    /* First-match ordering: drag the handle, use its arrow keys, or the ▲▼ buttons. */
    if (!grid) return;
    const editableCards = () => $$(grid, '.sf-rule-card').filter((card) => $(card, '[data-sf-drag]'));
    let saveTimer = null, dragging = null;
    const announce = (card) => {
      const cards = editableCards();
      orderStatus.textContent = `${$(card, '.sf-rule-name').textContent} · ${tr('position')} ${cards.indexOf(card) + 1} / ${cards.length}`;
    };
    const scheduleSave = () => {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async () => {
        const ids = editableCards().map((card) => Number(card.dataset.ruleId));
        try {
          const data = await requestJson('/api/automations/rule-order', {
            method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ rule_ids: ids }),
          }, 'Could not reorder AutoFlows');
          for (const item of data.rules) {
            const label = $(grid, `.sf-rule-card[data-rule-id="${item.id}"] [data-sf-priority]`);
            if (label) label.textContent = `${tr('priority')} ${item.priority} · v${item.version}`;
          }
          if (data.changed.length) toast(tr('AutoFlow order saved'));
        } catch (error) {
          toast(error.message, { type: 'error' });
          orderStatus.textContent = error.message;
        }
      }, 650);
    };
    const move = (card, delta) => {
      const cards = editableCards();
      const index = cards.indexOf(card), target = cards[index + delta];
      if (!target) return false;
      if (delta < 0) target.before(card); else target.after(card);
      announce(card); scheduleSave();
      return true;
    };
    grid.addEventListener('click', (event) => {
      const button = event.target.closest('[data-sf-move]');
      if (!button) return;
      const card = button.closest('.sf-rule-card');
      if (move(card, Number(button.dataset.sfMove))) button.focus();
    }, { signal });
    grid.addEventListener('keydown', (event) => {
      const handle = event.target.closest('[data-sf-drag]');
      if (!handle || !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      if (move(handle.closest('.sf-rule-card'), event.key === 'ArrowUp' || event.key === 'ArrowLeft' ? -1 : 1)) handle.focus();
    }, { signal });
    grid.addEventListener('pointerdown', (event) => {
      const handle = event.target.closest('[data-sf-drag]');
      if (handle) handle.closest('.sf-rule-card').draggable = true;
    }, { signal });
    grid.addEventListener('dragstart', (event) => {
      const card = event.target.closest('.sf-rule-card');
      if (!card?.draggable) { event.preventDefault(); return; }
      dragging = card; card.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', card.dataset.ruleId);
    }, { signal });
    grid.addEventListener('dragover', (event) => {
      if (!dragging) return;
      const over = event.target.closest('.sf-rule-card');
      if (!over || over === dragging || !$(over, '[data-sf-drag]')) return;
      event.preventDefault();
      const rect = over.getBoundingClientRect();
      const before = rect.width > rect.height * 1.6 ? event.clientY < rect.top + rect.height / 2 : event.clientX < rect.left + rect.width / 2;
      if (before) over.before(dragging); else over.after(dragging);
    }, { signal });
    const endDrag = () => {
      if (!dragging) return;
      dragging.classList.remove('dragging'); dragging.draggable = false;
      announce(dragging); scheduleSave(); dragging = null;
    };
    grid.addEventListener('drop', (event) => { event.preventDefault(); endDrag(); }, { signal });
    grid.addEventListener('dragend', endDrag, { signal });
  };

  /* ============================ SETTINGS ============================ */
  initialisers.settings = (root, signal) => {
    const index = $(root, '[data-sf-settings-index]');
    const layout = $(root, '.settings-layout');
    const back = $(root, '[data-sf-settings-back]');
    const groups = $$(root, '[data-sf-group]');
    const navLinks = $$(root, '[data-sf-nav-target]');
    const groupOf = (id) => document.getElementById(id)?.closest('[data-sf-group]')?.dataset.sfGroup || null;

    const title = document.querySelector('[data-sf-settings-title]');
    const topBack = document.querySelector('[data-sf-settings-topback]');
    const baseTitle = title?.textContent || '';

    function render() {
      const hash = decodeURIComponent(location.hash.slice(1));
      const active = hash.startsWith('group-') ? hash.slice(6) : (hash ? groupOf(hash) : null);
      back.hidden = true;
      if (!isPhone()) {
        index.hidden = true; layout.hidden = false;
        groups.forEach((group) => { group.hidden = group.hasAttribute('data-sf-phone-only'); });
        root.dataset.view = 'all';
        return;
      }
      if (!active) {
        index.hidden = false; layout.hidden = true; root.dataset.view = 'index';
        if (title) title.textContent = baseTitle;
        return;
      }
      index.hidden = true; layout.hidden = false; root.dataset.view = 'group';
      groups.forEach((group) => { group.hidden = group.dataset.sfGroup !== active; });
      const heading = $(root, `[data-sf-group="${active}"] .sf-settings-group-title`);
      if (title && heading) title.textContent = heading.textContent;
      setTimeout(() => {
        const target = hash && !hash.startsWith('group-') ? document.getElementById(hash) : null;
        if (target) target.scrollIntoView({ block: 'start' }); else window.scrollTo(0, 0);
      }, 30);
    }
    window.addEventListener('hashchange', render, { signal });
    window.matchMedia('(max-width: 820px)').addEventListener('change', render, { signal });
    const toIndex = () => {
      history.pushState(history.state, '', location.pathname + location.search);
      render();
      $(index, '.sf-index-row')?.focus();
    };
    back.addEventListener('click', toIndex, { signal });
    topBack?.addEventListener('click', (event) => {
      if (root.dataset.view === 'group') { event.preventDefault(); toIndex(); return; }
      if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) {
        event.preventDefault(); history.back();
      }
    }, { signal });

    /* Preferences toggles persist through the workspace preferences API. */
    $$(root, '[data-sf-pref-toggle]').forEach((toggle) => toggle.addEventListener('change', async () => {
      const out = $(toggle.closest('.sf-pref-row').nextElementSibling ? toggle.closest('.sf-prefs') : root, '[data-sf-pref-status]');
      const desired = toggle.checked;
      toggle.disabled = true;
      try {
        const current = await requestJson('/api/preferences/workspace', {}, 'Could not save preferences');
        await requestJson('/api/preferences/workspace', {
          method: 'PUT', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ ...current, [toggle.dataset.sfPrefToggle]: desired }),
        }, 'Could not save preferences');
        toast(tr('Preferences saved'));
      } catch (error) {
        toggle.checked = !desired;
        if (out) out.textContent = error.message;
        toast(error.message, { type: 'error' });
      } finally {
        toggle.disabled = false;
      }
    }, { signal }));
    render();

    /* Desktop scroll-spy: highlight the section currently in view. */
    if ('IntersectionObserver' in window) {
      const byId = new Map(navLinks.map((link) => [link.dataset.sfNavTarget, link]));
      const visible = new Set();
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => { if (entry.isIntersecting) visible.add(entry.target.id); else visible.delete(entry.target.id); });
        const first = [...byId.keys()].find((id) => visible.has(id));
        navLinks.forEach((link) => link.removeAttribute('aria-current'));
        if (first) byId.get(first).setAttribute('aria-current', 'location');
      }, { rootMargin: '-10% 0px -60% 0px' });
      byId.forEach((_link, id) => { const node = document.getElementById(id); if (node) observer.observe(node); });
      signal.addEventListener('abort', () => observer.disconnect(), { once: true });
    }
  };

  function init() {
    const roots = [...document.querySelectorAll('[data-surface]')];
    // DOM flags survive HTMX history snapshots, but their event listeners do not.
    // Track actual nodes and live controllers instead; repeat settle events must
    // not abort an in-flight Ask on an unchanged page.
    if (pageController && !pageController.signal.aborted && roots.length === pageRoots.length
        && roots.every((root, index) => root === pageRoots[index])) return;
    pageController?.abort();
    pageRoots = roots;
    pageController = new AbortController();
    const { signal } = pageController;
    for (const root of roots) {
      const name = root.dataset.surface;
      if (!initialisers[name]) continue;
      try { initialisers[name](root, signal); } catch (error) { console.error('localplaud surface init failed', name, error); }
    }
  }

  window.lpSurfaces = { init, toast, tr, requestJson, openMenu, trapFocus, initialisers, openSearch, highlightText, streamAsk };
  document.addEventListener('htmx:beforeSwap', (event) => { if (event.detail?.target?.id === 'app-view' && event.detail?.shouldSwap !== false) pageController?.abort(); });
  document.addEventListener('lp:navigated', init);
  document.addEventListener('htmx:historyRestore', init);
  init();
})();
