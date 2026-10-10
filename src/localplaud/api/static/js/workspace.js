/* localplaud recording workspace: player, synchronized transcript, inline
 * correction, speakers, mind map, and phone sheets.
 *
 * Loaded as a classic script at the end of detail.html, so it re-runs on every
 * full load and every HTMX #app-view swap. Each run binds to the current DOM and
 * tears itself down when #app-view is cleaned up or the next run starts.
 * User-visible strings go through window.localplaudT (see i18n.py).
 */
(() => {
  'use strict';
  const configElement = document.getElementById('ws-config');
  if (!configElement) return;
  const cfg = JSON.parse(configElement.textContent || '{}');
  const tr = window.localplaudT || window.lp?.t || (message => message);
  const controller = new AbortController();
  const { signal } = controller;
  window.localplaudWorkspaceTeardown?.();
  window.localplaudWorkspaceTeardown = () => controller.abort();
  const appView = document.getElementById('app-view');
  appView?.addEventListener('htmx:beforeCleanupElement', event => { if (event.target === appView) controller.abort(); }, { signal });

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const on = (target, type, handler, options = {}) => target?.addEventListener(type, handler, { signal, ...options });
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const phone = window.matchMedia('(max-width: 820px)');
  const scrollBehavior = () => (reduceMotion.matches ? 'auto' : 'smooth');
  const stamp = seconds => {
    const total = Math.max(0, Math.floor(seconds || 0)), h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  };
  const isTyping = target => target?.closest?.('input,textarea,select,[contenteditable="true"],[contenteditable=""]');
  const speakerVars = ['--sp0', '--sp1', '--sp2', '--sp3', '--sp4', '--sp5', '--sp6', '--sp7'];
  const colorFor = key => (key && window.spkColor ? `var(${speakerVars[window.spkColor(key)]})` : '');

  // Shared live state (the inline find/replace reads the current revision).
  const state = window.localplaudWorkspaceState = {
    revision: Number(cfg.revision || 0),
    view: cfg.view || 'corrected',
    speakers: new Map((cfg.speakers || []).map(item => [item.key, item.name || ''])),
  };
  const anonymousSpeakerLabel = key => {
    const label = cfg.fallback_names?.[key] || key;
    const numbered = /^Speaker (\d+)$/.exec(label || '');
    return numbered ? tr('Speaker {number}').replace('{number}', numbered[1]) : label;
  };
  const speakerLabel = key => state.speakers.get(key) || anonymousSpeakerLabel(key);
  // Sprite icons (<svg class="i"><use href="#i-NAME">) with a legacy mask fallback.
  function setIcon(node, name) {
    if (!node) return;
    const use = node.querySelector?.('use');
    if (use) use.setAttribute('href', `#i-${name}`);
    else node.style?.setProperty('--nav-icon', `url('/static/lucide/${name}.svg')`);
  }
  // Chapter outline state (filled from /api/files/{id}/outline when present).
  const outline = { chapters: [] };
  const outlineSection = $('[data-outline]');
  const chapterNow = document.createElement('span');
  chapterNow.className = 'ws-chapter-now';
  chapterNow.setAttribute('aria-live', 'off');
  document.getElementById('player-clock')?.before(chapterNow);

  // ---------------------------------------------------------------- toast
  function toast(message, { error = false } = {}) {
    // Prefer the shared design-system toast (CONVENTIONS: lp.toast) when present.
    if (typeof window.lp?.toast === 'function') { window.lp.toast(message, { type: error ? 'error' : 'success' }); return; }
    let host = document.getElementById('ws-toast');
    if (!host) {
      host = document.createElement('div');
      host.id = 'ws-toast'; host.className = 'ws-toast';
      host.setAttribute('role', 'status'); host.setAttribute('aria-live', 'polite');
      document.body.append(host);
    }
    host.textContent = message;
    host.classList.toggle('error', error);
    host.classList.add('show');
    clearTimeout(host.hideTimer);
    host.hideTimer = setTimeout(() => host.classList.remove('show'), error ? 5200 : 2600);
  }

  // ------------------------------------------------------- transcript index
  const transcript = document.getElementById('transcript');
  const transcriptPanel = document.getElementById('recording-panel-transcript');
  const index = { dirty: true, segments: [], starts: [] };
  const segmentsInOrder = () => {
    if (index.dirty) {
      index.segments = transcript ? $$('.seg[data-start]', transcript) : [];
      index.starts = index.segments.map(seg => parseFloat(seg.dataset.start) || 0);
      index.dirty = false;
    }
    return index.segments;
  };
  if (transcript) {
    const observer = new MutationObserver(() => { index.dirty = true; });
    observer.observe(transcript, { childList: true });
    signal.addEventListener('abort', () => observer.disconnect(), { once: true });
  }
  const colorize = root => {
    const segs = root.matches?.('.seg[data-spk]') ? [root] : [];
    segs.push(...$$('.seg[data-spk]', root));
    segs.forEach(seg => seg.style.setProperty('--c', colorFor(seg.dataset.spk)));
  };

  // Load further transcript pages until `seconds` is covered (deep links,
  // seeking, and following playback beyond the lazily loaded window).
  let loading = null;
  async function ensureLoaded(seconds) {
    if (!transcript) return;
    if (loading) return loading;
    loading = (async () => {
      for (let guard = 0; guard < 80 && !signal.aborted; guard += 1) {
        const segs = segmentsInOrder();
        const last = segs.at(-1);
        if (last && (parseFloat(last.dataset.end) || parseFloat(last.dataset.start) || 0) >= seconds) return;
        const loader = $('.transcript-page-loader', transcript);
        if (!loader || loader.classList.contains('htmx-request') || !loader.getAttribute('hx-get')) return;
        const url = new URL(loader.getAttribute('hx-get'), location.href);
        url.searchParams.set('limit', '200');
        const placeholder = loader.cloneNode(true);
        ['hx-get', 'hx-trigger', 'hx-target', 'hx-swap'].forEach(name => placeholder.removeAttribute(name));
        loader.before(placeholder);
        window.htmx?.remove(loader);
        try {
          const response = await fetch(url, { signal, credentials: 'same-origin', headers: { 'HX-Request': 'true' } });
          if (!response.ok) throw new Error(String(response.status));
          const template = document.createElement('template');
          template.innerHTML = await response.text();
          if (!placeholder.isConnected) return;
          const nextLoader = template.content.querySelector('.transcript-page-loader');
          nextLoader?.remove();
          const inserted = [...template.content.children];
          placeholder.before(template.content);
          inserted.forEach(node => { window.htmx?.process(node); colorize(node); });
          index.dirty = true;
          if (nextLoader) { placeholder.replaceWith(nextLoader); window.htmx?.process(nextLoader); } else placeholder.remove();
        } catch (error) {
          if (placeholder.isConnected) {
            placeholder.setAttribute('hx-get', url.pathname + url.search.replace(/([?&])limit=200/, '$1limit=120'));
            placeholder.setAttribute('hx-trigger', 'intersect once threshold:0.1');
            placeholder.setAttribute('hx-target', 'this');
            placeholder.setAttribute('hx-swap', 'outerHTML');
            window.htmx?.process(placeholder);
          }
          return;
        }
      }
    })().finally(() => { loading = null; });
    return loading;
  }

  // After an edit, later lazily loaded pages must come from the new revision.
  function repinLoader(revision) {
    const loader = transcript && $('.transcript-page-loader', transcript);
    if (!loader || loader.classList.contains('htmx-request') || !loader.getAttribute('hx-get')) return;
    const url = new URL(loader.getAttribute('hx-get'), location.href);
    ['page_transcript_id', 'page_transcript_token', 'revision'].forEach(name => url.searchParams.delete(name));
    url.searchParams.set('view', 'corrected');
    url.searchParams.set('page_revision', String(revision));
    const fresh = loader.cloneNode(true);
    fresh.setAttribute('hx-get', url.pathname + url.search);
    loader.before(fresh);
    window.htmx?.remove(loader);
    window.htmx?.process(fresh);
  }

  function setRevision(revision) {
    state.revision = Number(revision);
    $$('.segedit input[name="base_revision"]').forEach(input => { input.value = String(revision); });
    if (state.view !== 'corrected') {
      state.view = 'corrected';
      $$('.segedit input[name="view"]').forEach(input => { input.value = 'corrected'; });
      const url = new URL(location.href);
      url.searchParams.set('view', 'corrected');
      history.replaceState(history.state, '', url);
    }
    repinLoader(revision);
  }

  // ---------------------------------------------------------------- player
  // Shared app-wide player (app.js): reuses the live element when this recording is
  // already playing from another page, so playback continues across navigation.
  const pagePlayer = document.getElementById('player');
  const player = window.lp?.player?.attach
    ? window.lp.player.attach(pagePlayer, { fileId: cfg.fileId, title: cfg.title, href: `/file/${encodeURIComponent(cfg.fileId)}?tab=transcript` })
    : pagePlayer;
  const followPill = document.getElementById('ws-follow');
  let following = true, activeSegment = null, activeWord = null, lastProgrammaticScroll = 0;

  const transcriptVisible = () => transcriptPanel && !transcriptPanel.hidden;
  function updateFollowPill() {
    if (!followPill) return;
    const show = !following && transcriptVisible() && player && !player.paused;
    followPill.hidden = !show;
  }
  // Plaud parity: the active paragraph is brought to the top of the reading
  // area (just under the sticky tabs/toolbar, via CSS scroll-margin-top).
  function scrollToSegment(seg, force = false) {
    if (!seg || !transcriptVisible()) return;
    const margin = parseFloat(getComputedStyle(seg).scrollMarginTop) || 0;
    const top = seg.getBoundingClientRect().top;
    if (!force && Math.abs(top - margin) < 24) return;
    lastProgrammaticScroll = Date.now();
    seg.scrollIntoView({ block: 'start', behavior: scrollBehavior() });
  }
  function updateActive(seconds) {
    const segs = segmentsInOrder();
    let low = 0, high = segs.length - 1, found = -1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (index.starts[mid] <= seconds) { found = mid; low = mid + 1; } else high = mid - 1;
    }
    let seg = found >= 0 ? segs[found] : null;
    if (seg) {
      const end = parseFloat(seg.dataset.end) || 0;
      if (end && seconds > end + 1.5) seg = null;
    }
    if (seg !== activeSegment) {
      activeSegment?.classList.remove('active');
      activeSegment?.removeAttribute('aria-current');
      seg?.classList.add('active');
      seg?.setAttribute('aria-current', 'time');
      activeSegment = seg;
      if (seg && following && player && !player.paused) scrollToSegment(seg);
    }
    let word = null;
    if (seg) {
      for (const span of seg.querySelectorAll('.w')) {
        if (parseFloat(span.dataset.s) <= seconds) word = span; else break;
      }
      if (word && seconds > parseFloat(word.dataset.e) + 0.6) word = null;
    }
    if (word !== activeWord) {
      activeWord?.classList.remove('on');
      word?.classList.add('on');
      activeWord = word;
    }
    const last = segs.at(-1);
    if (!seg && last && seconds > (parseFloat(last.dataset.end) || 0) && player && !player.paused && following) {
      ensureLoaded(seconds + 30).then(() => { if (!signal.aborted) updateActive(player.currentTime); });
    }
    updateFollowPill();
  }

  // --- deep-link helpers (exercised in Node by tests/test_playback_deeplink.py)
  // ?t= accepts only finite, non-negative seconds; anything else is ignored.
  function deepLinkSeconds(search) {
    const raw = new URLSearchParams(search).get('t');
    if (raw === null) return null;
    const s = parseFloat(raw);
    return Number.isFinite(s) && s >= 0 ? s : null;
  }
  function playbackRestoreSeconds(search, stored, reusingLivePlayer) {
    const linked = deepLinkSeconds(search);
    if (linked !== null) return linked;
    // The parked element kept playing while this workspace's storage listener
    // was torn down. Its current position is newer than sessionStorage.
    if (reusingLivePlayer) return null;
    const seconds = parseInt(stored || '', 10);
    return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
  }
  // Cached media may already have metadata (seek now); cold media must load
  // first, otherwise the browser resets currentTime when metadata arrives.
  function seekWhenReady(media, seconds, abortSignal, after) {
    const apply = () => { media.currentTime = seconds; after?.(); };
    if (media.readyState >= 1) apply();
    else { media.addEventListener('loadedmetadata', apply, { once: true, signal: abortSignal }); media.load(); }
  }
  // --- end deep-link helpers

  function seekTo(seconds, { play = false, follow = true } = {}) {
    if (!player || !Number.isFinite(seconds)) return;
    const target = Math.max(0, Math.min(Number.isFinite(player.duration) ? player.duration : Infinity, seconds));
    seekWhenReady(player, target, signal, () => { if (play) player.play().catch(() => {}); });
    if (follow) {
      following = true;
      ensureLoaded(seconds).then(() => {
        if (signal.aborted) return;
        updateActive(seconds);
        scrollToSegment(activeSegment, false);
      });
    }
  }

  if (player) {
    const toggle = document.getElementById('player-toggle');
    const toggleIcon = document.getElementById('player-toggle-icon');
    const progress = document.getElementById('player-progress');
    const clock = document.getElementById('player-clock');
    const current = document.getElementById('player-current');
    const speed = document.getElementById('player-speed');
    const canvas = document.getElementById('waveform');
    const context = canvas?.getContext('2d');
    const knownDuration = Number(cfg.durationSeconds || 0);
    let peaks = [];
    const duration = () => (Number.isFinite(player.duration) && player.duration > 0 ? player.duration : knownDuration);
    const colors = () => {
      const style = getComputedStyle(canvas);
      return { played: style.getPropertyValue('--ws-wave-played').trim() || '#111', rest: style.getPropertyValue('--ws-wave').trim() || '#cbd5e1' };
    };
    const draw = () => {
      if (!canvas || !context) return;
      const ratio = window.devicePixelRatio || 1, width = Math.max(1, canvas.clientWidth), height = Math.max(1, canvas.clientHeight);
      canvas.width = width * ratio; canvas.height = height * ratio;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      const played = duration() ? player.currentTime / duration() : 0;
      const { played: playedColor, rest } = colors();
      const bars = peaks.length ? peaks : Array(Math.max(24, Math.floor(width / 4))).fill(0.12);
      const step = width / bars.length, barWidth = Math.max(1, step * 0.6);
      bars.forEach((peak, i) => {
        const x = i * step, h = Math.max(2, Math.min(1, peak) * (height - 2));
        context.fillStyle = (x + barWidth / 2) / width <= played ? playedColor : rest;
        context.fillRect(x, (height - h) / 2, barWidth, h);
      });
    };
    const sync = () => {
      const t = player.currentTime, d = duration();
      if (progress && !progress.matches(':active')) progress.value = d ? String(Math.round((t / d) * 1000)) : '0';
      progress?.setAttribute('aria-valuetext', `${stamp(t)} / ${stamp(d)}`);
      if (clock) clock.textContent = `${stamp(t)} / ${stamp(d)}`;
      if (current) current.textContent = stamp(t);
      setIcon(toggleIcon, player.paused ? 'play' : 'pause');
      toggle?.setAttribute('aria-label', player.paused ? tr('Play') : tr('Pause'));
      document.getElementById('persistent-player')?.classList.toggle('playing', !player.paused);
      updateActive(t);
      draw();
      updateChrome(t);
    };
    const togglePlay = () => (player.paused ? player.play().catch(() => {}) : player.pause());
    const skip = delta => seekTo(player.currentTime + delta, { follow: following });
    on(toggle, 'click', togglePlay);
    on(document.getElementById('player-back'), 'click', () => skip(-15));
    on(document.getElementById('player-forward'), 'click', () => skip(15));
    // Speed: Plaud-style popover (bottom sheet on phones) with checked rate.
    const speedMenu = document.getElementById('player-speed-menu');
    const rates = speedMenu ? $$('[data-rate]', speedMenu).map(item => item.dataset.rate) : ['1'];
    const setRate = (rate, { remember = true } = {}) => {
      if (!rates.includes(String(rate))) return;
      player.playbackRate = Number(rate);
      speed.dataset.rate = String(rate);
      speed.textContent = String(rate) + "×";
      $$('[data-rate]', speedMenu).forEach(item => item.setAttribute('aria-checked', String(item.dataset.rate === String(rate))));
      if (remember) { try { localStorage.setItem('localplaud:playback-rate', String(rate)); } catch (_error) { /* storage unavailable */ } }
    };
    const setSpeedMenu = open => {
      speedMenu.hidden = !open;
      speed.setAttribute('aria-expanded', String(open));
      if (open) requestAnimationFrame(() => ($('[aria-checked="true"]', speedMenu) || $('[data-rate]', speedMenu))?.focus());
    };
    on(speed, 'click', event => { event.stopPropagation(); setSpeedMenu(speedMenu.hidden); });
    on(speedMenu, 'click', event => {
      const item = event.target.closest('[data-rate]');
      if (!item) return;
      setRate(item.dataset.rate);
      setSpeedMenu(false);
      speed.focus();
    });
    on(speedMenu, 'keydown', event => {
      const items = $$('[data-rate]', speedMenu), index = items.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length].focus(); }
      else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setSpeedMenu(false); speed.focus(); }
      else if (event.key === 'Tab') setSpeedMenu(false);
    });
    on(document, 'click', event => { if (!speedMenu.hidden && !event.target.closest('.ws-speed')) setSpeedMenu(false); });
    try { const stored = localStorage.getItem('localplaud:playback-rate'); if (stored) setRate(stored, { remember: false }); } catch (_error) { /* storage unavailable */ }
    on(progress, 'input', () => { const d = duration(); if (d) { player.currentTime = (Number(progress.value) / 1000) * d; sync(); } });
    on(progress, 'change', () => { following = true; scrollToSegment(activeSegment, true); });
    ['loadedmetadata', 'timeupdate', 'play', 'pause', 'ratechange', 'ended', 'seeked'].forEach(name => on(player, name, sync));
    on(player, 'play', () => { following = true; });
    if (canvas) {
      const resize = new ResizeObserver(draw);
      resize.observe(canvas);
      signal.addEventListener('abort', () => resize.disconnect(), { once: true });
    }
    let waveformTimer = null;
    signal.addEventListener('abort', () => { if (waveformTimer) clearTimeout(waveformTimer); if (window.lp?.player?.audio !== player || !window.lp.player.parked) player.pause(); }, { once: true });
    const loadWaveform = async () => {
      if (signal.aborted) return;
      try {
        const response = await fetch(`/audio/${encodeURIComponent(cfg.fileId)}/waveform?buckets=240`, { signal });
        if (response.status === 202) { waveformTimer = setTimeout(loadWaveform, 1200); return; }
        if (!response.ok) return;
        const data = await response.json();
        if (Array.isArray(data.peaks)) { peaks = data.peaks; draw(); }
      } catch (_error) { /* waveform is decorative; the range input still seeks */ }
    };
    loadWaveform();
    sync();

    // Keyboard: Space/K play-pause, ←/→ 5 s (Shift 30 s), J/L 15 s, [ ] speed.
    on(document, 'keydown', event => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTyping(event.target) || document.body.classList.contains('dialog-open')) return;
      const onControl = event.target?.closest?.('button,a,summary,[role="tab"]');
      const key = event.key;
      if ((key === ' ' && !onControl) || key === 'k' || key === 'K') { event.preventDefault(); togglePlay(); }
      else if (key === 'ArrowLeft' || key === 'ArrowRight') {
        if (event.target?.closest?.('[role="tablist"],#player-progress')) return;
        event.preventDefault();
        skip((key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 30 : 5));
      } else if (key === 'j' || key === 'J') { event.preventDefault(); skip(-15); }
      else if (key === 'l' || key === 'L') { event.preventDefault(); skip(15); }
      else if (key === '[' || key === ']') {
        const i = Math.max(0, rates.indexOf(speed.dataset.rate || '1'));
        const next = rates[Math.max(0, Math.min(rates.length - 1, i + (key === ']' ? 1 : -1)))];
        setRate(next);
        toast(`${tr('Playback speed')}: ${next}×`);
      }
    });

    if ('mediaSession' in navigator) {
      try {
        navigator.mediaSession.metadata = new window.MediaMetadata({ title: cfg.title || 'localplaud', artist: 'localplaud', artwork: [{ src: '/static/logo.svg', type: 'image/svg+xml' }] });
        navigator.mediaSession.setActionHandler('play', () => player.play());
        navigator.mediaSession.setActionHandler('pause', () => player.pause());
        navigator.mediaSession.setActionHandler('seekbackward', () => skip(-15));
        navigator.mediaSession.setActionHandler('seekforward', () => skip(15));
        navigator.mediaSession.setActionHandler('seekto', details => seekTo(details.seekTime, { follow: following }));
      } catch (_error) { /* optional platform integration */ }
    }

    // Deep link ?t=seconds seeks without autoplay; otherwise restore the
    // position from this tab's last visit (survives reloads mid-listen).
    const positionKey = `localplaud:playback:${cfg.fileId}`;
    const linked = deepLinkSeconds(location.search);
    let stored;
    try { stored = sessionStorage.getItem(positionKey); } catch (_error) { /* storage unavailable */ }
    const restoreAt = playbackRestoreSeconds(location.search, stored, player !== pagePlayer);
    if (restoreAt !== null) {
      seekTo(restoreAt, { follow: false });
      const reveal = () => ensureLoaded(restoreAt).then(() => {
        if (signal.aborted) return;
        updateActive(restoreAt);
        if (linked !== null) scrollToSegment(activeSegment, true);
      });
      if (transcript && !$('.seg', transcript)) on(transcript, 'htmx:afterSwap', reveal, { once: true });
      else reveal();
    }
    let lastStored = -1;
    on(player, 'timeupdate', () => {
      const second = Math.floor(player.currentTime);
      if (second === lastStored) return;
      lastStored = second;
      try { if (second > 0) sessionStorage.setItem(positionKey, String(second)); else sessionStorage.removeItem(positionKey); } catch (_error) { /* storage unavailable */ }
    });
  }

  // Following pauses when the reader scrolls on their own; the pill returns.
  // Manual scrolling pauses following; it resumes after 5 s without scrolling
  // (or immediately through the "Back to current" pill).
  let resumeTimer = null;
  const stopFollowing = event => {
    if (!player || player.paused || Date.now() - lastProgrammaticScroll < 120) return;
    if (event.type === 'keydown' && !['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    if (event.type === 'keydown' && isTyping(event.target)) return;
    if (!transcriptVisible() || event.target?.closest?.('.ws-speaker-sheet,.import-backdrop,.ask-dock,.mindmap-viewport')) return;
    following = false;
    updateFollowPill();
    clearTimeout(resumeTimer);
    resumeTimer = setTimeout(() => {
      if (signal.aborted || following || !player || player.paused || $('.seg.editing')) return;
      following = true;
      updateFollowPill();
      scrollToSegment(activeSegment, true);
    }, 5000);
  };
  signal.addEventListener('abort', () => clearTimeout(resumeTimer), { once: true });
  ['wheel', 'touchmove'].forEach(type => on(window, type, stopFollowing, { passive: true }));
  on(window, 'keydown', stopFollowing);
  on(followPill, 'click', () => {
    following = true;
    updateFollowPill();
    if (activeSegment) scrollToSegment(activeSegment, true);
    else if (player) seekTo(player.currentTime, { follow: true });
  });

  // Click a word or paragraph to seek; timestamp buttons and Ask citations
  // carry data-seek. Editing controls never seek.
  on(document, 'click', event => {
    const seekButton = event.target.closest('[data-seek]');
    if (seekButton) {
      event.preventDefault();
      if (!player) return;
      seekTo(parseFloat(seekButton.dataset.seek) || 0, { play: true });
      if (seekButton.closest('#recording-panel-ask') && phone.matches) toast(`${tr('Playing from')} ${stamp(parseFloat(seekButton.dataset.seek) || 0)}`);
      return;
    }
    const seg = event.target.closest('.seg[data-start]');
    if (!seg || event.target.closest('button,input,textarea,select,form,a') || !player) return;
    if (window.getSelection && String(window.getSelection()).length > 0) return;
    const word = event.target.closest('.w[data-s]');
    seekTo(parseFloat(word ? word.dataset.s : seg.dataset.start) || 0, { play: true });
  });

  // ---------------------------------------------------- inline correction
  const autosize = area => { area.style.height = 'auto'; area.style.height = `${Math.min(window.innerHeight * 0.5, area.scrollHeight + 2)}px`; };
  function resetSegmentEditor(form) {
    form.reset();
    form.querySelector('[data-segment-status]').textContent = '';
    form.querySelector('[type="submit"]').disabled = false;
    form.hidden = true;
    form.closest('.seg')?.classList.remove('editing');
    form.parentElement.querySelector('.editbtn')?.focus();
  }
  function openSegmentEditor(seg) {
    const form = seg?.querySelector('.segedit');
    if (!form) return;
    if (!form.hidden) { resetSegmentEditor(form); return; }
    $$('.seg .segedit:not([hidden])').forEach(other => { if (other !== form) resetSegmentEditor(other); });
    form.hidden = false;
    seg.classList.add('editing');
    const area = form.querySelector('textarea');
    autosize(area);
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  }
  on(document, 'click', event => {
    const button = event.target.closest('.seg .editbtn');
    if (!button) return;
    event.stopPropagation();
    openSegmentEditor(button.closest('.seg'));
  });
  // Double-click (or long text selection-free double tap) also edits.
  on(document, 'dblclick', event => {
    const seg = event.target.closest('.seg[data-start]');
    if (!seg || event.target.closest('button,input,textarea,select,form')) return;
    const form = seg.querySelector('.segedit');
    if (!form || !form.hidden) return;
    window.getSelection?.()?.removeAllRanges?.();
    openSegmentEditor(seg);
  });
  on(document, 'input', event => { if (event.target.matches?.('.segedit textarea')) autosize(event.target); });
  on(document, 'click', event => {
    const cancel = event.target.closest('.seg .segedit [data-cancel]');
    if (!cancel) return;
    event.stopPropagation();
    resetSegmentEditor(cancel.closest('.segedit'));
  });
  on(document, 'keydown', event => {
    const form = event.target.closest?.('.seg .segedit');
    if (!form) return;
    if (event.key === 'Escape') { event.preventDefault(); form.querySelector('[data-cancel]').click(); }
    else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); form.requestSubmit(); }
  });

  function applySpeaker(seg, key) {
    const meta = seg.querySelector('.seg-meta');
    let chip = meta?.querySelector('.who');
    if (key) {
      seg.dataset.spk = key;
      seg.classList.add('spk');
      seg.style.setProperty('--c', colorFor(key));
      if (!chip && meta) {
        chip = document.createElement('button');
        chip.type = 'button'; chip.className = 'who'; chip.dataset.speakerMenu = '';
        chip.setAttribute('aria-haspopup', 'dialog');
        chip.innerHTML = '<span class="dot"></span><span class="who-name"></span>';
        meta.prepend(chip);
      }
      chip?.classList.remove('who-none');
      if (chip) { chip.title = speakerLabel(key); chip.querySelector('.who-name').textContent = speakerLabel(key); }
    } else {
      delete seg.dataset.spk;
      seg.classList.remove('spk');
      seg.style.removeProperty('--c');
      if (chip) { chip.classList.add('who-none'); chip.title = ''; chip.querySelector('.who-name').textContent = tr('No speaker'); }
    }
    const select = seg.querySelector('.segedit select[name="speaker"]');
    if (select) {
      const value = key || '__none__';
      [...select.options].forEach(option => { option.defaultSelected = option.value === value; });
      select.value = value;
    }
  }

  const saveErrors = {
    stale_revision: 'Transcript changed. Reload before saving.',
    recording_processing: 'Recording is processing. Wait until it finishes before editing.',
  };
  async function postSegment(form, { text, speaker }) {
    const body = new FormData(form);
    body.set('base_revision', String(state.revision));
    body.set('view', state.view);
    if (text !== undefined) body.set('text', text);
    if (speaker !== undefined) body.set('speaker', speaker);
    if (player) { const second = Math.floor(player.currentTime); body.set('t', second > 0 ? String(second) : ''); }
    const response = await fetch(form.action, { method: 'POST', headers: { accept: 'application/json' }, body, signal });
    let result = {};
    try { result = await response.json(); } catch (_error) { /* non-JSON error */ }
    if (!response.ok) throw new Error(tr(saveErrors[result.code] || result.error || 'Could not save'));
    if (result.changed) { setRevision(result.revision); loadOutline(); }
    return result;
  }

  // Optimistic save: the corrected text shows immediately and is rolled back
  // with the editor reopened (draft intact) if the server rejects it.
  on(document, 'submit', async event => {
    const form = event.target.closest?.('.seg .segedit');
    if (!form) return;
    event.preventDefault();
    const seg = form.closest('.seg'), paragraph = seg.querySelector('.seg-text');
    const status = form.querySelector('[data-segment-status]'), save = form.querySelector('[type="submit"]');
    const area = form.querySelector('textarea'), select = form.querySelector('select[name="speaker"]');
    const text = area.value, speaker = select ? select.value : '__unchanged__';
    const before = { html: paragraph.innerHTML, key: seg.dataset.spk || null };
    paragraph.textContent = text;
    if (select) applySpeaker(seg, speaker === '__none__' ? null : speaker);
    form.hidden = true;
    seg.classList.remove('editing');
    seg.classList.add('saving');
    save.disabled = true;
    try {
      const result = await postSegment(form, { text, speaker });
      area.defaultValue = text;
      seg.classList.remove('saving');
      seg.classList.add('saved');
      setTimeout(() => seg.classList.remove('saved'), 1600);
      save.disabled = false;
      status.textContent = '';
      if (result.changed) toast(tr('Correction saved'));
      seg.querySelector('.editbtn')?.focus();
    } catch (error) {
      if (error.name === 'AbortError') return;
      paragraph.innerHTML = before.html;
      if (select) applySpeaker(seg, before.key);
      area.value = text;
      seg.classList.remove('saving');
      form.hidden = false;
      seg.classList.add('editing');
      save.disabled = false;
      status.textContent = error.message || tr('Could not save');
      area.focus();
    }
  });

  // -------------------------------------------------------------- speakers
  const sheet = document.getElementById('ws-speaker-sheet');
  let scrim = null, sheetContext = null;
  function updateSpeakerEverywhere(key) {
    const label = speakerLabel(key);
    $$(`.seg[data-spk="${CSS.escape(key)}"] .who`).forEach(chip => { chip.title = label; chip.querySelector('.who-name').textContent = label; });
    $$(`.legend .swatch[data-spk="${CSS.escape(key)}"]`).forEach(swatch => {
      const pill = swatch.closest('.speaker-pill');
      const text = swatch.nextElementSibling;
      if (text) text.textContent = label;
      const input = pill?.querySelector('.spk-name');
      if (input) { input.value = state.speakers.get(key) || ''; input.defaultValue = input.value; }
    });
    $$('select[name="speaker"] option, select[data-merge-target] option').forEach(option => { if (option.value === key) option.textContent = label; });
  }
  function refreshNoteHistory(host, html) {
    if (!host || typeof html !== 'string') return;
    const parsed = document.createElement('template');
    parsed.innerHTML = html;
    const fresh = parsed.content.querySelector('.note-history-tool');
    const current = host.querySelector('.note-history-tool');
    if (!fresh || !current) return;
    // Retain the details node owned by the workspace popover controller.
    current.replaceChildren(...fresh.childNodes);
    current.hidden = false;
    on(current.querySelector('[data-close-popover]'), 'click', () => {
      current.open = false; current.querySelector('summary')?.focus();
    });
    current.querySelectorAll('[data-version-preview]').forEach(button => on(button, 'click', () => {
      const preview = button.closest('[data-version-row]')?.querySelector('.note-version-preview');
      if (!preview) return;
      preview.hidden = !preview.hidden;
      button.setAttribute('aria-expanded', String(!preview.hidden));
      button.textContent = tr(preview.hidden ? 'Preview' : 'Hide preview');
    }));
  }
  function updateNamedRecordingTitle(updates) {
    if (!updates?.recording_title_changed || typeof updates.display_title !== 'string') return;
    const title = updates.display_title.trim();
    if (!title) return;
    cfg.title = title;
    document.title = `${title} — localplaud`;
    const selector = `#recording-title-display, .recording-reading-title, .ws-crumb-title, .card[data-recording-id="${CSS.escape(cfg.fileId)}"] .ct`;
    document.querySelectorAll(selector).forEach(node => { node.textContent = title; node.title = title; });
    const input = document.getElementById('recording-title');
    if (input) {
      const form = document.getElementById('recording-title-form');
      const draft = input.value;
      input.defaultValue = title;
      input.value = form && !form.hidden ? draft : title;
    }
    window.lp?.player?.updateTitle?.(cfg.fileId, title);
  }
  function applySpeakerNoteProjection(projection, updates = {}) {
    if (!projection || signal.aborted || (projection.file_id && projection.file_id !== cfg.fileId)) return;
    updateNamedRecordingTitle(updates);
    const warning = document.querySelector('[data-speaker-note-warning]');
    if (warning) warning.hidden = !(updates?.unresolved > 0);
    for (const note of projection.notes || []) {
      const panel = document.querySelector(`[data-note-panel="sum-${CSS.escape(String(note.id))}"]`);
      const prose = panel?.querySelector('[data-generated-note-prose]');
      if (!prose || typeof note.content_html !== 'string') continue;
      // HTML is rendered and sanitized by the same server renderer as initial notes.
      // Do not replace the panel: active tabs, user-authored forms and audio stay put.
      prose.innerHTML = note.content_html;
      hideRepeatedNoteTitle(prose);
      const stale = panel.querySelector('[data-note-stale]');
      if (stale) stale.hidden = !note.stale;
      refreshNoteHistory(panel, note.history_html);
    }
    state.refreshNoteOutline?.();
    if (typeof projection.mind_map?.content_md === 'string') {
      state.refreshMindMap?.(projection.mind_map.content_md);
      refreshNoteHistory(document.getElementById('recording-panel-mindmap'), projection.mind_map.history_html);
    }
  }
  async function renameSpeaker(key, name) {
    const body = new URLSearchParams({ key, name, return_to: '/', t: '' });
    const response = await fetch(`/file/${encodeURIComponent(cfg.fileId)}/speakers`, { method: 'POST', headers: { accept: 'application/json' }, body, signal });
    let result = {};
    try { result = await response.json(); } catch (_error) { /* non-JSON */ }
    if (!response.ok) throw new Error(tr(result.error || 'Could not rename speaker'));
    state.speakers.set(key, result.name || '');
    updateSpeakerEverywhere(key);
    applySpeakerNoteProjection(result.note_projection, result.note_updates);
    loadOutline();
    return result;
  }
  async function renameSpeakers(names) {
    const body = new URLSearchParams({ names: JSON.stringify(names), return_to: '/', t: '' });
    const suggested = Object.keys(names).filter(key => document.querySelector(`[data-suggestion-for="${CSS.escape(key)}"]`)?.dataset.suggestionName === names[key]);
    if (suggested.length) body.set('suggested', JSON.stringify(suggested));
    const response = await fetch(`/file/${encodeURIComponent(cfg.fileId)}/speakers`, { method: 'POST', headers: { accept: 'application/json' }, body, signal });
    let result = {};
    try { result = await response.json(); } catch (_error) { /* non-JSON */ }
    if (!response.ok) throw new Error(tr(result.error || 'Could not rename speaker'));
    for (const [key, name] of Object.entries(result.names || {})) {
      state.speakers.set(key, name || '');
      updateSpeakerEverywhere(key);
    }
    document.dispatchEvent(new CustomEvent('localplaud:speakers-renamed', { detail: { names: result.names || {} } }));
    applySpeakerNoteProjection(result.note_projection, result.note_updates);
    loadOutline();
    return result;
  }
  async function mergeSpeakers(source, target) {
    const body = new URLSearchParams({ source, target, base_revision: String(state.revision) });
    const response = await fetch(`/file/${encodeURIComponent(cfg.fileId)}/speakers/merge`, { method: 'POST', headers: { accept: 'application/json' }, body, signal });
    let result = {};
    try { result = await response.json(); } catch (_error) { /* non-JSON */ }
    if (!response.ok) throw new Error(tr(saveErrors[result.code] || result.error || 'Could not merge speakers'));
    if (result.changed) {
      setRevision(result.revision);
      $$(`.seg[data-spk="${CSS.escape(source)}"]`).forEach(seg => applySpeaker(seg, target));
      $$(`.legend .swatch[data-spk="${CSS.escape(source)}"]`).forEach(swatch => swatch.closest('.speaker-pill')?.remove());
      loadOutline();
    }
    return result;
  }
  // Legend popovers: rename in place instead of a full reload.
  on(document, 'submit', async event => {
    const form = event.target.closest?.('.speaker-editor');
    if (!form) return;
    event.preventDefault();
    const status = form.querySelector('[data-speaker-status]');
    const key = form.elements.key.value, name = form.elements.name.value.trim();
    if (status) status.textContent = tr('Saving…');
    try {
      await renameSpeaker(key, name);
      if (status) status.textContent = '';
      form.closest('details')?.removeAttribute('open');
      toast(tr('Speaker renamed'));
    } catch (error) { if (error.name !== 'AbortError' && status) status.textContent = error.message; }
  });
  on(document, 'click', async event => {
    const button = event.target.closest('[data-merge-speaker]');
    if (!button) return;
    const source = button.dataset.mergeSpeaker, select = button.closest('.ws-merge')?.querySelector('[data-merge-target]');
    if (!select?.value) return;
    if (!await confirmSpeakerMerge(source, select.value, button)) return;
    const status = button.closest('form')?.querySelector('[data-speaker-status]');
    button.disabled = true;
    try {
      const result = await mergeSpeakers(source, select.value);
      toast(`${tr('Speakers merged')} · ${result.segments || 0}`);
    } catch (error) { if (error.name !== 'AbortError' && status) status.textContent = error.message; button.disabled = false; }
  });

  function closeSheet({ restoreFocus = true } = {}) {
    if (!sheet || sheet.hidden) return;
    sheet.hidden = true;
    scrim?.remove(); scrim = null;
    document.body.classList.remove('ws-sheet-open');
    if (restoreFocus) sheetContext?.opener?.focus();
    sheetContext = null;
  }
  function openSheet(opener) {
    if (!sheet) return;
    const seg = opener.closest('.seg');
    const key = seg?.dataset.spk || null;
    sheetContext = { opener, seg, key, chosen: key };
    const input = $('#ws-speaker-name', sheet), choices = $('[data-sheet-choices]', sheet), status = $('[data-sheet-status]', sheet);
    input.value = key ? (state.speakers.get(key) || '') : '';
    input.placeholder = key ? speakerLabel(key) : tr('Speaker name');
    status.textContent = '';
    choices.replaceChildren(...[...state.speakers.keys()].map(other => {
      const chip = document.createElement('button');
      chip.type = 'button'; chip.className = 'ws-speaker-choice'; chip.dataset.key = other;
      chip.style.setProperty('--c', colorFor(other));
      chip.setAttribute('aria-pressed', String(other === key));
      chip.innerHTML = '<span class="dot"></span><span></span>';
      chip.lastChild.textContent = speakerLabel(other);
      return chip;
    }));
    const scope = sheet.querySelector('input[name="scope"][value="all"]');
    if (scope) scope.checked = true;
    const segmentScope = sheet.querySelector('input[name="scope"][value="segment"]');
    if (segmentScope) segmentScope.disabled = !seg?.querySelector('.segedit');
    sheet.hidden = false;
    document.body.classList.add('ws-sheet-open');
    scrim = document.createElement('div');
    scrim.className = 'ws-scrim';
    scrim.addEventListener('click', () => closeSheet());
    sheet.before(scrim);
    if (!phone.matches) {
      const rect = opener.getBoundingClientRect();
      const width = Math.min(340, window.innerWidth - 24);
      sheet.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`;
      const below = rect.bottom + 8, height = sheet.offsetHeight || 320;
      sheet.style.top = `${below + height > window.innerHeight - 12 ? Math.max(12, rect.top - height - 8) : below}px`;
    } else { sheet.style.left = ''; sheet.style.top = ''; }
    requestAnimationFrame(() => { input.focus(); input.select(); });
  }
  on(document, 'click', event => {
    const opener = event.target.closest('[data-speaker-menu]');
    if (!opener || opener.closest('.transcript-import')) return;
    event.preventDefault();
    openSheet(opener);
  });
  on(sheet, 'click', event => {
    if (event.target.closest('[data-sheet-close]')) { closeSheet(); return; }
    const choice = event.target.closest('.ws-speaker-choice');
    if (!choice || !sheetContext) return;
    sheetContext.chosen = choice.dataset.key;
    $('#ws-speaker-name', sheet).value = state.speakers.get(choice.dataset.key) || '';
    $('#ws-speaker-name', sheet).placeholder = speakerLabel(choice.dataset.key);
    $$('.ws-speaker-choice', sheet).forEach(item => item.setAttribute('aria-pressed', String(item === choice)));
  });
  on(sheet && $('#ws-speaker-name', sheet), 'input', () => {
    if (!sheetContext) return;
    const typed = $('#ws-speaker-name', sheet).value.trim();
    const match = [...state.speakers.keys()].find(key => speakerLabel(key) === typed);
    sheetContext.chosen = match || null;
    $$('.ws-speaker-choice', sheet).forEach(item => item.setAttribute('aria-pressed', String(item.dataset.key === match)));
  });
  on(sheet && $('[data-sheet-form]', sheet), 'submit', async event => {
    event.preventDefault();
    if (!sheetContext) return;
    const { seg, key } = sheetContext;
    const status = $('[data-sheet-status]', sheet), save = $('.ws-sheet-save', sheet);
    const name = $('#ws-speaker-name', sheet).value.trim();
    const scope = sheet.querySelector('input[name="scope"]:checked')?.value || 'all';
    const chosen = sheetContext.chosen;
    save.disabled = true;
    status.textContent = tr('Saving…');
    try {
      if (scope === 'segment' || !key) {
        if (!chosen) throw new Error(tr('Choose a speaker from this recording to reassign this paragraph.'));
        const form = seg?.querySelector('.segedit');
        if (!form) throw new Error(tr('This transcript view cannot be edited.'));
        if (chosen !== key) {
          await postSegment(form, { text: form.querySelector('textarea').defaultValue, speaker: chosen });
          applySpeaker(seg, chosen);
          toast(tr('Paragraph reassigned'));
        }
      } else if (chosen && chosen !== key) {
        if (!cfg.canEdit) throw new Error(tr('This transcript view cannot be edited.'));
        if (!await confirmSpeakerMerge(key, chosen, save)) { status.textContent = ''; save.disabled = false; return; }
        const result = await mergeSpeakers(key, chosen);
        toast(`${tr('Speakers merged')} · ${result.segments || 0}`);
      } else {
        await renameSpeaker(key, name === key ? '' : name);
        toast(tr('Speaker renamed'));
      }
      save.disabled = false;
      closeSheet();
    } catch (error) {
      if (error.name === 'AbortError') return;
      save.disabled = false;
      status.textContent = error.message || tr('Could not save');
    }
  });
  on(document, 'keydown', event => { if (event.key === 'Escape' && sheet && !sheet.hidden) { event.preventDefault(); closeSheet(); } });
  signal.addEventListener('abort', () => closeSheet({ restoreFocus: false }), { once: true });

  // -------------------------------------------------------------- mind map
  function parseMindMap(markdown) {
    // Decode Markdown punctuation escapes as text, never as HTML.
    const plainText = value => value.replace(/\\([\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e])/g, '$1');
    let rootLabel = tr('Mind map');
    const roots = [], headings = [], bullets = [];
    for (const line of String(markdown || '').split('\n')) {
      const heading = line.match(/^ {0,3}(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = heading[1].length;
        const text = plainText(heading[2].trim().replace(/\s+#+\s*$/, ''));
        if (!text) continue;
        bullets.length = 0;
        if (level === 1) { rootLabel = text; headings.length = 0; continue; }
        while (headings.length && headings.at(-1).level >= level) headings.pop();
        const node = { text, children: [] };
        (headings.length ? headings.at(-1).node.children : roots).push(node);
        headings.push({level, node});
        continue;
      }
      const bullet = line.match(/^(\s*)[-*+]\s+(.*)$/);
      if (!bullet || !bullet[2].trim()) continue;
      const depth = Math.min(Math.floor(bullet[1].replace(/\t/g, '  ').length / 2), bullets.length);
      const node = { text: plainText(bullet[2].trim()), children: [] };
      const parent = depth ? bullets[depth - 1].children : headings.length ? headings.at(-1).node.children : roots;
      parent.push(node);
      bullets.length = depth;
      bullets.push(node);
    }
    return { text: rootLabel, children: roots };
  }
  const mindmapSource = document.getElementById('mindmap-src');
  if (mindmapSource) {
    const render = (node, depth = 0) => {
      const wrap = document.createElement('div');
      wrap.className = `mm-node${depth === 0 ? ' mm-root' : ''}`;
      wrap.style.setProperty('--mm-branch', depth === 0 ? 'var(--fg)' : `var(${speakerVars[(node.branch ?? 0) % speakerVars.length]})`);
      const label = document.createElement('div');
      label.className = 'mm-label';
      label.textContent = node.text;
      wrap.append(label);
      if (node.children.length) {
        const toggle = document.createElement('button');
        toggle.type = 'button'; toggle.className = 'mm-toggle';
        toggle.setAttribute('aria-expanded', 'true');
        toggle.setAttribute('aria-label', `${tr('Collapse branch')}: ${node.text}`);
        toggle.textContent = String(node.children.length);
        label.append(toggle);
        const children = document.createElement('div');
        children.className = 'mm-children';
        node.children.forEach((child, i) => {
          child.branch = depth === 0 ? i : node.branch;
          const item = document.createElement('div');
          item.className = 'mm-child';
          item.append(render(child, depth + 1));
          children.append(item);
        });
        wrap.append(children);
      }
      return wrap;
    };
    const tree = document.getElementById('mindmap-tree');
    const viewport = document.getElementById('mindmap-viewport');
    const zoom = document.getElementById('mm-zoom');
    // Open with only the first level showing; later refreshes keep the reader's expansion state.
    let initialRender = true;
    state.refreshMindMap = markdown => {
      mindmapSource.textContent = markdown;
      const collapsed = $$('.mm-node', tree).map(node => node.classList.contains('collapsed'));
      tree.replaceChildren(render(parseMindMap(markdown)));
      $$('.mm-node', tree).forEach((node, index) => {
        const branch = !node.classList.contains('mm-root') && node.querySelector('.mm-children');
        if (branch && (initialRender || collapsed[index])) setCollapsed(node, true);
      });
      initialRender = false;
    };
    const setCollapsed = (node, collapsed) => {
      node.classList.toggle('collapsed', collapsed);
      const toggle = node.querySelector(':scope > .mm-label > .mm-toggle');
      if (toggle) {
        toggle.setAttribute('aria-expanded', String(!collapsed));
        toggle.setAttribute('aria-label', `${tr(collapsed ? 'Expand branch' : 'Collapse branch')}: ${node.querySelector(':scope > .mm-label').firstChild.textContent}`);
      }
    };
    state.refreshMindMap(mindmapSource.textContent);
    on(tree, 'click', event => {
      const toggle = event.target.closest('.mm-toggle');
      if (!toggle) return;
      const node = toggle.closest('.mm-node');
      setCollapsed(node, !node.classList.contains('collapsed'));
    });
    const toggleAll = document.getElementById('mm-toggle-all');
    on(toggleAll, 'click', () => {
      const collapse = toggleAll.getAttribute('aria-pressed') !== 'true';
      $$('.mm-node', tree).forEach(node => { if (!node.classList.contains('mm-root') && node.querySelector('.mm-children')) setCollapsed(node, collapse); });
      toggleAll.setAttribute('aria-pressed', String(collapse));
      toggleAll.textContent = tr(collapse ? 'Expand all' : 'Collapse all');
    });
    const setZoom = value => {
      const clamped = Math.max(Number(zoom.min), Math.min(Number(zoom.max), Number(value)));
      zoom.value = String(clamped);
      tree.style.setProperty('--mm-scale', clamped / 100);
    };
    const naturalSize = () => {
      const prior = zoom.value;
      setZoom(100);
      const size = { width: Math.max(1, tree.scrollWidth), height: Math.max(1, tree.scrollHeight) };
      setZoom(prior);
      return size;
    };
    const fit = () => {
      const natural = naturalSize();
      setZoom(Math.min(100, ((viewport.clientWidth - 24) / natural.width) * 100, ((viewport.clientHeight - 24) / natural.height) * 100));
      viewport.scrollTo({ left: 0, top: 0, behavior: scrollBehavior() });
    };
    on(zoom, 'input', () => setZoom(zoom.value));
    on(document.getElementById('mm-zoom-out'), 'click', () => setZoom(Number(zoom.value) - 10));
    on(document.getElementById('mm-zoom-in'), 'click', () => setZoom(Number(zoom.value) + 10));
    on(document.getElementById('mm-fit'), 'click', fit);
    on(viewport, 'keydown', event => {
      if (event.key === '+' || event.key === '=') { event.preventDefault(); setZoom(Number(zoom.value) + 10); }
      else if (event.key === '-') { event.preventDefault(); setZoom(Number(zoom.value) - 10); }
      else if (event.key === '0') { event.preventDefault(); fit(); }
    });
    // Ctrl/⌘ + wheel (and trackpad pinch, which browsers report the same way) zooms.
    on(viewport, 'wheel', event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setZoom(Number(zoom.value) * (event.deltaY < 0 ? 1.08 : 0.92));
    }, { passive: false });
    // One pointer pans; two pointers pinch-zoom (touch).
    const pointers = new Map();
    let pan = null, pinch = null;
    on(viewport, 'pointerdown', event => {
      if (event.target.closest('button,a,input')) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      viewport.setPointerCapture(event.pointerId);
      if (pointers.size === 1) pan = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), zoom: Number(zoom.value) };
        pan = null;
      }
      viewport.classList.add('dragging');
    });
    on(viewport, 'pointermove', event => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pinch && pointers.size >= 2) {
        const [a, b] = [...pointers.values()];
        setZoom(pinch.zoom * (Math.hypot(a.x - b.x, a.y - b.y) / Math.max(1, pinch.distance)));
      } else if (pan) {
        viewport.scrollLeft = pan.left - (event.clientX - pan.x);
        viewport.scrollTop = pan.top - (event.clientY - pan.y);
      }
    });
    const release = event => {
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!pointers.size) { pan = null; viewport.classList.remove('dragging'); }
    };
    on(viewport, 'pointerup', release);
    on(viewport, 'pointercancel', release);
    const panel = document.getElementById('recording-panel-mindmap');
    const fullscreen = document.getElementById('mm-fullscreen');
    const setFull = full => {
      panel.classList.toggle('ws-mm-full', full);
      document.body.classList.toggle('ws-mm-full-open', full);
      fullscreen?.setAttribute('aria-pressed', String(full));
      requestAnimationFrame(fit);
    };
    on(fullscreen, 'click', () => setFull(!panel.classList.contains('ws-mm-full')));
    on(document, 'keydown', event => { if (event.key === 'Escape' && panel.classList.contains('ws-mm-full')) { event.preventDefault(); setFull(false); fullscreen?.focus(); } });
    signal.addEventListener('abort', () => document.body.classList.remove('ws-mm-full-open'), { once: true });
    // Fit once the panel is first shown with real dimensions.
    let fitted = false;
    const fitWhenVisible = () => { if (!fitted && !panel.hidden && viewport.clientWidth > 0) { fitted = true; fit(); } };
    const visibility = new MutationObserver(fitWhenVisible);
    visibility.observe(panel, { attributes: true, attributeFilter: ['hidden'] });
    signal.addEventListener('abort', () => visibility.disconnect(), { once: true });
    fitWhenVisible();
  }

  // ------------------------------------------------- panels, sheets, menus
  on(document, 'click', event => {
    if (!event.target.closest('[data-open-ask-sheet]')) return;
    document.getElementById('open-ask-dock')?.click();
    requestAnimationFrame(() => document.getElementById('ask-q')?.focus());
  });
  const openFind = () => {
    document.querySelector('.tabs>button[data-panel="transcript"]:not(.on)')?.click();
    const details = document.getElementById('transcript-find')?.closest('details');
    if (!details) return;
    details.open = true;
    requestAnimationFrame(() => {
      document.getElementById('transcript-find')?.focus({ preventScroll: true });
      // The panel hangs under the transcript toolbar; when that toolbar sits low
      // in the viewport, scroll just enough to bring the whole panel into view.
      const panel = details.querySelector('.search-popover');
      const rect = panel?.getBoundingClientRect();
      if (!rect || (rect.bottom <= window.innerHeight - 12 && rect.top >= 0)) return;
      let scroller = details.parentElement;
      while (scroller && scroller !== document.body && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
      const target = !scroller || scroller === document.body ? window : scroller;
      const delta = rect.top < 0 ? rect.top - 12 : rect.bottom - window.innerHeight + 16;
      target.scrollBy({ top: delta });
    });
  };
  on(document, 'keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f' && transcriptVisible() && document.getElementById('transcript-find')) {
      event.preventDefault();
      openFind();
    }
  });

  // Keyboard shortcut reference.
  on(document.getElementById('player-shortcuts'), 'click', () => {
    toast([
      `${tr('Space')} / K · ${tr('Play')} / ${tr('Pause')}`,
      `← → · 5 s (${tr('Shift')} 30 s)`,
      `J / L · 15 s`,
      `[ ] · ${tr('Playback speed')}`,
      `Ctrl/⌘ F · ${tr('Find in transcript')}`,
    ].join('\n'));
  });

  // Shared design-system dialogs (CONVENTIONS: lp.openDialog/closeDialog on a
  // native <dialog class="dialog sheet-on-phone">), with a showModal fallback.
  const dialogModal = dialog => ({
    open(opener, focus) {
      if (window.lp?.openDialog) window.lp.openDialog(dialog, opener); else if (!dialog.open) dialog.showModal();
      if (focus) requestAnimationFrame(() => focus.focus());
    },
    close() { if (window.lp?.closeDialog) window.lp.closeDialog(dialog); else dialog.close(); },
  });
  function confirmSpeakerMerge(source, target, opener) {
    const dialog = document.getElementById('ws-merge-confirm');
    if (!dialog || dialog.open || signal.aborted) return Promise.resolve(false);
    $('[data-merge-message]', dialog).textContent = `${speakerLabel(source)} → ${speakerLabel(target)}`;
    dialog.returnValue = '';
    return new Promise(resolve => {
      const finish = () => {
        signal.removeEventListener('abort', cancel);
        dialog.removeEventListener('close', finish);
        resolve(!signal.aborted && dialog.returnValue === 'merge');
      };
      const cancel = () => { dialog.close(); finish(); };
      dialog.addEventListener('close', finish, { once: true });
      signal.addEventListener('abort', cancel, { once: true });
      dialogModal(dialog).open(opener);
    });
  }
  signal.addEventListener('abort', () => $$('dialog.ws-note-dialog[open], dialog.ws-name-speakers[open]').forEach(dialog => dialog.close()), { once: true });

  // Template chooser dialog for (re)generating notes.
  const noteBackdrop = document.getElementById('note-generate-backdrop');
  if (noteBackdrop) {
    const modal = dialogModal(noteBackdrop);
    const select = document.getElementById('note-template-select');
    const cards = $$('[data-template-card]', noteBackdrop);
    on(document, 'click', event => {
      const opener = event.target.closest('[data-open-note-dialog]');
      if (!opener || opener.disabled) return;
      const checked = noteBackdrop.querySelector('input[name="note-template-card"]:checked') || noteBackdrop.querySelector('input[name="note-template-card"]');
      modal.open(opener, checked);
    });
    on(document.getElementById('note-generate-cancel'), 'click', () => modal.close());
    on(noteBackdrop, 'change', event => {
      if (event.target.name !== 'note-template-card' || !select) return;
      select.value = event.target.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    // Auto vs custom generation, and the provider privacy boundary of the
    // selected AI profile (principle 7: egress is visible before generating).
    const customBlock = $('[data-gen-custom]', noteBackdrop);
    const profileSelect = document.getElementById('note-profile-select');
    const boundary = $('[data-gen-boundary]', noteBackdrop);
    const labels = $('template[data-privacy-labels]', noteBackdrop)?.content;
    const syncMode = () => {
      const auto = noteBackdrop.querySelector('input[name="note-generate-mode"]:checked')?.value === 'auto';
      if (customBlock) customBlock.hidden = auto;
      if (auto && select) {
        select.value = 'auto';
        if (profileSelect) { profileSelect.value = ''; profileSelect.dispatchEvent(new Event('change', { bubbles: true })); }
      } else if (select) {
        const checked = noteBackdrop.querySelector('input[name="note-template-card"]:checked');
        if (checked) select.value = checked.value;
      }
    };
    const syncBoundary = () => {
      const option = profileSelect?.selectedOptions[0];
      if (!option || !boundary || !labels) return;
      const privacy = option.dataset.privacy || 'unknown';
      const badge = $('.ws-privacy', boundary);
      badge.className = `ws-privacy ws-privacy-${privacy}`;
      badge.textContent = labels.querySelector(`[data-label="${privacy}"]`)?.textContent || '';
      $('[data-gen-model]', boundary).textContent = option.dataset.model || '';
      $('[data-gen-note]', boundary).textContent = labels.querySelector(`[data-note="${privacy}"]`)?.textContent || '';
    };
    on(noteBackdrop, 'change', event => { if (event.target.name === 'note-generate-mode') syncMode(); });
    on(profileSelect, 'change', syncBoundary);
    syncMode();
    syncBoundary();
    on(document.getElementById('note-template-filter'), 'input', event => {
      const query = event.target.value.trim().toLocaleLowerCase();
      let shown = 0;
      cards.forEach(card => { const match = !query || card.dataset.search.includes(query); card.hidden = !match; shown += match ? 1 : 0; });
      const empty = $('[data-template-empty]', noteBackdrop);
      if (empty) empty.hidden = shown > 0;
    });
  }

  // ------------------------------------------------------ clipboard, menus
  async function writeClipboard(text) {
    try { await navigator.clipboard.writeText(text); return; } catch (_error) { /* fall back */ }
    const area = document.createElement('textarea');
    area.value = text; area.setAttribute('readonly', ''); area.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    document.body.append(area); area.select();
    const copied = document.execCommand('copy');
    area.remove();
    if (!copied) throw new Error('clipboard unavailable');
  }
  on(document, 'click', async event => {
    const button = event.target.closest('[data-copy-segment]');
    if (!button) return;
    event.stopPropagation();
    const seg = button.closest('.seg');
    const text = seg.querySelector('.seg-text')?.textContent.trim() || '';
    const who = seg.querySelector('.who:not(.who-none) .who-name')?.textContent.trim();
    const time = seg.querySelector('.ts')?.textContent.trim();
    try { await writeClipboard(`${time ? `[${time}] ` : ''}${who ? `${who}: ` : ''}${text}`); toast(tr('Copied')); } catch (_error) { toast(tr('Copy failed'), { error: true }); }
  });

  on(document.getElementById('open-find'), 'click', () => openFind());

  // Name speakers dialog: one field per voice with sample clips.
  const namesBackdrop = document.getElementById('name-speakers-backdrop');
  if (namesBackdrop) {
    const modal = dialogModal(namesBackdrop);
    const form = document.getElementById('name-speakers-form');
    $$('.swatch[data-spk]', namesBackdrop).forEach(swatch => { swatch.style.background = colorFor(swatch.dataset.spk); });
    on(document.getElementById('menu-name-speakers'), 'click', event => {
      event.currentTarget.closest('details')?.removeAttribute('open');
      $$('input', form).forEach(input => {
        input.value = state.speakers.get(input.name) || '';
        input.placeholder = anonymousSpeakerLabel(input.name);
        input.setAttribute('aria-label', `${tr('Display name for')} ${speakerLabel(input.name)}`);
      });
      modal.open(event.currentTarget, $('input', form));
    });
    on(document.getElementById('name-speakers-cancel'), 'click', () => modal.close());
    on(form, 'submit', async event => {
      event.preventDefault();
      if (form.dataset.busy === 'true') return;
      const status = document.getElementById('name-speakers-status');
      const changed = $$('input', form).filter(input => input.value.trim() !== (state.speakers.get(input.name) || ''));
      if (!changed.length) { modal.close(); return; }
      status.textContent = tr('Saving…');
      form.dataset.busy = 'true';
      namesBackdrop.dataset.busy = 'true';
      const submit = form.querySelector('[type=submit]');
      submit.disabled = true;
      try {
        await renameSpeakers(Object.fromEntries(changed.map(input => [input.name, input.value.trim()])));
        status.textContent = '';
        delete namesBackdrop.dataset.busy;
        modal.close();
        toast(tr('Speaker names saved'));
      } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message || tr('Could not save'); }
      finally { delete form.dataset.busy; delete namesBackdrop.dataset.busy; submit.disabled = false; }
    });
  }

  // Custom speech settings (per-recording ASR language and speaker count).
  const speechForm = document.getElementById('speech-settings-form');
  if (speechForm) {
    const speechDialog = speechForm.closest('dialog');
    const status = document.getElementById('speech-settings-status');
    on(document.getElementById('menu-speech-settings'), 'click', event => event.currentTarget.closest('details')?.removeAttribute('open'));
    on(speechForm, 'submit', async event => {
      event.preventDefault();
      if (speechForm.dataset.busy === 'true') return;
      const data = new FormData(speechForm), mode = data.get('speaker_mode');
      const count = name => (data.get(name) ? Number(data.get(name)) : null);
      const body = { language: data.get('language') || null };
      if (mode === 'exact') body.num_speakers = count('num_speakers');
      if (mode === 'range') { body.min_speakers = count('min_speakers'); body.max_speakers = count('max_speakers'); }
      if (mode === 'range' && body.min_speakers && body.max_speakers && body.min_speakers > body.max_speakers) {
        status.textContent = tr('The minimum number of speakers cannot be more than the maximum.');
        speechForm.elements.min_speakers.focus();
        return;
      }
      const rebuild = event.submitter?.value === 'rebuild';
      speechForm.dataset.busy = 'true'; speechDialog.dataset.busy = 'true';
      status.textContent = tr('Saving…');
      try {
        const response = await fetch(`/api/files/${encodeURIComponent(speechForm.dataset.fileId)}/speech-settings`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : tr('Could not save'));
        if (rebuild) {
          const queued = await fetch(`/file/${encodeURIComponent(speechForm.dataset.fileId)}/reprocess?force=true`, { method: 'POST', signal });
          const message = document.getElementById('reprocess-msg');
          if (message) message.innerHTML = await queued.text();
        }
        status.textContent = '';
        const menuItem = document.getElementById('menu-speech-settings');
        const custom = Object.values(body).some(value => value !== null && value !== undefined);
        let chip = menuItem?.querySelector('.chip');
        if (menuItem && custom && !chip) { chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = tr('Custom'); menuItem.append(' ', chip); }
        if (!custom) chip?.remove();
        delete speechDialog.dataset.busy;
        if (window.lp?.closeDialog) window.lp.closeDialog(speechDialog); else speechDialog.close();
        toast(rebuild ? tr('Speech settings saved. Rebuilding this recording.') : tr('Speech settings saved. They apply on the next rebuild.'));
      } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message || tr('Could not save'); }
      finally { delete speechForm.dataset.busy; delete speechDialog.dataset.busy; }
    });
  }

  // Template chooser categories.
  on(document, 'click', event => {
    const chip = event.target.closest('[data-template-category]');
    if (!chip) return;
    const category = chip.dataset.templateCategory;
    $$('[data-template-category]').forEach(item => item.setAttribute('aria-pressed', String(item === chip)));
    $$('[data-template-card]').forEach(card => { card.hidden = Boolean(category) && card.dataset.category !== category && card.querySelector('input').value !== 'auto'; });
  });

  // Notes outline rail (Plaud Web section TOC): one dash per heading of the
  // visible note, the current section highlighted, hover/focus reveals the
  // titles, and the rail can be collapsed (remembered per browser).
  const notesPanel = document.getElementById('recording-panel-notes');
  if (notesPanel) {
    const storageKey = 'localplaud:note-outline';
    let collapsed = false;
    try { collapsed = localStorage.getItem(storageKey) === 'collapsed'; } catch (_error) { /* storage unavailable */ }
    const column = document.createElement('div');
    column.className = 'ws-toc-col';
    const nav = document.createElement('nav');
    nav.className = 'ws-toc';
    nav.setAttribute('aria-label', tr('Note outline'));
    column.append(nav);
    notesPanel.append(column);
    let headings = [];
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'ws-toc-toggle';
    toggle.innerHTML = '<svg class="i i-sm" aria-hidden="true" focusable="false"><use href="#i-list-collapse"></use></svg>';
    const list = document.createElement('ol');
    list.className = 'ws-toc-list';
    nav.append(toggle, list);
    const applyCollapsed = () => {
      nav.classList.toggle('collapsed', collapsed);
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', tr(collapsed ? 'Show note outline' : 'Hide note outline'));
      toggle.title = toggle.getAttribute('aria-label');
    };
    on(toggle, 'click', () => {
      collapsed = !collapsed;
      try { localStorage.setItem(storageKey, collapsed ? 'collapsed' : 'open'); } catch (_error) { /* storage unavailable */ }
      applyCollapsed();
    });
    const build = () => {
      const body = $('[data-note-panel]:not([hidden]) .md', notesPanel);
      headings = body ? $$('h1, h2, h3', body).filter(item => item.textContent.trim()) : [];
      column.hidden = headings.length < 2;
      list.replaceChildren(...headings.map((heading, index) => {
        const item = document.createElement('li');
        const link = document.createElement('button');
        link.type = 'button';
        link.dataset.level = heading.tagName.slice(1);
        link.dataset.index = String(index);
        link.innerHTML = '<span class="ws-toc-dash" aria-hidden="true"></span><span class="ws-toc-label"></span>';
        link.lastChild.textContent = heading.textContent.trim();
        item.append(link);
        return item;
      }));
      highlight();
    };
    const highlight = () => {
      if (!headings.length || notesPanel.hidden) return;
      let current = 0;
      headings.forEach((heading, index) => { if (heading.getBoundingClientRect().top < 140) current = index; });
      $$('button[data-index]', list).forEach(link => link.classList.toggle('on', Number(link.dataset.index) === current));
    };
    on(list, 'click', event => {
      const link = event.target.closest('button[data-index]');
      if (!link) return;
      const heading = headings[Number(link.dataset.index)];
      heading?.scrollIntoView({ block: 'start', behavior: scrollBehavior() });
      heading?.setAttribute('tabindex', '-1');
      heading?.focus({ preventScroll: true });
    });
    const scroller = document.querySelector('.recording-pane');
    let ticking = false;
    const onScroll = () => { if (ticking) return; ticking = true; requestAnimationFrame(() => { ticking = false; highlight(); }); };
    on(scroller, 'scroll', onScroll, { passive: true });
    on(window, 'scroll', onScroll, { passive: true });
    const watcher = new MutationObserver(build);
    [notesPanel, ...$$('[data-note-panel]', notesPanel)].forEach(target => watcher.observe(target, { attributes: true, attributeFilter: ['hidden'] }));
    signal.addEventListener('abort', () => watcher.disconnect(), { once: true });
    state.refreshNoteOutline = build;
    applyCollapsed();
    build();
  }

  // The reading title already names the recording. Keep a distinct note heading,
  // but avoid repeating the same title and consuming the phone's first screen.
  function hideRepeatedNoteTitle(prose) {
    const normalize = value => String(value || '').trim().replace(/\s+/g, ' ');
    const heading = prose.firstElementChild;
    if (heading?.tagName === 'H1' && normalize(heading.textContent) === normalize(cfg.title)) heading.hidden = true;
  }
  $$('[data-generated-note-prose], [data-workspace-note-body]').forEach(hideRepeatedNoteTitle);

  // ------------------------------------------------ chapter outline (Sources)
  // API chapters use milliseconds. Keep generation/recovery controls visible
  // when no artifact exists; only an available outline replaces the phone's
  // inline transcript preview.
  function chapterAt(seconds) {
    let found = null;
    for (const chapter of outline.chapters) { if (chapter.start <= seconds) found = chapter; else break; }
    return found;
  }
  function updateChrome(seconds) {
    const paused = !player || player.paused;
    $$('[data-m-time]').forEach(node => { node.textContent = paused && !seconds ? stamp(Number(cfg.durationSeconds || 0)) : stamp(seconds); });
    $$('[data-m-play]').forEach(button => {
      button.setAttribute('aria-label', tr(paused ? 'Play' : 'Pause'));
      setIcon(button.querySelector('.i, .nav-icon'), paused ? 'play' : 'pause');
    });
    const chapter = chapterAt(seconds);
    const title = chapter ? chapter.title : '';
    if (chapterNow.textContent !== title) chapterNow.textContent = title;
    $$('[data-chapter-title]').forEach(node => { if (node.textContent !== title) node.textContent = title; });
    $$('[data-outline-list] button[data-seek]').forEach(button => button.classList.toggle('on', chapter !== null && Number(button.dataset.seek) === chapter.start));
  }
  let outlineTimer;
  let outlinePayload;
  let outlineBusy = false;
  let outlineLoadSequence = 0;
  const outlineStatus = $('[data-outline-status]');
  const outlineGenerate = $('[data-outline-generate]');
  const outlineMethod = $('[data-outline-method]');
  const outlineRefresh = $('[data-outline-refresh]');
  function outlineProvider() {
    const node = $('[data-outline-provider]');
    if (!node) return;
    const model = outlinePayload?.generation?.selection;
    node.textContent = outlineMethod?.value === 'time_slices'
      ? tr('Time sections are generated locally without a model.')
      : [tr('Uses this recording’s configured provider and quota.'), model?.provider_type, model?.model, model?.execution_target, model?.data_egress].filter(Boolean).join(' · ');
  }
  const renderOutline = data => {
    if (!outlineSection) return;
    outlinePayload = data;
    const stale = Boolean(data.stale);
    outline.chapters = (data.chapters || [])
      .map(item => ({ start: Number(item.start_ms) / 1000, end: Number(item.end_ms) / 1000, title: String(item.title || '').trim() }))
      .filter(item => Number.isFinite(item.start) && item.start >= 0 && item.end > item.start && item.title)
      .sort((a, b) => a.start - b.start);
    const list = $('[data-outline-list]', outlineSection);
    list.replaceChildren(...outline.chapters.map(chapter => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.seek = String(chapter.start);
      button.innerHTML = '<span class="ws-outline-time"></span><span class="ws-outline-title"></span>';
      button.firstChild.textContent = stamp(chapter.start);
      button.lastChild.textContent = chapter.title;
      item.append(button);
      return item;
    }));
    const busy = ['pending', 'running'].includes(data.status);
    outlineBusy = busy;
    outlineSection.setAttribute('aria-busy', String(busy));
    outlineStatus.textContent = busy ? tr('Generating outline…') : data.status === 'failed'
      ? tr('Outline failed. Your transcript and notes are still available.')
      : stale ? tr('Outline is out of date after transcript edits.')
      : outline.chapters.length ? (data.method === 'time_slices' ? tr('Time sections · no AI') : tr('Topic chapters'))
      : tr('No outline yet. Choose a method to generate one.');
    const llmOption = outlineMethod.querySelector('[value="llm"]');
    if (llmOption) llmOption.disabled = !data.generation?.llm_available;
    if (llmOption?.disabled && outlineMethod.value === 'llm') outlineMethod.value = 'time_slices';
    outlineGenerate.disabled = busy;
    outlineMethod.disabled = busy;
    outlineGenerate.textContent = tr(outline.chapters.length ? 'Regenerate outline' : 'Generate outline');
    outlineRefresh.hidden = true;
    const provenance = data.provenance;
    const prov = $('[data-outline-provenance]', outlineSection);
    if (prov) prov.textContent = provenance ? [provenance.provider, provenance.model, `${tr('Revision')} ${provenance.revision}`].filter(Boolean).join(' · ') : '';
    transcriptPanel?.classList.toggle('ws-has-outline', outline.chapters.length > 0);
    $('[data-transcript-expand]', outlineSection).hidden = !outline.chapters.length;
    outlineProvider();
    if (player) updateChrome(player.currentTime);
    clearTimeout(outlineTimer);
    if (busy) outlineTimer = setTimeout(loadOutline, 3000);
  };
  state.renderOutline = renderOutline;
  async function loadOutline() {
    if (!outlineSection || signal.aborted) return;
    const sequence = ++outlineLoadSequence;
    clearTimeout(outlineTimer);
    try {
      const response = await fetch(`/api/files/${encodeURIComponent(cfg.fileId)}/outline`, { signal, headers: { accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error('outline unavailable');
      const data = await response.json();
      if (sequence !== outlineLoadSequence || signal.aborted) return;
      renderOutline(data);
    } catch (_error) {
      if (signal.aborted || sequence !== outlineLoadSequence) return;
      outlineStatus.textContent = tr('Could not load outline. Retry to check its status.');
      outlineRefresh.hidden = false;
      outlineGenerate.disabled = true;
    }
  }
  on(outlineRefresh, 'click', loadOutline);
  on(outlineMethod, 'change', outlineProvider);
  on(outlineGenerate, 'click', async () => {
    if (outlineBusy) return;
    outlineBusy = true;
    outlineGenerate.disabled = true;
    outlineMethod.disabled = true;
    outlineStatus.textContent = tr('Generating outline…');
    try {
      const response = await fetch(`/api/files/${encodeURIComponent(cfg.fileId)}/outline/regenerate`, {
        method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ method: outlineMethod.value }),
      });
      if (!response.ok) throw new Error('outline request failed');
      await loadOutline();
    } catch (_error) {
      if (signal.aborted) return;
      outlineStatus.textContent = tr('Could not start outline. Check its status before retrying.');
      outlineRefresh.hidden = false;
    }
  });
  signal.addEventListener('abort', () => clearTimeout(outlineTimer), { once: true });
  loadOutline();

  // Phone inline player + full-screen transcript (Plaud app "expand").
  on(document, 'click', event => {
    if (event.target.closest('[data-m-play]')) {
      if (!player) return;
      if (player.paused) player.play().catch(() => {}); else player.pause();
      return;
    }
    if (event.target.closest('[data-transcript-expand]')) { setTranscriptFull(true); return; }
    if (event.target.closest('[data-transcript-collapse]')) setTranscriptFull(false);
  });
  function setTranscriptFull(full) {
    if (!transcriptPanel) return;
    transcriptPanel.classList.toggle('ws-full', full);
    document.body.classList.toggle('ws-transcript-full', full);
    const card = $('[data-chapter-card]', transcriptPanel);
    if (card) card.hidden = !full;
    if (full) {
      following = true;
      $('[data-transcript-collapse]', transcriptPanel)?.focus({ preventScroll: true });
      const at = player ? player.currentTime : 0;
      ensureLoaded(at).then(() => setTimeout(() => {
        if (signal.aborted || !transcriptPanel.classList.contains('ws-full')) return;
        updateActive(at);
        if (activeSegment) scrollToSegment(activeSegment, true); else transcriptPanel.scrollTop = 0;
      }, 60));
    } else {
      $('[data-transcript-expand]', transcriptPanel)?.focus();
    }
  }
  on(document, 'keydown', event => {
    if (event.key === 'Escape' && transcriptPanel?.classList.contains('ws-full') && !document.body.classList.contains('dialog-open')) { event.preventDefault(); setTranscriptFull(false); }
  });
  signal.addEventListener('abort', () => document.body.classList.remove('ws-transcript-full'), { once: true });

  // Phone header switch (Sources | Notes) and the note picker ("Summary ⌄").
  const shell = document.querySelector('.recording-shell');
  const noteTabs = () => $$('.tabs > button[data-note-target], #recording-tab-mindmap');
  const syncSwitch = () => {
    const panel = shell?.dataset.activePanel || 'transcript';
    if (panel === 'ask') return;
    $$('[data-m-switch]').forEach(button => button.setAttribute('aria-pressed', String((button.dataset.mSwitch === 'transcript') === (panel === 'transcript'))));
    const active = panel === 'mindmap' ? document.getElementById('recording-tab-mindmap') : $('.tabs > button[data-note-target].on');
    const label = $('[data-note-picker-label]');
    if (label && active) label.textContent = active.textContent.trim();
    if (panel !== 'transcript') setTranscriptFull(false);
  };
  if (shell) {
    const switchWatcher = new MutationObserver(syncSwitch);
    switchWatcher.observe(shell, { attributes: true, attributeFilter: ['data-active-panel'] });
    signal.addEventListener('abort', () => switchWatcher.disconnect(), { once: true });
  }
  on(document, 'click', event => {
    const target = event.target.closest('[data-m-switch]');
    if (!target) return;
    if (target.dataset.mSwitch === 'transcript') document.getElementById('recording-tab-transcript')?.click();
    else ($('.tabs > button[data-note-target].on') || $('.tabs > button[data-note-target]') || document.getElementById('recording-tab-notes'))?.click();
  });
  const pickerButton = $('[data-note-picker]');
  const pickerMenu = document.getElementById('ws-note-picker-menu');
  const setPicker = open => {
    if (!pickerMenu) return;
    if (open) {
      const current = shell?.dataset.activePanel === 'mindmap' ? document.getElementById('recording-tab-mindmap') : $('.tabs > button[data-note-target].on');
      pickerMenu.replaceChildren(...noteTabs().map(tab => {
        const item = document.createElement('button');
        item.type = 'button';
        item.setAttribute('role', 'menuitemradio');
        item.setAttribute('aria-checked', String(tab === current));
        item.dataset.pickTab = tab.id || '';
        item.textContent = tab.textContent.trim();
        item.addEventListener('click', () => { tab.click(); setPicker(false); pickerButton.focus(); });
        return item;
      }));
    }
    pickerMenu.hidden = !open;
    pickerButton.setAttribute('aria-expanded', String(open));
    if (open) requestAnimationFrame(() => ($('[aria-checked="true"]', pickerMenu) || $('button', pickerMenu))?.focus());
  };
  on(pickerButton, 'click', event => { event.stopPropagation(); setPicker(pickerMenu.hidden); });
  on(document, 'click', event => { if (pickerMenu && !pickerMenu.hidden && !event.target.closest('.ws-note-picker')) setPicker(false); });
  on(pickerMenu, 'keydown', event => {
    const items = $$('button', pickerMenu), index = items.indexOf(document.activeElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus(); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setPicker(false); pickerButton.focus(); }
  });
  syncSwitch();

  // Phones: the global top bar is replaced by the workspace's own header.
  document.body.classList.add('ws-recording-open');
  signal.addEventListener('abort', () => document.body.classList.remove('ws-recording-open', 'ws-sheet-open'), { once: true });
})();
