/* localplaud shared Web App runtime (owner: foundation).
 *
 * Loaded synchronously in <head> so `window.lp` exists before any inline page
 * script runs; DOM wiring happens on DOMContentLoaded and is delegated at the
 * document level, so it keeps working after HTMX swaps #app-view.
 *
 * Public API (see CONVENTIONS):
 *   lp.toast(message, {type:'info'|'success'|'error', timeout:ms, action:{label, onClick}})
 *   lp.openDialog(idOrElement, trigger?) / lp.closeDialog(idOrElement)
 *   lp.openMenu(button) / lp.closeMenus()
 *   lp.shortcut('g h', handler, {description}) — single keys or two-key sequences
 *   lp.setAppearance('light'|'dark'|'system')
 *   lp.t(message) — translation lookup (same as window.localplaudT)
 * Declarative hooks:
 *   [data-menu="menuId"] on a button toggles <div class="menu" id="menuId" role="menu" hidden>
 *   [data-dialog-open="dialogId"], [data-dialog-close] inside a <dialog class="dialog|sheet">
 *   [data-tooltip="text"] shows a tooltip on hover/focus for icon-only controls
 *   [data-toast="message"] shows a toast on click (handy for demos/tests)
 * Events: 'lp:dialog-open' / 'lp:dialog-close' (on the dialog), 'lp:navigated' (document)
 */
(() => {
  'use strict';
  const lp = window.lp = window.lp || {};
  lp.t = message => (window.localplaudT ? window.localplaudT(message) : message);
  const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const isTyping = target => !!target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));

  /* ------------------------------------------------------------- toasts */
  lp.toast = (message, {type = 'info', timeout = 4200, action = null} = {}) => {
    const region = document.getElementById('toast-region');
    if (!region) return null;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const iconName = type === 'error' ? 'circle-x' : type === 'success' ? 'circle-check' : 'info';
    toast.innerHTML = `<svg class="i" aria-hidden="true"><use href="#i-${iconName}"></use></svg><span></span>`;
    toast.querySelector('span').textContent = message;
    if (type === 'error') toast.setAttribute('role', 'alert');
    const dismiss = () => { toast.remove(); };
    if (action?.label) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = action.label;
      button.addEventListener('click', () => { try { action.onClick?.(); } finally { dismiss(); } });
      toast.append(button);
    }
    region.append(toast);
    if (timeout > 0) setTimeout(dismiss, timeout);
    return {dismiss};
  };

  /* ------------------------------------------------------------ dialogs */
  const dialogOpeners = new WeakMap();
  const resolve = target => (typeof target === 'string' ? document.getElementById(target) : target);
  lp.openDialog = (target, trigger) => {
    const dialog = resolve(target);
    if (!dialog || dialog.open) return dialog;
    dialogOpeners.set(dialog, trigger || document.activeElement);
    lp.closeMenus();
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    document.body.classList.add('dialog-open');
    const focusTarget = dialog.querySelector('[autofocus], input:not([type="hidden"]):not([disabled]), select, textarea, button:not([data-dialog-close])');
    requestAnimationFrame(() => focusTarget?.focus());
    dialog.dispatchEvent(new CustomEvent('lp:dialog-open', {bubbles: true}));
    return dialog;
  };
  lp.closeDialog = target => {
    const dialog = resolve(target);
    if (!dialog || !dialog.open) return;
    if (dialog.dataset.busy === 'true') return;
    dialog.close();
  };
  document.addEventListener('close', event => {
    const dialog = event.target;
    if (!(dialog instanceof HTMLDialogElement)) return;
    if (!document.querySelector('dialog[open]')) document.body.classList.remove('dialog-open');
    const opener = dialogOpeners.get(dialog);
    // A menu item inside a now-closed <details> menu cannot take focus; return
    // focus to that menu's summary instead of dropping it on <body>.
    if (opener?.isConnected) {
      opener.focus();
      if (document.activeElement !== opener) opener.closest('details')?.querySelector('summary')?.focus();
    }
    dialogOpeners.delete(dialog);
    dialog.dispatchEvent(new CustomEvent('lp:dialog-close', {bubbles: true}));
  }, true);
  document.addEventListener('cancel', event => {
    if (event.target instanceof HTMLDialogElement && event.target.dataset.busy === 'true') event.preventDefault();
  }, true);
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-dialog-open]');
    if (opener) { event.preventDefault(); lp.openDialog(opener.dataset.dialogOpen, opener); return; }
    const closer = event.target.closest('[data-dialog-close]');
    if (closer) { const dialog = closer.closest('dialog'); if (dialog) { event.preventDefault(); lp.closeDialog(dialog); } return; }
    // Backdrop click: a click whose target is the dialog element itself.
    if (event.target instanceof HTMLDialogElement && event.target.open && (event.target.classList.contains('dialog') || event.target.classList.contains('sheet'))) {
      const rect = event.target.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) lp.closeDialog(event.target);
    }
  });

  /* -------------------------------------------------------------- menus */
  const menuFor = button => document.getElementById(button.dataset.menu || button.getAttribute('aria-controls'));
  const menuItems = menu => [...menu.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="menuitemcheckbox"]')]
    .filter(item => !item.disabled && item.getAttribute('aria-disabled') !== 'true' && !item.closest('[hidden]'));
  let openMenuButton = null;
  lp.closeMenus = ({restoreFocus = false} = {}) => {
    if (!openMenuButton) return;
    const button = openMenuButton, menu = menuFor(button);
    openMenuButton = null;
    if (menu) menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus();
  };
  lp.openMenu = (button, {focus = 'none'} = {}) => {
    const menu = menuFor(button);
    if (!menu) return;
    if (openMenuButton && openMenuButton !== button) lp.closeMenus();
    menu.hidden = false; button.setAttribute('aria-expanded', 'true'); openMenuButton = button;
    if (!menu.dataset.placement) menu.dataset.placement = 'bottom-end';
    const items = menuItems(menu);
    if (focus === 'first') items[0]?.focus(); else if (focus === 'last') items.at(-1)?.focus();
  };
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-menu]');
    if (button) {
      event.preventDefault();
      if (button.getAttribute('aria-expanded') === 'true') lp.closeMenus(); else lp.openMenu(button);
      return;
    }
    if (!openMenuButton) return;
    const menu = menuFor(openMenuButton);
    const item = event.target.closest('[role^="menuitem"]');
    if (menu?.contains(event.target)) { if (item && !item.hasAttribute('data-keep-open')) lp.closeMenus(); return; }
    lp.closeMenus();
  });
  document.addEventListener('keydown', event => {
    const trigger = event.target.closest?.('[data-menu]');
    if (trigger && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); lp.openMenu(trigger, {focus: event.key === 'ArrowDown' ? 'first' : 'last'}); return;
    }
    if (!openMenuButton) return;
    const menu = menuFor(openMenuButton);
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); lp.closeMenus({restoreFocus: true}); return; }
    if (event.key === 'Tab') { lp.closeMenus(); return; }
    if (!menu?.contains(document.activeElement) && document.activeElement !== openMenuButton) return;
    const items = menuItems(menu), index = items.indexOf(document.activeElement);
    let next = null;
    if (event.key === 'ArrowDown') next = (index + 1) % items.length;
    else if (event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    if (next !== null && items.length) { event.preventDefault(); items[next].focus(); }
  }, true);

  /* ----------------------------------------------------------- tooltips */
  let tooltip = null, tooltipTimer = null;
  const hideTooltip = () => { clearTimeout(tooltipTimer); tooltip?.remove(); tooltip = null; };
  const showTooltip = target => {
    hideTooltip();
    const text = target.dataset.tooltip;
    if (!text || matchMedia('(hover: none)').matches) return;
    tooltipTimer = setTimeout(() => {
      if (!target.isConnected) return;
      tooltip = document.createElement('div');
      tooltip.className = 'tooltip'; tooltip.setAttribute('role', 'tooltip'); tooltip.textContent = text;
      document.body.append(tooltip);
      const r = target.getBoundingClientRect(), tr = tooltip.getBoundingClientRect();
      let top = r.bottom + 6, left = r.left + r.width / 2 - tr.width / 2;
      if (top + tr.height > innerHeight - 4) top = r.top - tr.height - 6;
      left = Math.max(6, Math.min(left, innerWidth - tr.width - 6));
      tooltip.style.top = `${top}px`; tooltip.style.left = `${left}px`;
    }, 350);
  };
  document.addEventListener('pointerover', event => { const t = event.target.closest?.('[data-tooltip]'); if (t) showTooltip(t); });
  document.addEventListener('pointerout', event => { if (event.target.closest?.('[data-tooltip]')) hideTooltip(); });
  document.addEventListener('focusin', event => { const t = event.target.closest?.('[data-tooltip]'); if (t && t.matches(':focus-visible')) showTooltip(t); });
  document.addEventListener('focusout', hideTooltip);
  document.addEventListener('click', hideTooltip, true);

  /* ---------------------------------------------------------- appearance */
  const darkQuery = matchMedia('(prefers-color-scheme: dark)');
  const syncAppearanceButtons = () => {
    const mode = document.documentElement.dataset.appearanceMode || 'system';
    document.querySelectorAll('[data-appearance-set]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.appearanceSet === mode)));
  };
  lp.setAppearance = mode => {
    if (!['light', 'dark', 'system'].includes(mode)) mode = 'system';
    try { localStorage.setItem('localplaud:appearance', mode); } catch (_error) { /* private mode */ }
    const root = document.documentElement;
    root.dataset.appearanceMode = mode;
    root.dataset.appearance = mode === 'system' ? (darkQuery.matches ? 'dark' : 'light') : mode;
    // Browser chrome (mobile status bar) follows the chosen appearance, not only the OS.
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta => {
      meta.content = root.dataset.appearance === 'dark' ? '#161616' : '#FFFFFF';
    });
    syncAppearanceButtons();
  };
  darkQuery.addEventListener?.('change', () => { if (document.documentElement.dataset.appearanceMode === 'system') lp.setAppearance('system'); });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-appearance-set]');
    if (button) { event.preventDefault(); lp.setAppearance(button.dataset.appearanceSet); }
  });

  /* ----------------------------------------------------------- shortcuts */
  const shortcuts = new Map();
  lp.shortcut = (combo, handler, {description = ''} = {}) => { shortcuts.set(combo, {handler, description}); };
  let pendingPrefix = null, prefixTimer = null;
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
    if (document.querySelector('dialog[open]') || !document.getElementById('import-backdrop')?.hidden) return;
    const key = event.key.length === 1 ? event.key : event.key;
    const combo = pendingPrefix ? `${pendingPrefix} ${key}` : key;
    if (pendingPrefix) { clearTimeout(prefixTimer); pendingPrefix = null; }
    if (shortcuts.has(combo)) { event.preventDefault(); shortcuts.get(combo).handler(event); return; }
    if ([...shortcuts.keys()].some(name => name.startsWith(`${key} `))) {
      pendingPrefix = key; prefixTimer = setTimeout(() => { pendingPrefix = null; }, 1200);
    }
  });
  const go = path => () => {
    const link = document.createElement('a');
    link.href = path; link.hidden = true; document.body.append(link);
    link.click(); link.remove();
  };
  lp.shortcut('g h', go('/home'), {description: 'Go to Home'});
  lp.shortcut('g f', go('/'), {description: 'Go to All files'});
  lp.shortcut('g a', go('/ask'), {description: 'Go to Ask'});
  lp.shortcut('g t', go('/templates'), {description: 'Go to Templates'});
  lp.shortcut('g s', go('/settings'), {description: 'Go to Settings'});
  lp.shortcut('?', () => lp.openDialog('shortcuts-dialog'), {description: 'Show keyboard shortcuts'});
  lp.shortcut('[', () => document.querySelector('.sidebar-collapse, .nav-expand:not([hidden])')?.click(), {description: 'Toggle sidebar'});

  /* ----------------------------------------------- persistent audio player
   * One recording plays at a time and keeps playing while the user navigates.
   * The detail page registers its <audio> with lp.player.attach(el, info);
   * before #app-view is swapped away a playing element is moved into
   * #lp-audio-host (outside #app-view), and the mini player takes over. When
   * the same recording is opened again, attach() puts the live element back
   * into the page so playback, position, and rate continue uninterrupted.
   *
   *   lp.player.attach(audioEl, {fileId, title, href}) -> the element to use
   *   lp.player.audio / .info / .play() / .pause() / .toggle() / .seek(s) / .skip(d)
   *   lp.player.stop()   — pause and dismiss the mini player
   *   lp.player.on('change'|'time', fn)
   */
  lp.player = (() => {
    let audio = null, info = {};
    const listeners = {change: new Set(), time: new Set()};
    const emit = type => listeners[type].forEach(fn => { try { fn(api); } catch (_error) { /* listener bug */ } });
    const fileIdFrom = el => {
      try { return decodeURIComponent(new URL(el.currentSrc || el.getAttribute('src') || '', location.href).pathname.split('/audio/')[1] || '').split('/')[0] || null; }
      catch (_error) { return null; }
    };
    const titleNow = () => (document.title || '').replace(/\s+—\s+localplaud$/, '') || 'localplaud';
    const bound = new WeakSet();
    function bind(el) {
      if (bound.has(el)) return;
      bound.add(el);
      el.addEventListener('play', () => {
        if (audio && audio !== el) { audio.pause(); if (!audio.closest('#app-view')) audio.remove(); }
        if (audio !== el) { audio = el; info = el._lpInfo || info; }
        updateSession(); emit('change');
      });
      ['pause', 'ended', 'ratechange', 'loadedmetadata'].forEach(type => el.addEventListener(type, () => { if (el === audio) { updateSession(); emit('change'); } }));
      el.addEventListener('timeupdate', () => { if (el === audio) emit('time'); });
    }
    function attach(el, details = {}) {
      if (!el) return el;
      const fileId = details.fileId || fileIdFrom(el);
      const next = {fileId, title: details.title || titleNow(), href: details.href || `/file/${encodeURIComponent(fileId || '')}`};
      if (audio && audio !== el && info.fileId && info.fileId === fileId) {
        // Same recording: hand the live element back to the page.
        audio.id = el.id; audio.className = el.className;
        el.replaceWith(audio);
        info = {...info, ...next};
        audio._lpInfo = info;
        emit('change');
        return audio;
      }
      el._lpInfo = next;
      bind(el);
      if (!audio || (audio.paused && audio.currentTime === 0)) { audio = el; info = next; emit('change'); }
      return el;
    }
    function park() {
      // Called before #app-view is replaced: keep a started recording alive.
      if (!audio || !audio.closest('#app-view')) return;
      const started = !audio.paused || audio.currentTime > 0;
      if (!started) { audio = null; info = {}; emit('change'); return; }
      // Leave an inert twin in the page so history snapshots keep valid markup.
      const twin = audio.cloneNode(false);
      twin.removeAttribute('autoplay');
      audio.replaceWith(twin);
      // Only the active workspace owns #player; a parked recording must not
      // intercept another recording's DOM lookups. attach restores its ID.
      audio.removeAttribute('id');
      document.getElementById('lp-audio-host')?.append(audio);
      emit('change');
    }
    function stop() {
      if (!audio) return;
      audio.pause();
      if (!audio.closest('#app-view')) audio.remove();
      audio = null; info = {};
      if ('mediaSession' in navigator) navigator.mediaSession.metadata = null;
      emit('change');
    }
    function updateSession() {
      if (!('mediaSession' in navigator) || !audio) return;
      try {
        navigator.mediaSession.metadata = new MediaMetadata({title: info.title || 'localplaud', artist: 'localplaud',
          artwork: [{src: '/static/icons/icon-192.png', sizes: '192x192', type: 'image/png'}, {src: '/static/icons/icon-512.png', sizes: '512x512', type: 'image/png'}]});
        navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
      } catch (_error) { /* unsupported */ }
    }
    if ('mediaSession' in navigator) {
      const handlers = {play: () => api.play(), pause: () => api.pause(), seekbackward: () => api.skip(-15), seekforward: () => api.skip(15),
        seekto: details => api.seek(details.seekTime), stop: () => stop()};
      for (const [action, handler] of Object.entries(handlers)) { try { navigator.mediaSession.setActionHandler(action, handler); } catch (_error) { /* unsupported action */ } }
    }
    const api = {
      attach, park, stop,
      updateTitle: (fileId, title) => {
        if (!audio || info.fileId !== fileId || typeof title !== 'string' || !title.trim()) return;
        info = {...info, title};
        audio._lpInfo = info;
        updateSession(); emit('change');
      },
      get audio() { return audio; },
      get info() { return {...info}; },
      get parked() { return Boolean(audio && !audio.closest('#app-view')); },
      play: () => audio?.play().catch(() => {}),
      pause: () => audio?.pause(),
      toggle: () => (audio ? (audio.paused ? api.play() : api.pause()) : undefined),
      seek: seconds => { if (audio && Number.isFinite(seconds)) audio.currentTime = Math.max(0, seconds); },
      skip: delta => { if (audio) audio.currentTime = Math.max(0, Math.min(audio.duration || Infinity, audio.currentTime + delta)); },
      on: (type, fn) => { listeners[type]?.add(fn); return () => listeners[type]?.delete(fn); },
    };
    document.addEventListener('htmx:beforeSwap', event => { if (event.detail?.target?.id === 'app-view' && event.detail?.shouldSwap !== false) park(); });
    // Back/forward restores #app-view from htmx's history cache without a beforeSwap;
    // this listener is registered before htmx's own popstate handler, so it runs first.
    window.addEventListener('popstate', park);
    return api;
  })();

  /* ---------------------------------------------------------- mini player */
  const clock = seconds => {
    const total = Math.max(0, Math.floor(seconds || 0)), h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  };
  const whenReady = fn => (document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', fn) : fn());
  whenReady(() => {
    const bar = document.getElementById('lp-mini-player');
    if (!bar) return;
    const toggle = bar.querySelector('[data-mini-toggle]'), link = bar.querySelector('[data-mini-link]');
    const name = bar.querySelector('.mini-name'), time = bar.querySelector('.mini-time'), fill = bar.querySelector('.mini-progress span');
    const render = () => {
      const p = lp.player, audio = p.audio, show = Boolean(audio && p.parked);
      bar.hidden = !show;
      document.body.classList.toggle('has-mini-player', show);
      if (!show) return;
      const info = p.info;
      name.textContent = info.title || 'localplaud';
      const href = info.href || '/';
      if (link.getAttribute('href') !== href) {
        link.setAttribute('href', href);
        if (link.hasAttribute('hx-get')) { link.setAttribute('hx-get', href); window.htmx?.process(link); }
      }
      const playing = !audio.paused;
      toggle.querySelector('use').setAttribute('href', playing ? '#i-pause' : '#i-play');
      toggle.setAttribute('aria-label', playing ? toggle.dataset.labelPause : toggle.dataset.labelPlay);
      renderTime();
    };
    const renderTime = () => {
      const audio = lp.player.audio;
      if (!audio || bar.hidden) return;
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      time.textContent = duration ? `${clock(audio.currentTime)} / ${clock(duration)}` : clock(audio.currentTime);
      fill.style.width = duration ? `${Math.min(100, (audio.currentTime / duration) * 100)}%` : '0%';
    };
    lp.player.on('change', render);
    lp.player.on('time', renderTime);
    bar.addEventListener('click', event => {
      if (event.target.closest('[data-mini-toggle]')) lp.player.toggle();
      const skip = event.target.closest('[data-mini-skip]');
      if (skip) lp.player.skip(Number(skip.dataset.miniSkip));
      if (event.target.closest('[data-mini-close]')) lp.player.stop();
    });
    document.addEventListener('htmx:afterSettle', render);
    render();
  });

  /* ---------------- phone list gestures: swipe actions, long-press, pull-to-refresh
   * Touch only, and every gesture has a button equivalent (row "…" stays in the
   * accessibility tree; the header "…" has "Select recordings").
   */
  whenReady(() => {
    const compact = matchMedia('(max-width: 820px)');
    const SWIPE_W = 72;
    let gesture = null, suppressClick = false, openSwipe = null;
    const closeSwipe = (row = openSwipe) => {
      if (!row) return;
      row.classList.remove('swipe-open');
      const main = row.querySelector('.row-main'); if (main) main.style.transform = '';
      if (openSwipe === row) openSwipe = null;
    };
    const swipeActions = row => {
      let tray = row.querySelector('.row-swipe');
      if (tray) return tray;
      const more = row.querySelector('.row-more');
      if (!more) return null;
      const actions = more.dataset.trash === '1' ? [['open', 'external-link', 'Open recording']]
        : [['notes', 'note', 'Notes'], ['move', 'folder-input', 'Move'], ['rename', 'pencil', 'Rename']];
      tray = document.createElement('div');
      tray.className = 'row-swipe'; tray.setAttribute('aria-hidden', 'true');
      for (const [action, iconName, text] of actions) {
        const button = document.createElement('button');
        button.type = 'button'; button.tabIndex = -1; button.dataset.swipeAction = action;
        button.innerHTML = `<svg class="i" aria-hidden="true"><use href="#i-${iconName}"></use></svg><span></span>`;
        button.querySelector('span').textContent = lp.t(text);
        tray.append(button);
      }
      tray.style.width = `${actions.length * SWIPE_W}px`;
      row.append(tray);
      return tray;
    };
    document.addEventListener('pointerdown', event => {
      if (event.pointerType !== 'touch' || !compact.matches) return;
      const row = event.target.closest('.file-row');
      if (openSwipe && openSwipe !== row) closeSwipe();
      if (!row || event.target.closest('.row-swipe, .row-check, button')) return;
      gesture = {row, x: event.clientX, y: event.clientY, dx: 0, mode: null, pointerId: event.pointerId,
        timer: setTimeout(() => {
          if (!gesture || gesture.mode) return;
          gesture.mode = 'press'; suppressClick = true;
          navigator.vibrate?.(12);
          const check = row.querySelector('.row-select');
          if (check) {
            row.closest('.file-table')?.classList.add('is-selecting');
            check.checked = !check.checked;
            check.dispatchEvent(new Event('change', {bubbles: true}));
          } else row.querySelector('.row-more')?.click();
        }, 500)};
    }, {passive: true});
    document.addEventListener('pointermove', event => {
      if (!gesture || event.pointerId !== gesture.pointerId) return;
      const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
      if (!gesture.mode) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        clearTimeout(gesture.timer);
        gesture.mode = Math.abs(dx) > Math.abs(dy) * 1.3 ? 'swipe' : 'scroll';
        if (gesture.mode === 'swipe' && !swipeActions(gesture.row)) gesture.mode = 'scroll';
      }
      if (gesture.mode !== 'swipe') return;
      const tray = gesture.row.querySelector('.row-swipe'), max = tray.offsetWidth;
      const base = gesture.row.classList.contains('swipe-open') ? -max : 0;
      gesture.dx = Math.max(-max - 24, Math.min(0, base + dx));
      gesture.row.classList.add('is-swiping');
      gesture.row.querySelector('.row-main').style.transform = `translateX(${gesture.dx}px)`;
    }, {passive: true});
    const endGesture = event => {
      if (!gesture || event.pointerId !== gesture.pointerId) return;
      clearTimeout(gesture.timer);
      const {row, mode, dx} = gesture;
      gesture = null;
      if (mode !== 'swipe') return;
      suppressClick = true;
      row.classList.remove('is-swiping');
      const max = row.querySelector('.row-swipe').offsetWidth;
      if (dx < -Math.min(80, max / 2)) {
        row.classList.add('swipe-open'); row.querySelector('.row-main').style.transform = `translateX(${-max}px)`;
        if (openSwipe && openSwipe !== row) closeSwipe(); openSwipe = row;
      } else closeSwipe(row);
    };
    document.addEventListener('pointerup', endGesture);
    document.addEventListener('pointercancel', event => { if (gesture?.mode === 'swipe') endGesture(event); else { clearTimeout(gesture?.timer); gesture = null; } });
    document.addEventListener('click', event => {
      const action = event.target.closest('[data-swipe-action]');
      if (action) {
        const row = action.closest('.file-row'), more = row?.querySelector('.row-more');
        closeSwipe(row);
        if (more) lp.rowAction(more, action.dataset.swipeAction);
        event.preventDefault(); event.stopPropagation(); return;
      }
      if (suppressClick) { suppressClick = false; if (event.target.closest('.file-row')) { event.preventDefault(); event.stopPropagation(); } }
    }, true);
    document.addEventListener('contextmenu', event => { if (compact.matches && event.target.closest('.file-row') && matchMedia('(pointer: coarse)').matches) event.preventDefault(); });

    // Pull to refresh (lists only; native overscroll reload is disabled in CSS).
    const indicator = document.createElement('div');
    indicator.className = 'ptr-indicator'; indicator.setAttribute('aria-hidden', 'true');
    indicator.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-refresh"></use></svg>';
    document.body.append(indicator);
    let pull = null;
    const listPage = () => document.querySelector('#app-view .library-page .file-list, #app-view .explore-page');
    document.addEventListener('touchstart', event => {
      if (!compact.matches || window.scrollY > 0 || document.querySelector('dialog[open]') || document.body.classList.contains('nav-open') || !listPage()) return;
      if (event.target.closest('.file-row.swipe-open, .m-tabbar, .mini-player')) return;
      pull = {y: event.touches[0].clientY, dy: 0};
    }, {passive: true});
    document.addEventListener('touchmove', event => {
      if (!pull) return;
      pull.dy = Math.max(0, event.touches[0].clientY - pull.y);
      if (window.scrollY > 0) { pull = null; indicator.style.transform = ''; return; }
      const distance = Math.min(110, pull.dy * 0.5);
      indicator.style.transform = `translate(-50%, ${distance}px) rotate(${distance * 3}deg)`;
      indicator.classList.toggle('is-ready', distance >= 64);
    }, {passive: true});
    document.addEventListener('touchend', () => {
      if (!pull) return;
      const ready = Math.min(110, pull.dy * 0.5) >= 64;
      pull = null;
      if (!ready) { indicator.style.transform = ''; indicator.classList.remove('is-ready'); return; }
      indicator.classList.add('is-refreshing');
      indicator.style.transform = 'translate(-50%, 64px)';
      const done = () => { indicator.classList.remove('is-refreshing', 'is-ready'); indicator.style.transform = ''; };
      if (window.htmx) {
        window.htmx.ajax('GET', location.pathname + location.search, {target: '#app-view', select: '#app-view', swap: 'outerHTML'})
          .then(done, () => { done(); lp.toast(lp.t('Could not refresh'), {type: 'error'}); });
      } else location.reload();
    });
  });

  /* ------------------------------- phone "+" sheet: upload / Plaud sync */
  whenReady(() => {
    const sheet = document.getElementById('add-sheet');
    if (!sheet) return;
    const box = sheet.querySelector('[data-add-progress]'), label = sheet.querySelector('[data-add-progress-label]');
    const bar = box.querySelector('[role="progressbar"]'), fill = bar.querySelector('span');
    const file = sheet.querySelector('[data-add-file]'), sync = sheet.querySelector('[data-add-sync]');
    const setProgress = (pct, text) => {
      box.hidden = false;
      const value = Math.max(0, Math.min(100, Math.round(pct)));
      fill.style.width = `${value}%`; bar.setAttribute('aria-valuenow', String(value));
      if (text) label.textContent = text;
    };
    const busy = value => {
      sheet.dataset.busy = String(value);
      file.disabled = value; sync.disabled = value;
      sheet.querySelectorAll('.add-option').forEach(item => item.classList.toggle('is-disabled', value));
    };
    sheet.addEventListener('lp:dialog-open', () => { if (sheet.dataset.busy !== 'true') { box.hidden = true; label.textContent = ''; } });
    file.addEventListener('change', () => {
      const chosen = file.files?.[0];
      if (!chosen) return;
      busy(true); setProgress(0, `${lp.t('Importing')} ${chosen.name}…`);
      const body = new FormData(); body.append('file', chosen);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/imports/local/audio');
      xhr.upload.addEventListener('progress', event => { if (event.lengthComputable) setProgress((event.loaded / event.total) * 100); });
      xhr.addEventListener('load', () => {
        busy(false); file.value = '';
        let data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch (_error) { /* non-JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && data.id) {
          setProgress(100, lp.t('Imported. Opening recording…'));
          sheet.close();
          lp.toast(lp.t('Imported. Processing starts automatically.'), {type: 'success', action: {label: lp.t('Open'), onClick: () => { location.href = `/file/${encodeURIComponent(data.id)}`; }}});
          const link = document.createElement('a'); link.href = `/file/${encodeURIComponent(data.id)}`; link.hidden = true; document.body.append(link); link.click(); link.remove();
        } else {
          const detail = typeof data.detail === 'string' ? lp.t(data.detail) : lp.t('Could not upload audio');
          setProgress(0, detail); lp.toast(detail, {type: 'error'});
        }
      });
      xhr.addEventListener('error', () => { busy(false); file.value = ''; setProgress(0, lp.t('Could not upload audio')); lp.toast(lp.t('Could not upload audio'), {type: 'error'}); });
      xhr.send(body);
    });
    let pollTimer = null;
    const poll = async () => {
      try {
        const response = await fetch('/api/imports/plaud/metadata/status');
        const data = await response.json();
        const pct = data.total ? (data.processed / data.total) * 100 : 5;
        if (['queued', 'running'].includes(data.status)) {
          setProgress(pct, `${data.processed || 0} ${lp.t('of')} ${data.total || '…'} · ${lp.t('metadata only, audio not downloaded')}`);
          pollTimer = setTimeout(poll, 1000); return;
        }
        busy(false);
        if (data.status === 'completed') {
          setProgress(100, `${lp.t('Complete')} · ${data.processed} ${lp.t('recordings')}`);
          lp.toast(`${lp.t('Plaud sync complete')} · ${data.new || 0} ${lp.t('new')}`, {type: 'success', action: {label: lp.t('Refresh'), onClick: () => location.reload()}});
        } else if (data.status === 'failed') {
          const message = `${lp.t('Import stopped')} · ${lp.t(data.error || 'Unknown error')}`;
          setProgress(0, message); lp.toast(message, {type: 'error'});
        }
      } catch (_error) { busy(false); setProgress(0, lp.t('Could not check Plaud import status')); }
    };
    sync.addEventListener('click', async () => {
      if (sheet.dataset.busy === 'true') return;
      busy(true); setProgress(2, lp.t('Starting Plaud sync…'));
      try {
        const response = await fetch('/api/imports/plaud/metadata', {method: 'POST'});
        if (!response.ok && response.status !== 409) {
          const data = await response.json().catch(() => ({}));
          throw new Error(typeof data.detail === 'string' ? lp.t(data.detail) : lp.t('Could not start Plaud import'));
        }
        clearTimeout(pollTimer); poll();
      } catch (failure) { busy(false); setProgress(0, failure.message); lp.toast(failure.message, {type: 'error'}); }
    });
  });

  /* ------------------------------------------ view transitions, offline */
  try {
    if (window.htmx && document.startViewTransition && !reduceMotion()) window.htmx.config.globalViewTransitions = true;
  } catch (_error) { /* older htmx */ }
  // Only page navigations cross-fade; in-page fragment swaps stay instant.
  document.addEventListener('htmx:beforeTransition', event => { if (event.detail?.target?.id !== 'app-view') event.preventDefault(); });
  whenReady(() => {
    const banner = document.getElementById('lp-offline-banner');
    const sync = () => { if (banner) banner.hidden = navigator.onLine; document.body.classList.toggle('is-offline', !navigator.onLine); };
    window.addEventListener('online', () => { sync(); lp.toast(lp.t('Back online'), {type: 'success', timeout: 2500}); });
    window.addEventListener('offline', sync);
    sync();
  });

  /* --------------------------------------------------- shell: DOM wiring */
  const onReady = fn => (document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', fn) : fn());

  // Collapsible panels (desktop): sidebar and recording file list.
  onReady(() => {
    const panels = {nav: ['lp-nav-collapsed', 'localplaud:nav-collapsed'], list: ['lp-list-collapsed', 'localplaud:list-collapsed']};
    const sync = () => {
      for (const [name, [cls]] of Object.entries(panels)) {
        const collapsed = document.documentElement.classList.contains(cls);
        document.querySelectorAll(`[data-collapse-toggle="${name}"]`).forEach(toggle => {
          toggle.setAttribute('aria-expanded', String(!collapsed));
          if (name === 'list') { const label = lp.t(collapsed ? 'Show file list' : 'Hide file list'); toggle.setAttribute('aria-label', label); toggle.title = label; }
        });
      }
    };
    document.addEventListener('click', event => {
      const toggle = event.target.closest('[data-collapse-toggle]'); if (!toggle) return;
      const [cls, key] = panels[toggle.dataset.collapseToggle];
      const collapsed = document.documentElement.classList.toggle(cls);
      try { localStorage.setItem(key, collapsed ? '1' : '0'); } catch (_error) { /* ignore */ }
      sync();
      if (toggle.dataset.collapseToggle === 'nav') requestAnimationFrame(() => document.querySelector(collapsed ? '.nav-expand' : '.sidebar-collapse')?.focus());
    });
    document.addEventListener('htmx:afterSwap', sync); sync();

    // Sidebar groups (Folders, Comes from) remember their open state.
    const groupKey = name => `localplaud:nav-group:${name}`;
    document.querySelectorAll('[data-collapse-group]').forEach(toggle => {
      const list = document.getElementById(toggle.getAttribute('aria-controls'));
      let collapsed = false; try { collapsed = localStorage.getItem(groupKey(toggle.dataset.collapseGroup)) === '0'; } catch (_error) { /* ignore */ }
      if (collapsed && list) { list.hidden = true; toggle.setAttribute('aria-expanded', 'false'); }
      toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', String(open)); if (list) list.hidden = !open;
        try { localStorage.setItem(groupKey(toggle.dataset.collapseGroup), open ? '1' : '0'); } catch (_error) { /* ignore */ }
      });
    });
    syncAppearanceButtons();
  });

  // Drawer sidebar on compact widths (opened from the top bar inside #app-view).
  onReady(() => {
    const sidebar = document.getElementById('workspace-sidebar'), scrim = document.querySelector('.nav-scrim');
    if (!sidebar) return;
    const drawerMedia = matchMedia('(max-width:820px)');
    let opener = null;
    const openers = () => document.querySelectorAll('[data-nav-open]');
    const syncState = open => {
      if (drawerMedia.matches) { sidebar.toggleAttribute('inert', !open); sidebar.setAttribute('aria-hidden', String(!open)); scrim.hidden = !open; }
      else { sidebar.removeAttribute('inert'); sidebar.removeAttribute('aria-hidden'); scrim.hidden = true; }
      openers().forEach(button => button.setAttribute('aria-expanded', String(open && drawerMedia.matches)));
    };
    const close = () => { const wasOpen = document.body.classList.contains('nav-open'); document.body.classList.remove('nav-open'); syncState(false); if (wasOpen && opener?.isConnected) opener.focus(); };
    document.addEventListener('click', event => {
      if (event.target.closest('[data-nav-close]')) { close(); return; }
      const trigger = event.target.closest('[data-nav-open]');
      if (trigger) { opener = trigger; document.body.classList.add('nav-open'); syncState(true); sidebar.querySelector('[data-nav-close]')?.focus(); return; }
      if (drawerMedia.matches && sidebar.contains(event.target) && event.target.closest('a.nav-item[href], a.menu-item[href]')) close();
    });
    document.addEventListener('keydown', event => {
      if (!document.body.classList.contains('nav-open')) return;
      if (event.key === 'Escape') { close(); return; }
      if (event.key !== 'Tab' || !drawerMedia.matches) return;
      const focusable = [...sidebar.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])')].filter(item => !item.closest('[hidden]'));
      if (!focusable.length) return;
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    drawerMedia.addEventListener('change', () => { document.body.classList.remove('nav-open'); syncState(false); });
    document.addEventListener('htmx:afterSettle', () => syncState(document.body.classList.contains('nav-open')));
    syncState(false);
  });

  // Phone tab bar visibility and soft-keyboard detection.
  onReady(() => {
    const syncTabbar = () => {
      const view = document.getElementById('app-view');
      document.body.classList.toggle('has-tabbar', view?.dataset.tabbar === 'on');
    };
    document.addEventListener('htmx:afterSettle', syncTabbar);
    document.addEventListener('htmx:historyRestore', syncTabbar);
    syncTabbar();
    const viewport = window.visualViewport;
    if (viewport) {
      const check = () => document.body.classList.toggle('keyboard-open', window.innerHeight - viewport.height > 150 && isTyping(document.activeElement));
      viewport.addEventListener('resize', check);
      document.addEventListener('focusin', check); document.addEventListener('focusout', () => setTimeout(check, 50));
    }
  });

  // Folder create/rename dialog (sidebar "+", library manager entry points).
  onReady(() => {
    const dialog = document.getElementById('folder-dialog');
    if (!dialog) return;
    const form = dialog.querySelector('[data-folder-form]'), error = dialog.querySelector('[data-folder-error]');
    const title = dialog.querySelector('.dialog-title'), submit = dialog.querySelector('[data-folder-submit]');
    let editingId = null;
    lp.openFolderDialog = ({id = null, name = '', color = null, trigger = null} = {}) => {
      editingId = id; form.reset(); error.textContent = '';
      form.elements.name.value = name;
      if (color) { const radio = [...form.elements.color].find(item => item.value.toLowerCase() === String(color).toLowerCase()); if (radio) radio.checked = true; }
      title.textContent = id ? title.dataset.renameTitle : title.dataset.createTitle;
      lp.openDialog(dialog, trigger);
    };
    document.addEventListener('click', event => {
      const trigger = event.target.closest('[data-folder-create]');
      if (trigger) { event.preventDefault(); lp.openFolderDialog({trigger}); }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const name = form.elements.name.value.trim(); if (!name) return;
      submit.disabled = true; dialog.dataset.busy = 'true'; error.textContent = '';
      try {
        const response = await fetch(editingId ? `/api/folders/${editingId}` : '/api/folders', {
          method: editingId ? 'PATCH' : 'POST', headers: {'content-type': 'application/json'},
          body: JSON.stringify({name, color: form.elements.color.value}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? lp.t(data.detail) : lp.t('Could not save folder'));
        dialog.dataset.busy = 'false'; dialog.close();
        lp.toast(lp.t(editingId ? 'Folder updated' : 'Folder created'), {type: 'success'});
        setTimeout(() => location.reload(), 350);
      } catch (failure) {
        error.textContent = failure.message || lp.t('Could not save folder');
      } finally { submit.disabled = false; dialog.dataset.busy = 'false'; }
    });
  });

  // Declarative toasts.
  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-toast]');
    if (trigger) lp.toast(trigger.dataset.toast, {type: trigger.dataset.toastType || 'info'});
  });

  /* -------------------------------------------- recording row "…" actions */
  onReady(() => {
    const menu = document.getElementById('row-menu');
    if (!menu) return;
    let row = null;
    // Position the shared menu next to the clicked "…" before app.js opens it.
    document.addEventListener('click', event => {
      const button = event.target.closest('.row-more[data-menu="row-menu"]');
      if (!button) return;
      row = {...button.dataset, button};
      const r = button.getBoundingClientRect();
      menu.style.position = 'fixed';
      menu.style.right = 'auto'; menu.style.bottom = 'auto';
      const width = 240;
      menu.style.left = `${Math.max(8, Math.min(r.right - width, innerWidth - width - 8))}px`;
      const below = r.bottom + 4, height = 260;
      menu.style.top = `${below + height > innerHeight ? Math.max(8, r.top - height - 4) : below}px`;
      menu.style.width = `${width}px`;
      menu.querySelector('[data-row-action="open"]').href = row.href;
      const notes = menu.querySelector('[data-row-action="notes"]');
      notes.href = `/file/${encodeURIComponent(row.fileId)}?tab=notes`;
      const exportLink = menu.querySelector('[data-row-action="export"]');
      exportLink.href = `/file/${encodeURIComponent(row.fileId)}/export.md`;
      exportLink.hidden = row.exportable !== '1';
      menu.querySelector('[data-row-action="rename"]').hidden = row.trash === '1';
      menu.querySelector('[data-row-action="move"]').hidden = row.trash === '1';
    }, true);
    window.addEventListener('scroll', () => { if (openMenuButton?.matches('.row-more')) lp.closeMenus(); }, true);
    const renameDialog = document.getElementById('rename-dialog'), moveDialog = document.getElementById('move-dialog');
    // Shared by the "…" menu, swipe actions, and the long-press sheet.
    lp.rowAction = (button, action) => {
      const data = {...button.dataset, button};
      if (action === 'rename') {
        const form = renameDialog.querySelector('form');
        form.elements.title.value = data.title || '';
        renameDialog.querySelector('[data-rename-error]').textContent = '';
        renameDialog.dataset.fileId = data.fileId;
        lp.openDialog(renameDialog, button);
      } else if (action === 'move') {
        const form = moveDialog.querySelector('form');
        [...form.elements.folder_id].forEach(radio => { radio.checked = radio.value === (data.folderId || ''); });
        moveDialog.querySelector('[data-move-error]').textContent = '';
        moveDialog.dataset.fileId = data.fileId;
        lp.openDialog(moveDialog, button);
      } else if (action === 'notes' || action === 'open') {
        const link = document.createElement('a');
        link.href = action === 'notes' ? `/file/${encodeURIComponent(data.fileId)}?tab=notes` : data.href;
        link.hidden = true; document.body.append(link); link.click(); link.remove();
      }
    };
    menu.addEventListener('click', event => {
      const item = event.target.closest('[data-row-action]');
      if (!item || !row) return;
      if (['rename', 'move'].includes(item.dataset.rowAction)) lp.rowAction(row.button, item.dataset.rowAction);
    });
    const submitJson = async (dialog, url, method, body, errorEl, fallback) => {
      dialog.dataset.busy = 'true';
      const submit = dialog.querySelector('[type="submit"]'); submit.disabled = true;
      try {
        const response = await fetch(url, {method, headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? lp.t(data.detail) : lp.t(fallback));
        dialog.dataset.busy = 'false'; dialog.close();
        return data;
      } catch (failure) { errorEl.textContent = failure.message || lp.t(fallback); return null; }
      finally { submit.disabled = false; dialog.dataset.busy = 'false'; }
    };
    renameDialog?.querySelector('form').addEventListener('submit', async event => {
      event.preventDefault();
      const id = renameDialog.dataset.fileId, title = event.currentTarget.elements.title.value.trim();
      const data = await submitJson(renameDialog, `/api/files/${encodeURIComponent(id)}/title`, 'PATCH', {title: title || null}, renameDialog.querySelector('[data-rename-error]'), 'Could not rename recording');
      if (!data) return;
      const name = data.display_title || data.title || title;
      document.querySelectorAll(`.file-row[data-file-id="${CSS.escape(id)}"]`).forEach(item => {
        const link = item.querySelector('.row-title'); if (name && link) { link.textContent = name; link.title = name; }
        const more = item.querySelector('.row-more'); if (more && name) more.dataset.title = name;
      });
      lp.toast(lp.t('Recording renamed'), {type: 'success'});
    });
    moveDialog?.querySelector('form').addEventListener('submit', async event => {
      event.preventDefault();
      const id = moveDialog.dataset.fileId, value = event.currentTarget.elements.folder_id.value;
      const data = await submitJson(moveDialog, '/api/files/organize', 'POST', {file_ids: [id], folder_id: value ? Number(value) : null}, moveDialog.querySelector('[data-move-error]'), 'Could not move recording');
      if (!data) return;
      lp.toast(lp.t('Recording moved'), {type: 'success'});
      setTimeout(() => location.reload(), 400);
    });
  });

  /* ------------------------------------------------ Add audio / import dialog */
  onReady(() => {
    const tr = window.localplaudT, button = document.getElementById('add-audio-button'), menu = document.getElementById('add-audio-menu'), backdrop = document.getElementById('import-backdrop'), closeButton = document.getElementById('import-close');
    if (!button || !menu || !backdrop) return;
    const menuItems = [...menu.querySelectorAll('[role="menuitem"]')], tabs = [...document.querySelectorAll('[data-import-tab]')], devicePanel = document.getElementById('device-import-panel'), plaudPanel = document.getElementById('plaud-import-panel'), input = document.getElementById('audio-file-input'), drop = document.getElementById('audio-drop-zone'), localStatus = document.getElementById('local-import-status'), plaudButton = document.getElementById('start-plaud-import');
    const importModal=window.localplaudModal({backdrop,background:()=>[document.getElementById('app-view'),document.querySelector('.sidebar'),document.querySelector('.nav-scrim')]});
    let progressTimer=null,requestController=null,busy=false;
    const detailMessage=(data,fallback)=>{const detail=data?.detail,item=Array.isArray(detail)?detail[0]?.msg:detail;return tr(typeof item==='string'?item:fallback);};
    function setMenu(open,{restoreFocus=false}={}){menu.hidden=!open;button.setAttribute('aria-expanded',String(open));if(!open&&restoreFocus)button.focus();}
    function stopImportWork(){if(progressTimer)clearTimeout(progressTimer);progressTimer=null;requestController?.abort();requestController=null;}
    function setDialogBusy(value){busy=value;importModal.setBusy(value);closeButton.disabled=value;tabs.forEach(tab=>{tab.disabled=value;});input.disabled=value;drop.setAttribute('aria-disabled',String(value));}
    function setPlaudBusy(value){plaudButton.disabled=value;plaudButton.textContent=tr(value?'Importing…':'Sync Plaud recordings and notes');}
    async function requestJson(url,options,fallback,acceptableStatuses=[]){const controller=new AbortController();requestController?.abort();requestController=controller;let response;try{response=await fetch(url,{...options,signal:controller.signal});}catch(error){if(error.name==='AbortError')throw error;throw new Error(tr(fallback));}finally{if(requestController===controller)requestController=null;}let data={};try{data=await response.json();}catch(_error){}if(!response.ok&&!acceptableStatuses.includes(response.status))throw new Error(detailMessage(data,fallback));return data;}
    function selectMode(mode,{focus=false}={}){stopImportWork();setDialogBusy(false);setPlaudBusy(false);devicePanel.hidden=mode!=='device';plaudPanel.hidden=mode!=='plaud';for(const tab of tabs){const active=tab.dataset.importTab===mode;tab.classList.toggle('on',active);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;if(active&&focus)tab.focus();}document.getElementById('import-title').textContent=tr(mode==='plaud'?'Import from Plaud':'Import audio');if(mode==='plaud')pollPlaudImport();}
    function choose(mode,trigger){setMenu(false);lp.closeMenus();document.body.classList.remove('nav-open');selectMode(mode);importModal.open(trigger,tabs.find(tab=>tab.dataset.importTab===mode));}
    function closeImport(){if(busy)return false;stopImportWork();setPlaudBusy(false);return importModal.close();}
    lp.openImport=(mode='device',trigger=null)=>choose(mode==='plaud'?'plaud':'device',trigger);
    document.addEventListener('click',event=>{const trigger=event.target.closest('[data-open-import]');if(trigger)choose(trigger.dataset.openImport==='plaud'?'plaud':'device',trigger);if(!menu.hidden&&!event.target.closest('.add-wrap'))setMenu(false);});
    button.addEventListener('click',()=>setMenu(menu.hidden));
    button.addEventListener('keydown',event=>{if(['ArrowDown','Enter',' '].includes(event.key)){event.preventDefault();setMenu(true);menuItems[0]?.focus();}});
    menu.addEventListener('keydown',event=>{const index=menuItems.indexOf(document.activeElement);let next=null;if(event.key==='ArrowDown')next=(index+1)%menuItems.length;else if(event.key==='ArrowUp')next=(index-1+menuItems.length)%menuItems.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=menuItems.length-1;if(next!==null){event.preventDefault();menuItems[next]?.focus();}});
    menu.addEventListener('focusout',()=>requestAnimationFrame(()=>{if(!menu.contains(document.activeElement))setMenu(false);}));
    document.querySelectorAll('[data-import-mode]').forEach(item=>item.addEventListener('click',()=>choose(item.dataset.importMode,button)));
    tabs.forEach((tab,index)=>{tab.addEventListener('click',()=>selectMode(tab.dataset.importTab));tab.addEventListener('keydown',event=>{let next=null;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;if(next!==null){event.preventDefault();selectMode(tabs[next].dataset.importTab,{focus:true});}});});
    closeButton.addEventListener('click',closeImport);
    backdrop.addEventListener('click',event=>{if(event.target===backdrop&&!backdrop.hidden&&!busy)closeImport();});
    backdrop.addEventListener('localplaud:modal-close',()=>{stopImportWork();setDialogBusy(false);setPlaudBusy(false);drop.classList.remove('drag');localStatus.textContent='';input.value='';});
    document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;if(!backdrop.hidden){event.preventDefault();event.stopPropagation();if(!busy)closeImport();}else if(!menu.hidden){event.preventDefault();event.stopPropagation();setMenu(false,{restoreFocus:true});}},true);
    ['dragenter','dragover'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();if(!busy)drop.classList.add('drag');}));
    ['dragleave','drop'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove('drag');}));
    drop.addEventListener('keydown',event=>{if(!busy&&(event.key==='Enter'||event.key===' ')){event.preventDefault();input.click();}});
    drop.addEventListener('drop',event=>{if(!busy&&event.dataTransfer.files[0])uploadLocal(event.dataTransfer.files[0]);});
    input.addEventListener('change',()=>{if(!busy&&input.files[0])uploadLocal(input.files[0]);});
    async function uploadLocal(file){
      setDialogBusy(true);localStatus.textContent=`${tr('Importing')} ${file.name}…`;const body=new FormData();body.append('file',file);
      try{const data=await requestJson('/api/imports/local/audio',{method:'POST',body},'Could not upload audio');localStatus.textContent=tr('Imported. Opening recording…');location.href=`/file/${encodeURIComponent(data.id)}`;}catch(error){if(error.name!=='AbortError')localStatus.textContent=error.message||tr('Import failed');}finally{setDialogBusy(false);input.value='';}
    }
    plaudButton.addEventListener('click',async()=>{if(plaudButton.disabled)return;setPlaudBusy(true);setDialogBusy(true);try{await requestJson('/api/imports/plaud/metadata',{method:'POST'},'Could not start Plaud import',[409]);setDialogBusy(false);pollPlaudImport();}catch(error){if(error.name!=='AbortError')showProgress({status:'failed',error:error.message});setDialogBusy(false);setPlaudBusy(false);}});
    async function pollPlaudImport(){
      try{const data=await requestJson('/api/imports/plaud/metadata/status',{},'Could not check Plaud import status');showProgress(data);const running=['queued','running'].includes(data.status);setPlaudBusy(running);if(running)progressTimer=setTimeout(pollPlaudImport,1000);}catch(error){if(error.name!=='AbortError'){showProgress({status:'failed',error:error.message});setPlaudBusy(false);}}
    }
    function showProgress(data){
      const box=document.getElementById('plaud-import-progress'),label=box.querySelector('[data-progress-label]'),progress=box.querySelector('[role="progressbar"]'),bar=progress.querySelector('span');
      if(data.status==='idle'){box.hidden=true;progress.setAttribute('aria-valuenow','0');return;}box.hidden=false;const pct=data.total?Math.round(data.processed/data.total*100):0;bar.style.width=`${pct}%`;progress.setAttribute('aria-valuenow',String(pct));
      const skipped=tr('skipped {value}').replace('{value}',data.skipped||0);
      if(data.status==='completed')label.textContent=`${tr('Complete')} · ${data.processed} ${tr('recordings')} · ${data.transcripts} ${tr('transcripts')} · ${data.summaries} ${tr('summaries')} · ${skipped}`;
      else if(data.status==='failed')label.textContent=`${tr('Import stopped')} · ${tr(data.error||'Unknown error')}`;
      else label.textContent=`${data.processed||0} ${tr('of')} ${data.total||'…'} · ${skipped} · ${tr('metadata only, audio not downloaded')}`;
    }
  });

  /* --------------------------- partial navigation, scroll restore, Ask lifecycle */
  onReady(() => {
    const scrollPrefix='localplaud:view-scroll:';
    const regionSelectors={fileList:'.fl-scroll',pane:'.pane',main:'.main',content:'.content',library:'.library-page'};
    let pendingFileListScroll=null;
    const storageKey=()=>`${scrollPrefix}${location.pathname}${location.search}`;
    const captureScroll=()=>{
      const view=document.getElementById('app-view');if(!view)return;
      const state={windowY:window.scrollY};
      for(const [name,selector] of Object.entries(regionSelectors)){const region=view.querySelector(selector);if(region)state[name]=region.scrollTop;}
      try{sessionStorage.setItem(storageKey(),JSON.stringify(state));}catch(_error){}
    };
    const readScroll=()=>{try{return JSON.parse(sessionStorage.getItem(storageKey())||'null');}catch(_error){return null;}};
    // The sidebar shows only the most-used tags; an active tag outside that
    // set gets a transient row (named by the library page) so the current
    // filter is always visible in the persistent shell.
    const syncSidebarTag=activeHref=>{
      const list=document.getElementById('sidebar-tags');if(!list)return;
      list.querySelectorAll(':scope > [data-transient-tag]').forEach(item=>item.remove());
      if(!activeHref?.startsWith('/?tag='))return;
      if([...list.querySelectorAll('.nav-item[href]')].some(link=>link.getAttribute('href')===activeHref))return;
      const name=document.querySelector('#app-view [data-active-tag-name]')?.dataset.activeTagName;if(!name)return;
      const item=document.createElement('li');item.dataset.transientTag='';
      const link=document.createElement('a');link.className='nav-item';link.href=activeHref;link.title=name;
      link.innerHTML='<span class="tag-glyph"><svg class="i" aria-hidden="true"><use href="#i-tag"></use></svg></span><span class="nav-label"></span>';
      link.querySelector('.nav-label').textContent=name;item.append(link);
      const more=list.querySelector(':scope > .nav-tags-more, :scope > .nav-tags-all');list.insertBefore(item,more);
    };
    document.addEventListener('input',event=>{
      const input=event.target.closest?.('[data-sidebar-tag-filter]');if(!input)return;
      const root=input.closest('[data-sidebar-tags-all]'),needle=input.value.trim().toLowerCase();let shown=0;
      root.querySelectorAll('[data-tag-name]').forEach(item=>{const hit=!needle||item.dataset.tagName.includes(needle);item.hidden=!hit;if(hit)shown+=1;});
      root.querySelector('[data-sidebar-tag-empty]').hidden=shown>0;
    });
    document.addEventListener('htmx:afterSwap',event=>{
      const all=event.detail?.requestConfig?.path==='/ui/sidebar-tags'?document.querySelector('[data-sidebar-tags-all]'):null;
      if(all){syncSidebar();all.querySelector('[data-sidebar-tag-filter]')?.focus();}
    });
    const syncSidebar=()=>{
      const path=location.pathname;
      const query=new URLSearchParams(location.search);let activeHref=null;
      if(path==='/'){
        if(query.get('ask')==='true'||query.has('ask_thread'))activeHref='/ask';
        else if(query.has('folder'))activeHref=`/?folder=${encodeURIComponent(query.get('folder'))}`;
        else if(query.has('tag'))activeHref=`/?tag=${encodeURIComponent(query.get('tag'))}`;
        else if(query.has('origin'))activeHref=`/?origin=${encodeURIComponent(query.get('origin'))}`;
        else if(query.has('scene'))activeHref=`/?scene=${encodeURIComponent(query.get('scene'))}`;
        else if(['uncategorized','trash'].includes(query.get('view')))activeHref=`/?view=${query.get('view')}`;
        else activeHref='/';
      }else if(path.startsWith('/file/'))activeHref=null;
      else activeHref=path.startsWith('/home')?'/home':path.startsWith('/ask')?'/ask':path.startsWith('/search')?'/search':path.startsWith('/notes')?'/notes':path.startsWith('/templates')?'/templates':path.startsWith('/discover')?'/discover':path.startsWith('/notifications')?'/notifications':path.startsWith('/settings')?'/settings':path.startsWith('/status')?'/status':null;
      syncSidebarTag(activeHref);
      document.querySelectorAll('.sidebar .nav-item[href]').forEach(link=>{const on=link.getAttribute('href')===activeHref;link.classList.toggle('on',on);if(on)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});
    };
    const syncFileList=()=>{
      const list=document.getElementById('recording-file-list');if(!list)return;
      const match=location.pathname.match(/^\/file\/([^/]+)\/?$/),activeId=match?decodeURIComponent(match[1]):null;
      let active=null;
      list.querySelectorAll('[data-recording-id]').forEach(card=>{const on=card.dataset.recordingId===activeId;card.classList.toggle('on',on);if(on)active=card;});
      list.classList.toggle('has-active',Boolean(activeId));
      if(active&&!Number.isFinite(pendingFileListScroll))active.scrollIntoView({block:'nearest'});
    };
    const restoreScroll=()=>{
      const state=readScroll(),view=document.getElementById('app-view');
      if(state&&view){for(const [name,selector] of Object.entries(regionSelectors)){const region=view.querySelector(selector);if(region&&Number.isFinite(state[name]))region.scrollTop=state[name];}if(Number.isFinite(state.windowY))window.scrollTo(0,state.windowY);}
      syncSidebar();syncFileList();
      const fileList=view?.querySelector('.fl-scroll');
      if(fileList&&Number.isFinite(pendingFileListScroll))fileList.scrollTop=pendingFileListScroll;
    };
    const isAppViewSwap=event=>{
      const detail=event.detail||{},trigger=detail.elt||detail.requestConfig?.elt;
      return detail.target?.id==='app-view'||event.target?.id==='app-view'||trigger?.getAttribute?.('hx-target')==='#app-view';
    };
    const isPartialNavigation=link=>{
      if(link.dataset.localplaudNav!==undefined)return true;
      if(link.target||link.hasAttribute('download')||link.dataset.noPartial!==undefined)return false;
      if((link.getAttribute('href')||'').startsWith('#'))return false;
      const url=new URL(link.href,location.href);
      if(url.origin!==location.origin)return false;
      return !/^\/(?:api|audio|static|login|logout|oauth)(?:\/|$)/.test(url.pathname)
        && !/\/(?:export|download)(?:\/|\.|$)/.test(url.pathname);
    };
    const prepareLinks=root=>{
      const links=[];
      if(root?.matches?.('a[href]'))links.push(root);
      root?.querySelectorAll?.('a[href]').forEach(link=>links.push(link));
      links.forEach(link=>{
        if(!isPartialNavigation(link)){link.setAttribute('hx-boost','false');return;}
        if(link.hasAttribute('hx-get'))return;
        const url=new URL(link.href,location.href);
        link.setAttribute('hx-get',`${url.pathname}${url.search}${url.hash}`);
        link.setAttribute('hx-target','#app-view');link.setAttribute('hx-select','#app-view');
        link.setAttribute('hx-swap','outerHTML');link.setAttribute('hx-push-url','true');
      });
    };
    document.addEventListener('htmx:beforeProcessNode',event=>prepareLinks(event.detail?.elt));
    document.addEventListener('htmx:configRequest',event=>{
      const trigger=event.detail?.elt||event.detail?.requestConfig?.elt;
      if(event.detail?.target?.id==='app-view'&&document.getElementById('recording-file-list')&&!trigger?.closest?.('[data-replace-filelist]'))event.detail.headers['X-Localplaud-Preserve-Filelist']='true';
    });
    prepareLinks(document);
    // htmx already processed the initial DOM before this ran; bind the links we just prepared.
    document.querySelectorAll('a[hx-get][hx-target="#app-view"]').forEach(link => window.htmx?.process(link));
    document.addEventListener('click',event=>{
      if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
      const link=event.target.closest('a[href]');
      if(!link)return;
      if(link.getAttribute('hx-target')==='#app-view'){
        const fileList=document.querySelector('#app-view .fl-scroll');pendingFileListScroll=link.hasAttribute('data-replace-filelist')?0:(fileList?.scrollTop??null);captureScroll();return;
      }
      if(link.hasAttribute('hx-get')||!isPartialNavigation(link))return;
      event.preventDefault();event.stopImmediatePropagation();captureScroll();
      const url=new URL(link.href,location.href);
      link.setAttribute('hx-get',`${url.pathname}${url.search}${url.hash}`);
      link.setAttribute('hx-target','#app-view');link.setAttribute('hx-select','#app-view');
      link.setAttribute('hx-swap','outerHTML');link.setAttribute('hx-push-url','true');
      window.htmx.process(link);queueMicrotask(()=>link.click());
    },true);
    document.addEventListener('htmx:beforeHistorySave',captureScroll);
    document.addEventListener('htmx:beforeRequest',event=>{if(isAppViewSwap(event)&&!event.detail?.elt?.matches?.('[data-progressive-loader]')){const fileList=document.querySelector('#app-view .fl-scroll');pendingFileListScroll=fileList?.scrollTop??null;captureScroll();document.body.classList.add('is-navigating');}});
    document.addEventListener('htmx:afterSwap',event=>{if(Number.isFinite(pendingFileListScroll)||isAppViewSwap(event))requestAnimationFrame(restoreScroll);});
    document.addEventListener('htmx:afterSettle',event=>{if(Number.isFinite(pendingFileListScroll)||isAppViewSwap(event)){
      document.body.classList.remove('is-navigating');lp.closeMenus();
      requestAnimationFrame(restoreScroll);
      setTimeout(restoreScroll,50);
      setTimeout(()=>{restoreScroll();if(!document.querySelector('[data-progressive-loader]'))pendingFileListScroll=null;},150);
      document.dispatchEvent(new CustomEvent('lp:navigated'));
    }});
    document.addEventListener('htmx:responseError',()=>document.body.classList.remove('is-navigating'));
    document.addEventListener('htmx:historyRestore',()=>{
      requestAnimationFrame(restoreScroll);setTimeout(restoreScroll,50);setTimeout(restoreScroll,150);
    });
    const askRequests=new WeakMap();
    const askFormFromEvent=event=>event.detail?.elt?.closest?.('[data-ask-request]');
    const askErrorMessage=xhr=>{if([404,409,422].includes(xhr?.status)){const text=(xhr.responseText||'').trim();try{const data=JSON.parse(text||'{}'),detail=data.detail,item=Array.isArray(detail)?detail[0]?.msg:detail;if(typeof item==='string')return window.localplaudT(item);}catch(_error){if(text&&text.length<=300&&!text.includes('<'))return window.localplaudT(text);}}return window.localplaudT('Could not show answer');};
    const finishAskRequest=(state,event,successful)=>{if(!state)return;const {forms,controls,status,target,question,focusTarget}=state;for(const form of forms)delete form.dataset.askBusy;for(const {control,disabled} of controls)control.disabled=disabled;target?.removeAttribute('aria-busy');if(successful){if(question?.isConnected)question.value='';if(status?.isConnected){status.textContent=window.localplaudT('Answer ready');status.classList.remove('error');}}else{if(status?.isConnected){status.textContent=[askErrorMessage(event.detail?.xhr),window.localplaudT('Check History before retrying to avoid a duplicate conversation.')].join(' ');status.classList.add('error');}requestAnimationFrame(()=>{(question?.isConnected?question:focusTarget?.isConnected?focusTarget:null)?.focus();});}};
    document.addEventListener('htmx:beforeRequest',event=>{const form=askFormFromEvent(event),xhr=event.detail?.xhr;if(!form||!xhr)return;const statusId=form.dataset.askStatus,forms=[...document.querySelectorAll('[data-ask-request]')].filter(candidate=>candidate.dataset.askStatus===statusId);if(forms.some(candidate=>candidate.dataset.askBusy==='true')){event.preventDefault();return;}const controls=forms.flatMap(candidate=>[...candidate.querySelectorAll('button[type="submit"],input[type="submit"]')]).map(control=>({control,disabled:control.disabled})),status=document.getElementById(statusId),target=event.detail?.target||document.querySelector(form.getAttribute('hx-target')),question=form.querySelector('[data-ask-question]'),focusTarget=question||form.querySelector('[type="submit"]');for(const candidate of forms)candidate.dataset.askBusy='true';for(const {control} of controls)control.disabled=true;askRequests.set(xhr,{forms,controls,status,target,question,focusTarget});if(status){status.textContent=window.localplaudT('Getting answer…');status.classList.remove('error');}target?.setAttribute('aria-busy','true');});
    document.addEventListener('htmx:afterRequest',event=>{const xhr=event.detail?.xhr,state=xhr?askRequests.get(xhr):null;if(!state)return;askRequests.delete(xhr);const successful=event.detail?.successful===true||(xhr.status>=200&&xhr.status<400);finishAskRequest(state,event,successful);});
    document.addEventListener('htmx:responseError',event=>{
      if(event.detail?.elt?.matches?.('[data-progressive-loader]'))document.body.classList.add('progressive-failed');
    });
    document.addEventListener('click',event=>{
      if(!event.target.closest('[data-progressive-retry]'))return;
      document.body.classList.remove('progressive-failed');
      const loader=document.querySelector('[data-progressive-loader]');if(loader)window.htmx.trigger(loader,'load');
    });
    document.addEventListener('htmx:beforeRequest',event=>{
      if(event.detail?.elt?.matches?.('[data-progressive-loader]'))document.body.classList.remove('progressive-failed');
    });
    window.addEventListener('pagehide',captureScroll);
    requestAnimationFrame(restoreScroll);
  });

})();
