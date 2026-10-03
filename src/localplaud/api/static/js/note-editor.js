/* localplaud notes: in-place editing and find/replace.
 *
 * Editable notes are edited directly on the rendered page, as in Plaud: a click
 * puts the caret where the user clicked and changes autosave as note versions.
 * The server tags each top-level block with its Markdown source lines
 * (data-md="start:end"); untouched blocks keep that source byte-for-byte and
 * only edited blocks are serialized back to Markdown from the page.
 *
 * Loaded after workspace.js on every full load and HTMX #app-view swap; each
 * run tears down the previous one. Strings go through window.localplaudT.
 */
(() => {
  'use strict';
  const tr = window.localplaudT || (message => message);
  const controller = new AbortController();
  const { signal } = controller;
  window.localplaudNoteEditorTeardown?.();
  window.localplaudNoteEditorTeardown = () => controller.abort();
  const appView = document.getElementById('app-view');
  appView?.addEventListener('htmx:beforeCleanupElement', event => { if (event.target === appView) controller.abort(); }, { signal });
  const on = (target, type, handler, options = {}) => target?.addEventListener(type, handler, { signal, ...options });

  /* ---------- Markdown serialization of edited blocks ---------- */

  const BLOCK_TAGS = new Set(['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'BLOCKQUOTE', 'PRE', 'HR', 'TABLE']);
  const escapeText = text => text.replace(/\u00a0/g, ' ').replace(/[\\`*_[\]~<]/g, '\\$&');
  // CommonMark only treats ** as emphasis when it is flanked correctly; keep
  // spaces and edge punctuation (「」, parentheses, escapes) outside the markers.
  const wrap = (marker, content) => {
    const match = content.match(/^(\s*)([\p{P}\p{S}]*)([\s\S]*?)([\p{P}\p{S}]*)(\s*)$/u);
    return match[3] ? `${match[1]}${match[2]}${marker}${match[3]}${marker}${match[4]}${match[5]}` : content;
  };
  // A <br> with nothing visible after it is the browser's placeholder for an
  // empty or just-split line, not a hard break.
  const trailingBreak = br => {
    for (let node = br.nextSibling; node; node = node.nextSibling) {
      if (node.nodeType === Node.TEXT_NODE ? node.nodeValue.trim() : node.nodeType === Node.ELEMENT_NODE && node.tagName !== 'BR') return false;
    }
    return true;
  };
  // ``singleLine`` holds for headings and table cells, where Markdown has no hard break.
  const inline = (node, singleLine = false) => [...node.childNodes].map(child => {
    if (child.nodeType === Node.TEXT_NODE) return escapeText(child.nodeValue.replace(/\s*\n\s*/g, ' '));
    if (child.nodeType !== Node.ELEMENT_NODE) return '';
    switch (child.tagName) {
      case 'STRONG': case 'B': return wrap('**', inline(child, singleLine));
      case 'EM': case 'I': return wrap('*', inline(child, singleLine));
      case 'DEL': case 'S': case 'STRIKE': return wrap('~~', inline(child, singleLine));
      case 'CODE': {
        const text = child.textContent, fence = text.includes('`') ? '``' : '`';
        return text ? `${fence}${fence.length > 1 ? ' ' : ''}${text}${fence.length > 1 ? ' ' : ''}${fence}` : '';
      }
      case 'A': {
        const label = inline(child, singleLine), href = child.getAttribute('href') || '', title = child.getAttribute('title');
        const target = href.replace(/[()\s<>]/g, encodeURIComponent) + (title ? ` "${title.replace(/["\\]/g, '\\$&')}"` : '');
        return href ? `[${label}](${target})` : label;
      }
      case 'IMG': return `![${escapeText(child.getAttribute('alt') || '')}](${child.getAttribute('src') || ''})`;
      case 'BR': return trailingBreak(child) ? '' : singleLine ? ' ' : '\\\n';
      case 'INPUT': return '';
      default: return BLOCK_TAGS.has(child.tagName) ? ` ${inline(child, singleLine)} ` : inline(child, singleLine);
    }
  }).join('');
  // Text that would start a different block (a heading, list, quote, rule)
  // keeps its literal meaning.
  const guardLineStart = text => text.replace(
    /^(\s*)(?:(\d+)([.)])(?=\s)|(#{1,6}(?=\s|$)|[-+*](?=\s)|>|={3,}|-{3,}))/gm,
    (_match, space, digits, delimiter, marker) => (digits ? `${space}${digits}\\${delimiter}` : `${space}\\${marker}`),
  );
  const indent = (text, prefix) => text.split('\n').map(line => (line ? prefix + line : line)).join('\n');
  const listMarkdown = list => {
    let number = Number(list.getAttribute('start') || 1);
    return [...list.children].filter(item => item.tagName === 'LI').map(item => {
      const marker = list.tagName === 'OL' ? `${number++}. ` : '- ';
      const checkbox = item.querySelector(':scope > input[type="checkbox"], :scope > p > input[type="checkbox"]');
      const loose = [...item.children].some(child => child.tagName === 'P');
      const parts = [];
      let text = '';
      const flush = () => { if (text.trim()) parts.push(guardLineStart(text.trim())); text = ''; };
      [...item.childNodes].forEach(child => {
        if (child.nodeType === Node.ELEMENT_NODE && (child.tagName === 'UL' || child.tagName === 'OL')) { flush(); parts.push(listMarkdown(child)); }
        else if (child.nodeType === Node.ELEMENT_NODE && child.tagName === 'P') { flush(); parts.push(guardLineStart(inline(child).trim())); }
        else if (child.nodeType === Node.ELEMENT_NODE && BLOCK_TAGS.has(child.tagName)) { flush(); parts.push(blockMarkdown(child)); }
        else text += inline({ childNodes: [child] });
      });
      flush();
      const task = checkbox ? (checkbox.checked ? '[x] ' : '[ ] ') : '';
      const [first = '', ...rest] = parts.filter(part => part.trim());
      const pad = ' '.repeat(marker.length);
      return [`${marker}${task}${indent(first, pad).trimStart()}`, ...rest.map(part => indent(part, pad))].join(loose ? '\n\n' : '\n');
    }).join('\n');
  };
  const tableMarkdown = table => {
    const rowElements = [...table.querySelectorAll('tr')];
    const rows = rowElements.map(row => [...row.children].map(cell => inline(cell, true).trim().replace(/\|/g, '\\|')));
    if (!rows.length) return '';
    const width = Math.max(...rows.map(row => row.length));
    const header = [...rowElements[0].children];
    const rule = Array.from({ length: width }, (_, index) => {
      const align = header[index]?.getAttribute('align') || header[index]?.style.textAlign || '';
      return { left: ':---', center: ':---:', right: '---:' }[align] || '---';
    });
    const line = row => `| ${Array.from({ length: width }, (_, index) => row[index] || '').join(' | ')} |`;
    return [line(rows[0]), `| ${rule.join(' | ')} |`, ...rows.slice(1).map(line)].join('\n');
  };
  function blockMarkdown(element) {
    const tag = element.tagName;
    if (/^H[1-6]$/.test(tag)) {
      const text = inline(element, true).trim();
      return text ? `${'#'.repeat(Number(tag[1]))} ${text}` : '';
    }
    if (tag === 'UL' || tag === 'OL') return listMarkdown(element);
    if (tag === 'BLOCKQUOTE') return indent(childBlocksMarkdown(element), '> ').replace(/^$/gm, '>');
    if (tag === 'PRE') {
      const code = element.textContent.replace(/\n$/, ''), fence = code.includes('```') ? '~~~' : '```';
      const language = (element.querySelector('code')?.className.match(/(?:^|\s)language-(\S+)/) || [])[1] || '';
      return `${fence}${language}\n${code}\n${fence}`;
    }
    if (tag === 'HR') return '---';
    if (tag === 'TABLE') return tableMarkdown(element);
    if (tag === 'DIV' && [...element.children].some(child => BLOCK_TAGS.has(child.tagName))) return childBlocksMarkdown(element);
    return guardLineStart(inline(element).trim());
  }
  // Loose text and inline elements directly under a container form paragraphs.
  function childBlocksMarkdown(container) {
    const blocks = [];
    let loose = [];
    const flush = () => {
      const text = inline({ childNodes: loose }).trim();
      if (text) blocks.push(guardLineStart(text));
      loose = [];
    };
    [...container.childNodes].forEach(child => {
      if (child.nodeType === Node.ELEMENT_NODE && BLOCK_TAGS.has(child.tagName)) { flush(); blocks.push(blockMarkdown(child)); }
      else loose.push(child);
    });
    flush();
    return blocks.filter(block => block.trim()).join('\n\n');
  }

  /* ---------- In-place editor ---------- */

  const editors = new Map();
  const panelOf = body => body.closest('.note-panel');

  const createEditor = body => {
    const id = body.dataset.noteEditable;
    const panel = panelOf(body);
    const form = panel.querySelector('[data-workspace-note-form]');
    const status = panel.querySelector('[data-note-save-state]');
    const sources = new WeakMap();
    let version = Number(body.dataset.noteVersion);
    // The exact stored source: a <textarea> would drop a leading newline and
    // shift every block's line range.
    let lastSaved = JSON.parse(panel.querySelector('script[data-note-markdown]')?.textContent || 'null') ?? form.elements.content_md.value;
    let timer = 0, saving = null, dirty = false, conflict = false, failed = false;

    const lines = lastSaved.split('\n');
    const covered = new Set();
    [...body.children].forEach(element => {
      const range = (element.dataset.md || element.firstElementChild?.dataset.md || '').split(':').map(Number);
      if (range.length !== 2 || !range.every(Number.isInteger)) return;
      for (let line = range[0]; line < range[1]; line += 1) covered.add(line);
      sources.set(element, { html: element.outerHTML, md: lines.slice(range[0], range[1]).join('\n').replace(/\s+$/, '') });
    });
    // Source that renders no block of its own (link reference definitions)
    // is kept verbatim at the end, where it still resolves.
    const unrendered = lines.filter((line, index) => !covered.has(index) && line.trim()).join('\n');

    const serialize = () => {
      const blocks = [];
      let loose = [];
      const flushLoose = () => {
        const text = inline({ childNodes: loose }).trim();
        if (text) blocks.push({ md: guardLineStart(text) });
        loose = [];
      };
      [...body.childNodes].forEach(node => {
        if (node.nodeType !== Node.ELEMENT_NODE || !BLOCK_TAGS.has(node.tagName)) { loose.push(node); return; }
        flushLoose();
        const known = sources.get(node);
        blocks.push({ element: node, md: known && known.html === node.outerHTML ? known.md : blockMarkdown(node) });
      });
      flushLoose();
      return blocks.filter(block => block.md.trim());
    };
    const contentOf = blocks => [...blocks.map(block => block.md), ...(unrendered ? [unrendered] : [])].join('\n\n');
    const request = (content, baseVersion, keepalive = false) => fetch(`/api/notes/${id}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ title: form.elements.title.value, content_md: content, base_version: baseVersion }),
      keepalive,
      credentials: 'same-origin',
    });

    const setStatus = (text, state = '') => {
      if (!status) return;
      status.textContent = text;
      status.dataset.state = state;
    };
    const syncChrome = () => {
      body.dataset.noteVersion = String(version);
      form.elements.base_version.value = String(version);
      form.elements.content_md.value = lastSaved;
      form.dataset.originalContent = lastSaved;
      const chip = panel.querySelector('[data-user-note-history] + .chip');
      if (chip) chip.textContent = `v${version}`;
      const history = panel.querySelector('[data-user-note-history]');
      if (history) history.dataset.noteVersion = String(version);
    };

    const save = async () => {
      clearTimeout(timer);
      if (saving) { await saving; if (!dirty) return; }
      if (!dirty || conflict) return;
      const blocks = serialize();
      const content = contentOf(blocks);
      dirty = false;
      if (content === lastSaved) { setStatus(tr('Saved'), 'saved'); return; }
      setStatus(tr('Saving…'), 'saving');
      saving = (async () => {
        try {
          const response = await request(content, version);
          const result = await response.json().catch(() => ({}));
          if (response.ok) {
            version = Number(result.version ?? version + 1);
            lastSaved = content;
            blocks.forEach(block => { if (block.element) sources.set(block.element, { html: block.element.outerHTML, md: block.md }); });
            syncChrome();
            setStatus(dirty ? tr('Unsaved changes') : tr('Saved'), dirty ? 'dirty' : 'saved');
            return;
          }
          if (response.status === 409) {
            conflict = true;
            body.contentEditable = 'false';
            setStatus(tr('This note changed elsewhere. Reload to see the latest version.'), 'error');
            return;
          }
          throw new Error(typeof result.detail === 'string' ? result.detail : tr('Could not save'));
        } catch (error) {
          dirty = true;
          failed = true;
          setStatus(tr('Could not save. Retrying…'), 'error');
        }
      })();
      await saving;
      saving = null;
      if (dirty && !conflict && !signal.aborted) schedule(failed ? 5000 : 1200);
      failed = false;
    };
    const schedule = (delay = 1200) => { clearTimeout(timer); timer = setTimeout(() => save(), delay); };
    const changed = () => {
      if (conflict) return;
      dirty = true;
      setStatus(tr('Unsaved changes'), 'dirty');
      schedule();
    };

    body.contentEditable = 'true';
    body.setAttribute('role', 'textbox');
    body.setAttribute('aria-multiline', 'true');
    body.spellcheck = true;
    on(body, 'focus', () => { try { document.execCommand('defaultParagraphSeparator', false, 'p'); } catch (_error) { /* unsupported */ } });
    on(body, 'input', changed);
    on(body, 'blur', () => save());
    on(body, 'keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); body.blur(); }
      else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
    });
    // Pasted rich text would bring foreign markup and styles; keep plain text.
    on(body, 'paste', event => {
      const text = event.clipboardData?.getData('text/plain');
      if (text == null) return;
      event.preventDefault();
      document.execCommand('insertText', false, text);
    });
    on(body, 'drop', event => event.preventDefault());
    // Links stay usable: a plain click follows them, as on a read-only page.
    on(body, 'click', event => {
      const link = event.target.closest('a[href]');
      if (!link || window.getSelection()?.toString()) return;
      event.preventDefault();
      if (event.metaKey || event.ctrlKey) window.open(link.href, '_blank', 'noopener');
      else location.href = link.href;
    });
    // Leaving the page must not lose the last keystrokes. Ask first while a
    // save is outstanding; once the page is really going, send what is on it.
    on(window, 'beforeunload', event => {
      if (!dirty && !saving) return;
      event.preventDefault();
      save();
    });
    on(window, 'pagehide', () => {
      if (conflict || (!dirty && !saving)) return;
      const content = contentOf(serialize());
      if (content === lastSaved) return;
      // An in-flight save will have advanced the version by the time this lands.
      const baseVersion = saving ? version + 1 : version;
      // Browsers cap keepalive bodies at 64 KiB; the beforeunload prompt covers larger notes.
      if (new Blob([content]).size < 60_000) request(content, baseVersion, true).catch(() => {});
    });

    const editor = {
      body,
      changed,
      flush: async () => { if (dirty || saving) await save(); },
      get busy() { return dirty || Boolean(saving); },
    };
    editors.set(body, editor);
    return editor;
  };

  document.querySelectorAll('[data-note-editable]').forEach(createEditor);

  // Markdown source editing reads the form, so finish any in-place save first.
  on(document, 'click', async event => {
    const button = event.target.closest('[data-note-edit]');
    const editor = button && editors.get(panelOf(button)?.querySelector('[data-note-editable]'));
    if (!editor?.busy) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    await editor.flush();
    // A failing save keeps its error visible instead of opening stale source.
    if (!editor.busy) button.click();
  }, { capture: true });

  const focusId = new URLSearchParams(location.search).get('focus_note');
  if (focusId) {
    const body = document.querySelector(`[data-note-editable="${CSS.escape(focusId)}"]`);
    if (body) requestAnimationFrame(() => {
      body.focus({ preventScroll: true });
      const selection = window.getSelection(), range = document.createRange();
      range.selectNodeContents(body.firstElementChild || body);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
    });
    const clean = new URL(location.href);
    clean.searchParams.delete('focus_note');
    history.replaceState(history.state, '', clean);
  }

  /* ---------- Find and replace ---------- */

  const highlights = typeof Highlight === 'function' && CSS.highlights ? CSS.highlights : null;
  const noteBody = panel => panel?.querySelector('[data-workspace-note-body], [data-generated-note-prose]');
  let active = null;

  const textIndex = body => {
    const nodes = [], walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    const blockOf = node => {
      let element = node.parentElement;
      while (element && element !== body && !BLOCK_TAGS.has(element.tagName) && !['LI', 'TD', 'TH'].includes(element.tagName)) element = element.parentElement;
      return element;
    };
    let text = '', previousBlock = null;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      // A line break between blocks keeps a match from spanning two of them.
      const block = blockOf(node);
      if (text && block !== previousBlock) text += '\n';
      previousBlock = block;
      nodes.push({ node, start: text.length });
      text += node.nodeValue;
    }
    const locate = offset => {
      let low = 0, high = nodes.length - 1;
      while (low < high) {
        const middle = (low + high + 1) >> 1;
        if (nodes[middle].start <= offset) low = middle; else high = middle - 1;
      }
      return nodes[low];
    };
    return { text, locate };
  };

  const clearHighlights = () => { highlights?.delete('note-find'); highlights?.delete('note-find-current'); };

  const openFind = panel => {
    const body = noteBody(panel);
    if (!body) return;
    if (active?.panel === panel) { active.input.focus({ preventScroll: true }); active.input.select(); active.bar.scrollIntoView({ block: 'nearest' }); return; }
    closeFind();
    const editor = editors.get(body);
    const bar = document.createElement('div');
    bar.className = 'note-find';
    bar.setAttribute('role', 'search');
    bar.innerHTML = `
      <div class="note-find-row">
        <input class="search-input" type="search" data-find-query>
        <span class="sub note-find-count" aria-live="polite"></span>
        <button class="icon-action icon-only" type="button" data-find-step="-1">↑</button>
        <button class="icon-action icon-only" type="button" data-find-step="1">↓</button>
        <button class="icon-action icon-only" type="button" data-find-close>✕</button>
      </div>
      ${editor ? `<div class="note-find-row">
        <input class="search-input" type="text" data-find-replacement>
        <button class="btn sec" type="button" data-find-replace-one></button>
        <button class="btn sec" type="button" data-find-replace-all></button>
        <label class="sub note-find-case"><input type="checkbox" data-find-case> <span></span></label>
      </div>` : ''}`;
    const input = bar.querySelector('[data-find-query]');
    input.placeholder = tr('Find in note');
    input.setAttribute('aria-label', tr('Find in note'));
    bar.querySelector('[data-find-step="-1"]').setAttribute('aria-label', tr('Previous match'));
    bar.querySelector('[data-find-step="1"]').setAttribute('aria-label', tr('Next match'));
    bar.querySelector('[data-find-close]').setAttribute('aria-label', tr('Close'));
    const replacement = bar.querySelector('[data-find-replacement]');
    if (replacement) {
      replacement.placeholder = tr('Replace with');
      replacement.setAttribute('aria-label', tr('Replace with'));
      bar.querySelector('[data-find-replace-one]').textContent = tr('Replace');
      bar.querySelector('[data-find-replace-all]').textContent = tr('Replace all');
      bar.querySelector('.note-find-case span').textContent = tr('Match case');
    }
    const count = bar.querySelector('.note-find-count');
    body.before(bar);
    const toggle = panel.querySelector('[data-note-find]');
    toggle?.setAttribute('aria-expanded', 'true');
    const state = { panel, body, bar, input, matches: [], starts: [], position: -1 };
    active = state;

    const caseSensitive = () => Boolean(bar.querySelector('[data-find-case]')?.checked);
    const paint = () => {
      if (!highlights) return;
      clearHighlights();
      if (!state.matches.length) return;
      highlights.set('note-find', new Highlight(...state.matches));
      if (state.position >= 0) highlights.set('note-find-current', new Highlight(state.matches[state.position]));
    };
    const refresh = ({ keepPosition = false } = {}) => {
      const query = input.value;
      const { text, locate } = textIndex(body);
      state.matches = [];
      state.starts = [];
      if (query) {
        const haystack = caseSensitive() ? text : text.toLocaleLowerCase();
        const needle = caseSensitive() ? query : query.toLocaleLowerCase();
        for (let index = haystack.indexOf(needle); index >= 0; index = haystack.indexOf(needle, index + needle.length)) {
          const start = locate(index), end = locate(index + needle.length - 1);
          const range = document.createRange();
          range.setStart(start.node, index - start.start);
          range.setEnd(end.node, index + needle.length - end.start);
          state.matches.push(range);
          state.starts.push(index);
        }
      }
      if (!keepPosition || state.position >= state.matches.length) state.position = state.matches.length ? 0 : -1;
      count.textContent = !query ? '' : state.matches.length ? `${state.position + 1} / ${state.matches.length}` : tr('No results');
      paint();
    };
    const reveal = () => {
      const range = state.matches[state.position];
      if (!range) return;
      const rect = range.getBoundingClientRect();
      if (rect.top < 80 || rect.bottom > window.innerHeight - 40) range.startContainer.parentElement?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      if (!highlights) { const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); }
    };
    const step = delta => {
      if (!state.matches.length) return;
      state.position = (state.position + delta + state.matches.length) % state.matches.length;
      count.textContent = `${state.position + 1} / ${state.matches.length}`;
      paint();
      reveal();
    };
    const replaceRanges = ranges => {
      const value = replacement.value;
      // Last first, so earlier ranges keep valid offsets.
      [...ranges].reverse().forEach(range => {
        range.deleteContents();
        if (value) range.insertNode(document.createTextNode(value));
      });
      body.normalize();
      editor.changed();
    };

    on(input, 'input', () => { refresh(); reveal(); });
    on(input, 'keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); step(event.shiftKey ? -1 : 1); }
      else if (event.key === 'Escape') { event.preventDefault(); closeFind(true); }
    });
    bar.querySelectorAll('[data-find-step]').forEach(button => on(button, 'click', () => step(Number(button.dataset.findStep))));
    on(bar.querySelector('[data-find-close]'), 'click', () => closeFind(true));
    on(bar.querySelector('[data-find-case]'), 'change', () => refresh());
    on(replacement, 'keydown', event => { if (event.key === 'Escape') { event.preventDefault(); closeFind(true); } });
    on(bar.querySelector('[data-find-replace-one]'), 'click', () => {
      const range = state.matches[state.position];
      if (!range) return;
      // Continue after the inserted text, even if it contains the query.
      const resume = state.starts[state.position] + replacement.value.length;
      replaceRanges([range]);
      refresh();
      const next = state.starts.findIndex(start => start >= resume);
      state.position = state.matches.length ? Math.max(0, next) : -1;
      if (state.matches.length) count.textContent = `${state.position + 1} / ${state.matches.length}`;
      paint();
      reveal();
    });
    on(bar.querySelector('[data-find-replace-all]'), 'click', () => {
      const total = state.matches.length;
      if (!total) return;
      replaceRanges(state.matches);
      refresh();
      count.textContent = `${tr('Replaced')} ${total}`;
    });
    if (editor) on(body, 'input', () => { if (active === state) refresh({ keepPosition: true }); });

    const selected = window.getSelection()?.toString();
    if (selected && !selected.includes('\n') && body.contains(window.getSelection().anchorNode)) input.value = selected;
    refresh();
    input.focus({ preventScroll: true });
    input.select();
    bar.scrollIntoView({ block: 'nearest' });
  };

  function closeFind(restoreFocus = false) {
    if (!active) return;
    const { panel, bar } = active;
    bar.remove();
    clearHighlights();
    const toggle = panel.querySelector('[data-note-find]');
    toggle?.setAttribute('aria-expanded', 'false');
    active = null;
    if (restoreFocus) toggle?.focus();
  }

  on(document, 'click', event => {
    const button = event.target.closest('[data-note-find]');
    if (!button) return;
    const panel = button.closest('.note-panel');
    if (active?.panel === panel) closeFind(true); else openFind(panel);
  });
  const visibleNotePanel = () => {
    const panel = [...document.querySelectorAll('.note-panel:not([hidden])')].find(item => item.offsetParent !== null);
    return panel && noteBody(panel) && !noteBody(panel).hidden ? panel : null;
  };
  // Ctrl/⌘ F searches the note being read; the transcript keeps its own find.
  on(document, 'keydown', event => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'f' || event.shiftKey || event.altKey) return;
    const panel = visibleNotePanel();
    if (!panel || document.activeElement?.closest('#recording-panel-transcript')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    openFind(panel);
  }, { capture: true });
  // Switching notes or tabs hides the panel; drop its stale highlights.
  const observer = new MutationObserver(() => { if (active && (active.panel.hidden || active.panel.offsetParent === null)) closeFind(); });
  document.querySelectorAll('.note-panel').forEach(panel => observer.observe(panel, { attributes: true, attributeFilter: ['hidden'] }));
  const notesPanel = document.getElementById('recording-panel-notes');
  if (notesPanel) observer.observe(notesPanel, { attributes: true, attributeFilter: ['hidden', 'class'] });
  signal.addEventListener('abort', () => { observer.disconnect(); clearHighlights(); });
})();
