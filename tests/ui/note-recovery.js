/* Browser regressions against the production scripts, with a closed fake API. */
(() => {
  'use strict';
  const root = document.getElementById('app-view');
  const results = document.getElementById('results');
  const source = '../../src/localplaud/api/static/js/';
  const original = '## Synthetic planning\n\nOriginal generated text.';
  const savedContent = '## Someone else’s saved copy\n\nKeep this exact content.\n';
  let calls = [], toasts = [], sourceOpens = 0, confirmCalls = 0, errors = [];
  let confirmDiscard = false, navigationRequests = [];
  let saveReply, copyReply, compareReply, speechReply, queueReply;
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const response = (status, body) => new Response(typeof body === 'string' ? body : JSON.stringify(body), { status });
  const waitFor = async predicate => {
    const deadline = performance.now() + 3000;
    while (!predicate()) {
      if (performance.now() > deadline) throw new Error('Timed out waiting for fixture state');
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  const load = name => new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = source + name;
    script.onload = () => { script.remove(); resolve(); };
    script.onerror = reject;
    document.head.append(script);
  });
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  window.confirm = () => { confirmCalls += 1; return confirmDiscard; };
  window.localplaudT = message => message;
  window.lp = { toast: (message, options) => toasts.push({ message, ...options }), closeDialog: dialog => dialog.close() };
  // A generated 0.1-second silent WAV, never user media.
  const wav = new Uint8Array(1644), view = new DataView(wav.buffer);
  const ascii = (offset, text) => [...text].forEach((character, index) => { wav[offset + index] = character.charCodeAt(0); });
  ascii(0, 'RIFF'); view.setUint32(4, 1636, true); ascii(8, 'WAVEfmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 8000, true); view.setUint32(28, 16000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ascii(36, 'data'); view.setUint32(40, 1600, true);
  document.querySelector('audio').src = 'data:audio/wav;base64,' + btoa(String.fromCharCode(...wav));

  window.fetch = async (input, options = {}) => {
    const url = String(input), method = options.method || 'GET';
    calls.push({ url, method, ...(options.body ? { body: JSON.parse(options.body) } : {}) });
    if (url.endsWith('/editable-copy')) return copyReply();
    if (url === '/api/notes/42' && method === 'PUT') return saveReply();
    if (url === '/api/notes?file_id=synthetic') return compareReply();
    if (url.endsWith('/speech-settings')) return speechReply();
    if (url === '/file/synthetic/reprocess?force=true') return queueReply();
    throw new Error(`Fixture blocked unexpected request: ${method} ${url}`);
  };

  // Run the vendored HTMX request/swap lifecycle, with manually ordered replies.
  // No XHR can reach even the static server: only this synthetic route is valid.
  window.XMLHttpRequest = class extends EventTarget {
    constructor() { super(); this.upload = new EventTarget(); }
    open(method, url) {
      check(method === 'GET' && new URL(url, location.href).pathname === '/fixture/navigation', `Fixture blocked XHR: ${method} ${url}`);
      this.responseURL = new URL(url, location.href).href;
    }
    overrideMimeType() {}
    setRequestHeader() {}
    getAllResponseHeaders() { return ''; }
    getResponseHeader() { return null; }
    send() { navigationRequests.push(this); }
    abort() { this.onabort?.(); }
    respond() {
      this.status = 200;
      this.response = this.responseText = '<p data-navigation-arrived>Another synthetic page</p>';
      this.onload();
    }
  };
  const navigate = (target = root) => {
    const source = document.createElement('button');
    source.hidden = true;
    document.querySelector('.fixture-controls').append(source);
    return window.htmx.ajax('GET', '/fixture/navigation', { source, target, swap: 'innerHTML' })
      .finally(() => source.remove());
  };
  const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
  };
  const conflictReply = generated => generated
    ? response(201, { id: 42, version: 7, content_md: savedContent })
    : response(409, { detail: { code: 'note_changed', current_version: 8 } });
  const holdSave = generated => {
    const pending = deferred();
    if (generated) copyReply = () => pending.promise; else saveReply = () => pending.promise;
    return pending;
  };

  async function reset({ generated = false } = {}) {
    window.localplaudNoteEditorTeardown?.();
    window.localplaudWorkspaceTeardown?.();
    calls = []; toasts = []; sourceOpens = 0; confirmCalls = 0; errors = [];
    confirmDiscard = false; navigationRequests = [];
    saveReply = () => response(409, { detail: { code: 'note_changed', current_version: 8 } });
    copyReply = () => response(201, { id: 42, version: 7, title: 'Existing copy', content_md: savedContent });
    compareReply = () => response(200, { notes: [{ id: 42, version: 8, content_md: savedContent }] });
    speechReply = () => response(200, { language: 'zh' });
    queueReply = () => response(503, '<h1>synthetic worker unavailable</h1>');
    root.innerHTML = `
      <script id="ws-config" type="application/json">{"fileId":"synthetic","title":"Synthetic planning","revision":4}<\/script>
      <div class="fixture-workspace">
        <div class="tabs"><button data-note-target="${generated ? 'sum-12' : 'saved-42'}"><span class="note-tab-label">Synthetic planning</span></button></div>
        <article class="note-panel" data-note-panel="${generated ? 'sum-12' : 'saved-42'}">
          <div class="saved-note-head"><h2>Synthetic planning</h2><span data-note-save-state role="status" aria-live="polite"></span></div>
          <button type="button" data-note-find>Find in note</button>
          ${generated ? '' : '<button type="button" data-note-edit="42">Edit Markdown</button>'}
          <div class="md note-editable" ${generated ? 'data-summary-editable="12" data-generated-note-prose' : 'data-note-editable="42" data-note-version="3" data-workspace-note-body'} aria-label="Synthetic note"><h2 data-md="0:1">Synthetic planning</h2><p data-md="2:3">Original generated text.</p></div>
          <script type="application/json" data-note-markdown>${JSON.stringify(original)}<\/script>
          ${generated ? '' : `<form hidden data-workspace-note-form="42"><input name="title" value="Synthetic planning"><input name="base_version" value="3"><textarea name="content_md"></textarea></form>`}
        </article>
        <p id="reprocess-msg" role="status"></p>
        <button id="menu-speech-settings">Speech settings</button>
      </div>
      <dialog id="speech-settings-dialog" aria-label="Speech settings">
        <form id="speech-settings-form" data-file-id="synthetic">
          <label>Language <select name="language"><option value="zh">Mandarin</option><option value="">Automatic</option></select></label>
          <input type="hidden" name="speaker_mode" value="auto">
          <span id="speech-settings-status" role="status" aria-live="polite"></span>
          <button class="btn" type="submit" value="rebuild">Save and rebuild</button>
          <button class="btn sec" type="submit" value="save">Save for next rebuild</button>
        </form>
      </dialog>`;
    root.querySelector('textarea[name="content_md"]')?.append(document.createTextNode(original));
    root.querySelector('[data-note-edit]')?.addEventListener('click', () => { sourceOpens += 1; });
    await load('workspace.js');
    await load('note-editor.js');
    check(!errors.length, errors.join('\n'));
  }
  const body = () => root.querySelector('.note-editable');
  const draftArea = () => root.querySelector('[data-note-draft]');
  function type(text) {
    const paragraph = body().querySelector('p');
    paragraph.textContent = text;
    body().dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    return `## Synthetic planning\n\n${text}`;
  }
  const save = () => body().dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 's', ctrlKey: true, cancelable: true }));
  const button = label => [...root.querySelectorAll('button')].find(item => item.textContent === label);
  const writes = () => calls.filter(call => call.method === 'PUT' && call.url === '/api/notes/42');
  const conflicted = () => Boolean(draftArea());
  async function conflict({ generated = false } = {}) {
    await reset({ generated });
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    if (generated) copyReply = () => pending; else saveReply = () => pending;
    const submitted = type('First typed draft');
    save();
    await waitFor(() => calls.length);
    type('最後一筆 Unicode 🙂 and literal <script>alert(1)</script> ' + 'long-content-'.repeat(70));
    // The rich-text serializer escapes literal angle brackets in Markdown.
    const exact = '## Synthetic planning\n\n最後一筆 Unicode 🙂 and literal \\<script>alert(1)\\</script> ' + 'long-content-'.repeat(70);
    const html = body().innerHTML;
    release(generated ? response(201, { id: 42, version: 7, content_md: savedContent }) : response(409, { detail: { code: 'note_changed', current_version: 8 } }));
    await waitFor(conflicted);
    check(draftArea().value === exact, 'Recovery dropped keystrokes typed during save');
    check(body().innerHTML === html, 'Conflict changed the draft DOM');
    check(body().getAttribute('aria-readonly') === 'true', 'Conflict must be read-only');
    check(generated ? writes().length === 0 : writes()[0].body.content_md === submitted, 'Unexpected write to existing copy');
    if (!generated) check(writes()[0].body.base_version === 3, 'Optimistic version lost');
    const count = calls.length;
    save(); window.dispatchEvent(new Event('pagehide'));
    await new Promise(resolve => setTimeout(resolve, 30));
    check(calls.length === count, 'Conflict attempted another save/keepalive');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    check(unload.defaultPrevented, 'Conflict draft missing unload protection');
    const navigation = new CustomEvent('htmx:beforeRequest', { bubbles: true, cancelable: true, detail: { target: root } });
    root.dispatchEvent(navigation);
    check(navigation.defaultPrevented && confirmCalls === 1, 'HTMX navigation discarded conflict draft');
    button('Edit Markdown')?.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    check(sourceOpens === 0, 'Stale Markdown editor opened over conflict');
    const result = await window.localplaudNoteSearch.replaceAll('Unicode', 'LOST');
    check(result.replaced === 0 && result.failed.length === 1 && draftArea().value === exact && body().innerHTML === html, 'Find/replace mutated blocked draft');
    button('Find in note').click();
    check(!root.querySelector('[data-find-replacement]'), 'Blocked draft still offers replacement');
    const latest = root.querySelector('.note-draft-actions a');
    check(latest.target === '_blank' && latest.rel === 'noopener' && latest.search.includes('note_id=42'), 'Recovery link must keep the draft page open');
    return exact;
  }

  const cases = [
    ['409 retains exact latest draft and fences all writes/navigation', () => conflict()],
    ['ExistingCopy retains exact latest draft without a PUT', () => conflict({ generated: true })],
    ...[false, true].flatMap(generated => {
      const kind = generated ? 'editable-copy' : 'PUT';
      return [
        [`Pending ${kind} navigation requires explicit discard before sending`, async () => {
          await reset({ generated });
          const pending = holdSave(generated);
          const exact = type('Keep the pending draft'); save(); await waitFor(() => calls.length);
          const navigation = navigate();
          check(confirmCalls === 1 && navigationRequests.length === 0, 'Navigation escaped while save was pending');
          await navigation;
          pending.resolve(conflictReply(generated)); await waitFor(conflicted);
          check(draftArea().value === exact, 'Pending navigation lost recovery');
        }],
        [`${kind} conflict before an earlier navigation reply keeps recovery mounted`, async () => {
          await reset({ generated });
          const navigation = navigate();
          check(navigationRequests.length === 1 && confirmCalls === 0, 'Clean navigation did not start');
          const pending = holdSave(generated);
          type('First pending text'); save(); await waitFor(() => calls.length);
          const exact = type('Late typing 最後 🙂');
          pending.resolve(conflictReply(generated)); await waitFor(conflicted);
          const retainedBody = body();
          navigationRequests[0].respond(); await navigation;
          check(confirmCalls === 1 && body() === retainedBody && draftArea()?.value === exact, 'Late navigation swap removed conflict recovery');
          check(!root.querySelector('[data-navigation-arrived]'), 'Navigation response replaced the draft');
          check(generated ? writes().length === 0 : writes().length === 1, 'Unexpected overwrite after conflict');
        }],
        [`Navigation reply before pending ${kind} resolution keeps the draft`, async () => {
          await reset({ generated });
          const navigation = navigate(), pending = holdSave(generated);
          const exact = type('Still saving when navigation returns'); save(); await waitFor(() => calls.length);
          navigationRequests[0].respond(); await navigation;
          check(confirmCalls === 1 && body(), 'Swap discarded an unresolved save');
          pending.resolve(conflictReply(generated)); await waitFor(conflicted);
          check(draftArea().value === exact, 'Recovery lost after pending-swap cancellation');
        }],
        [`Explicit discard during pending ${kind} permits only that navigation`, async () => {
          await reset({ generated });
          const pending = holdSave(generated);
          type('Explicitly discarded draft'); save(); await waitFor(() => calls.length);
          confirmDiscard = true;
          const navigation = navigate();
          check(navigationRequests.length === 1 && confirmCalls === 1, 'Explicit discard prevented navigation');
          pending.resolve(conflictReply(generated)); await waitFor(conflicted);
          confirmDiscard = false;
          const fragment = navigate(results);
          // An unrelated fragment request cannot consume/inherit discard approval.
          check(navigationRequests.length === 2 && confirmCalls === 1, 'Unrelated fragment prompted discard');
          navigationRequests[1].respond(); await fragment;
          await navigate();
          check(navigationRequests.length === 2 && confirmCalls === 2, 'Discard approval leaked to another request');
          navigationRequests[0].respond(); await navigation;
          check(root.querySelector('[data-navigation-arrived]') && confirmCalls === 2, 'Approved request prompted twice or failed to navigate');
        }],
      ];
    }),
    ['Successful pending save permits an earlier navigation response without a prompt', async () => {
      await reset();
      const navigation = navigate(), pending = holdSave(false);
      type('Saved before navigation returns'); save(); await waitFor(() => calls.length);
      pending.resolve(response(200, { version: 4 }));
      await waitFor(() => root.querySelector('[data-note-save-state]').dataset.state === 'saved');
      navigationRequests[0].respond(); await navigation;
      check(root.querySelector('[data-navigation-arrived]') && confirmCalls === 0, 'Successful save broke ordinary navigation');
    }],
    ['Explicit discard at a late conflict swap remains possible', async () => {
      await reset();
      const navigation = navigate(), pending = holdSave(false);
      type('Recover or discard'); save(); await waitFor(() => calls.length);
      pending.resolve(conflictReply(false)); await waitFor(conflicted);
      confirmDiscard = true;
      navigationRequests[0].respond(); await navigation;
      check(root.querySelector('[data-navigation-arrived]') && confirmCalls === 1, 'Late conflict cannot be explicitly discarded');
    }],
    ['Earlier revision restored to generated text still cannot be overwritten', async () => {
      await reset({ generated: true });
      copyReply = () => response(201, { id: 42, version: 4, content_md: original });
      const exact = type('Keep my new draft'); save(); await waitFor(conflicted);
      check(draftArea().value === exact && writes().length === 0, 'Revised existing copy overwritten');
    }],
    ['Compare loading, unavailable, missing, empty and latest saved content', async () => {
      const exact = await conflict();
      let release;
      compareReply = () => new Promise(resolve => { release = resolve; });
      const compare = button('Compare with saved note'); compare.click();
      check(compare.disabled && root.querySelector('[aria-busy="true"]'), 'Missing comparison loading state');
      release(response(503, { detail: 'unavailable' }));
      await waitFor(() => !compare.disabled);
      check(root.textContent.includes('Could not load the saved note') && draftArea().value === exact, 'Compare failure lost recovery');
      compareReply = () => response(200, { notes: [] }); compare.click(); await waitFor(() => !compare.disabled);
      check(root.textContent.includes('Could not load the saved note'), 'Missing note treated as success');
      compareReply = () => response(200, { notes: [{ id: 42, version: 9, content_md: '' }] });
      compare.click(); await waitFor(() => !compare.disabled);
      check(!root.querySelector('[data-note-saved]').closest('label').hidden && root.querySelector('[data-note-saved]').value === '', 'Empty saved note not compared');
      compareReply = () => response(200, { notes: [{ id: 42, version: 10, content_md: savedContent }] });
      compare.click(); await waitFor(() => !compare.disabled);
      check(root.querySelector('[data-note-saved]').value === savedContent && root.textContent.includes('v10'), 'Latest version not compared');
      check(draftArea().value === exact && root.querySelector('[name="base_version"]').value === '3', 'Comparison advanced optimistic version or replaced draft');
    }],
    ['Clipboard exact copy, denied fallback, download, accessible controls and mobile layout', async () => {
      const exact = await conflict({ generated: true });
      let copied;
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { copied = text; } } });
      button('Copy draft').click(); await waitFor(() => copied !== undefined);
      check(copied === exact, 'Clipboard content differs from draft');
      const oldExec = document.execCommand;
      navigator.clipboard.writeText = async () => { throw new Error('denied'); };
      document.execCommand = () => false;
      button('Copy draft').click(); await waitFor(() => root.textContent.includes('Copy failed.'));
      check(document.activeElement === draftArea() && draftArea().selectionEnd === exact.length, 'Clipboard denial must leave selected recoverable source');
      document.execCommand = oldExec;
      let downloaded;
      const oldURL = URL.createObjectURL;
      URL.createObjectURL = blob => { downloaded = blob; return oldURL(blob); };
      // Capture before navigation; no real fixture files are written.
      const intercept = event => { if (event.target.closest('a[download]')) event.preventDefault(); };
      root.addEventListener('click', intercept);
      button('Download draft').click();
      URL.createObjectURL = oldURL;
      root.removeEventListener('click', intercept);
      check(await downloaded.text() === exact, 'Downloaded draft differs from recovery');
      check(draftArea().readOnly && draftArea().labels[0].textContent.includes('Your unsaved draft'), 'Draft lacks accessible label/read-only state');
      check(document.documentElement.scrollWidth <= innerWidth + 1, 'Long draft causes horizontal overflow');
      check([...root.querySelectorAll('.note-draft-actions button')].every(item => item.getBoundingClientRect().height >= 44), 'Recovery touch targets too small');
    }],
    ['Normal save advances optimistic version and unchanged source', async () => {
      await reset(); saveReply = () => response(200, { version: 4 });
      const exact = type('Successfully saved'); save();
      await waitFor(() => root.querySelector('[data-note-save-state]').dataset.state === 'saved');
      check(writes()[0].body.base_version === 3 && writes()[0].body.content_md === exact, 'Wrong save source/version');
      check(root.querySelector('[name="base_version"]').value === '4' && root.querySelector('[name="content_md"]').value === exact, 'Saved chrome not synchronized');
      check(!conflicted(), 'Successful save showed conflict recovery');
    }],
    ['Generated copy starts at its returned version and saves one revision', async () => {
      await reset({ generated: true });
      copyReply = () => response(201, { id: 42, version: 1, content_md: original, title: 'Synthetic planning' });
      saveReply = () => response(200, { version: 2 });
      type('First safe edit'); save();
      await waitFor(() => root.querySelector('[data-note-save-state]').dataset.state === 'saved');
      check(writes().length === 1 && writes()[0].body.base_version === 1 && !conflicted(), 'New copy did not preserve optimistic version');
    }],
    ['Transient save error retains live text and can retry', async () => {
      await reset(); saveReply = () => response(503, {});
      const exact = type('Keep for retry'); save();
      await waitFor(() => root.querySelector('[data-note-save-state]').dataset.state === 'error');
      check(body().textContent.includes('Keep for retry') && !conflicted(), 'Transient failure discarded/froze draft');
      saveReply = () => response(200, { version: 4 }); save();
      await waitFor(() => root.querySelector('[data-note-save-state]').dataset.state === 'saved');
      check(writes()[1].body.base_version === 3 && writes()[1].body.content_md === exact, 'Retry changed source/base version');
    }],
    ...[409, 502, 503, 'network'].map(code => [`Rebuild ${code} keeps settings/dialog, gives no success and permits retry`, async () => {
      await reset();
      queueReply = () => { if (code === 'network') throw new TypeError('offline'); return response(code, '<h1>synthetic proxy failure</h1>'); };
      const dialog = root.querySelector('dialog'), form = root.querySelector('#speech-settings-form'); dialog.showModal();
      form.requestSubmit(form.querySelector('[value="rebuild"]'));
      await waitFor(() => !form.dataset.busy);
      check(dialog.open && toasts.length === 0, `Rebuild ${code} incorrectly toasted success or closed`);
      check(root.querySelector('#speech-settings-status').textContent.startsWith('Speech settings saved.'), 'Failure implies settings were not saved');
      check(!root.querySelector('#reprocess-msg h1'), 'Proxy HTML injected on failure');
      check(root.querySelector('#menu-speech-settings .chip'), 'Saved Custom chip missing');
      queueReply = () => response(200, '<span>synthetic queued</span>');
      form.requestSubmit(form.querySelector('[value="rebuild"]')); await waitFor(() => !form.dataset.busy);
      check(!dialog.open && toasts.length === 1 && toasts[0].message.includes('Rebuilding'), 'Successful retry not acknowledged');
    }]),
    ['Settings-only success does not rebuild; failed settings do not queue', async () => {
      await reset();
      const dialog = root.querySelector('dialog'), form = root.querySelector('#speech-settings-form'); dialog.showModal();
      speechReply = () => response(503, {});
      form.requestSubmit(form.querySelector('[value="rebuild"]')); await waitFor(() => !form.dataset.busy);
      check(calls.length === 1 && toasts.length === 0 && dialog.open, 'Settings failure queued work or toasted success');
      speechReply = () => response(200, {});
      form.requestSubmit(form.querySelector('[value="save"]')); await waitFor(() => !form.dataset.busy);
      check(calls.length === 2 && toasts[0].message.includes('next rebuild') && !dialog.open, 'Settings-only submission queued rebuild');
    }],
  ];

  window.runRegressions = async () => {
    const report = [];
    document.getElementById('run-tests').disabled = true;
    for (const [name, test] of cases) {
      try { await test(); check(!errors.length, errors.join('\n')); report.push({ name, passed: true }); }
      catch (error) { report.push({ name, passed: false, error: error.stack || String(error) }); }
      results.textContent = report.map(item => `${item.passed ? 'PASS' : 'FAIL'} ${item.name}${item.error ? '\n' + item.error : ''}`).join('\n');
    }
    // Leave a useful long-content comparison visible for desktop/mobile review.
    await conflict({ generated: true });
    button('Compare with saved note').click(); await waitFor(() => !button('Compare with saved note').disabled);
    window.regressionReport = { viewport: { width: innerWidth, height: innerHeight }, passed: report.filter(item => item.passed).length, total: report.length, report };
    document.getElementById('run-tests').disabled = false;
    return window.regressionReport;
  };
  window.resetRecoveryFixture = reset;
  document.getElementById('run-tests').onclick = () => window.runRegressions();
  document.getElementById('show-conflict').onclick = () => conflict({ generated: true });
  document.getElementById('show-speech').onclick = async () => {
    await reset(); const form = root.querySelector('#speech-settings-form');
    root.querySelector('dialog').showModal(); form.requestSubmit(form.querySelector('[value="rebuild"]'));
  };
  reset();
})();
