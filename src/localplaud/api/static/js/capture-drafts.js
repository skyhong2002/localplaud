/* localplaud phone-capture drafts (owner: capture).
 *
 * Keeps a *stopped*, not-yet-saved microphone recording in this browser only
 * after the user explicitly chooses "Keep draft on this device". Nothing here
 * uploads: a kept draft reaches the server only when the user taps Save.
 *
 * Boundaries (also disclosed in the UI):
 * - Only stopped recordings are kept. Audio from a recording that was still in
 *   progress when the page closed, crashed, or went to the background is not
 *   recoverable and this module never claims otherwise.
 * - Drafts live in this origin's IndexedDB on this device/browser profile, are
 *   listed and restored only for the account + workspace scope that kept them,
 *   and expire after POLICY.ttlMs. Browser storage is not encrypted by
 *   localplaud and the browser may evict it under storage pressure.
 * - Each draft is bounded (POLICY.maxBytes) and each scope holds at most
 *   POLICY.maxPerScope drafts; the store refuses rather than evicting a draft.
 *
 * Exposes window.localplaudCaptureDrafts (and module.exports for tests):
 *   createDraftStore({backend, scope, now?, estimate?})
 *   idbBackend(indexedDBFactory?) / memoryBackend()
 *   DraftError (error.code: unavailable | too-large | quota | limit | invalid)
 */
(root => {
  'use strict';
  const POLICY = Object.freeze({
    version: 1,
    ttlMs: 7 * 24 * 60 * 60 * 1000,
    // The recorder stops itself at 128 MiB of chunks; allow container overhead.
    maxBytes: 130 * 1024 * 1024,
    maxPerScope: 3,
    // A draft dated this far in the future means a broken clock or record.
    futureSkewMs: 24 * 60 * 60 * 1000,
  });
  const DB_NAME = 'localplaud-capture-drafts', STORE = 'drafts';

  class DraftError extends Error {
    constructor(code, message) { super(message || code); this.name = 'DraftError'; this.code = code; }
  }

  const isBlob = value => typeof Blob !== 'undefined' && value instanceof Blob;
  const finite = value => typeof value === 'number' && Number.isFinite(value);

  /** Why a stored record must be removed, or '' when it is a usable draft. */
  function staleReason(record, now) {
    if (!record || typeof record !== 'object' || typeof record.id !== 'string') return 'invalid';
    if (record.version !== POLICY.version || typeof record.scope !== 'string' || !record.scope) return 'invalid';
    if (!isBlob(record.blob) || !finite(record.createdAt) || !finite(record.durationSeconds)) return 'invalid';
    if (record.blob.size <= 0 || record.blob.size > POLICY.maxBytes) return 'too-large';
    if (record.createdAt > now + POLICY.futureSkewMs || now - record.createdAt >= POLICY.ttlMs) return 'expired';
    return '';
  }

  function memoryBackend() {
    const rows = new Map();
    return {
      async all() { return [...rows.values()]; },
      async put(record) { rows.set(record.id, record); },
      async delete(id) { rows.delete(id); },
      rows,
    };
  }

  function idbBackend(factory) {
    let opening = null;
    const open = () => {
      if (opening) return opening;
      opening = new Promise((resolve, reject) => {
        let request;
        try {
          const idb = factory === undefined ? root.indexedDB : factory;
          if (!idb) throw new DraftError('unavailable');
          request = idb.open(DB_NAME, 1);
        } catch (error) {
          reject(error instanceof DraftError ? error : new DraftError('unavailable', error?.message)); return;
        }
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE, {keyPath: 'id'});
        };
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => { db.close(); opening = null; };
          resolve(db);
        };
        request.onerror = () => reject(new DraftError('unavailable', request.error?.message));
        request.onblocked = () => reject(new DraftError('unavailable', 'blocked'));
      });
      opening.catch(() => { opening = null; });
      return opening;
    };
    const run = async (mode, work) => {
      const db = await open();
      return new Promise((resolve, reject) => {
        let tx, result;
        try { tx = db.transaction(STORE, mode); } catch (error) { reject(new DraftError('unavailable', error?.message)); return; }
        const fail = () => {
          const error = tx.error;
          reject(new DraftError(error?.name === 'QuotaExceededError' ? 'quota' : 'unavailable', error?.message));
        };
        tx.oncomplete = () => resolve(result);
        tx.onerror = fail; tx.onabort = fail;
        try {
          const request = work(tx.objectStore(STORE));
          if (request) request.onsuccess = () => { result = request.result; };
        } catch (error) {
          reject(new DraftError(error?.name === 'QuotaExceededError' ? 'quota' : 'unavailable', error?.message));
        }
      });
    };
    return {
      all: () => run('readonly', store => store.getAll()).then(rows => rows || []),
      put: record => run('readwrite', store => store.put(record)),
      delete: id => run('readwrite', store => store.delete(id)),
    };
  }

  const newId = () => {
    try { if (root.crypto?.randomUUID) return root.crypto.randomUUID(); } catch (_error) { /* insecure context */ }
    return `d-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  };

  function createDraftStore({backend, scope, now = () => Date.now(), estimate} = {}) {
    if (typeof scope !== 'string' || !scope) throw new DraftError('invalid', 'A draft scope is required');
    const storageEstimate = estimate !== undefined ? estimate : () => root.navigator?.storage?.estimate?.();

    /** Remove expired, malformed and oversized drafts in every scope; return this scope's drafts. */
    async function purge() {
      const at = now();
      const rows = await backend.all();
      const keep = [];
      for (const row of rows) {
        if (staleReason(row, at)) {
          try { await backend.delete(row?.id); } catch (_error) { /* retried on the next purge */ }
        } else if (row.scope === scope) keep.push(row);
      }
      return keep.sort((a, b) => b.createdAt - a.createdAt);
    }

    const describe = row => ({
      id: row.id, name: row.name, mimeType: row.mimeType, size: row.blob.size,
      durationSeconds: row.durationSeconds, createdAt: row.createdAt, expiresAt: row.createdAt + POLICY.ttlMs,
    });

    return {
      scope,
      policy: POLICY,
      async list() { return (await purge()).map(describe); },
      async keep(file, {durationSeconds = 0} = {}) {
        if (!isBlob(file) || !file.size) throw new DraftError('invalid');
        if (file.size > POLICY.maxBytes) throw new DraftError('too-large');
        const current = await purge();
        if (current.length >= POLICY.maxPerScope) throw new DraftError('limit');
        try {
          const quota = await storageEstimate?.();
          if (quota && finite(quota.quota) && finite(quota.usage) && quota.usage + file.size > quota.quota * 0.9) {
            throw new DraftError('quota');
          }
        } catch (error) { if (error instanceof DraftError) throw error; /* estimate unsupported */ }
        const createdAt = now();
        const record = {
          id: newId(), version: POLICY.version, scope, createdAt,
          durationSeconds: finite(durationSeconds) ? Math.max(0, Math.round(durationSeconds)) : 0,
          mimeType: file.type || 'application/octet-stream', name: String(file.name || 'Recording'), blob: file,
        };
        await backend.put(record);
        return describe(record);
      },
      /** The draft as a File, only when it belongs to this scope and is still valid. */
      async open(id) {
        const row = (await purge()).find(item => item.id === id);
        if (!row) return null;
        const file = typeof File !== 'undefined' ? new File([row.blob], row.name, {type: row.mimeType}) : row.blob;
        return {file, draft: describe(row)};
      },
      async discard(id) {
        const row = (await backend.all()).find(item => item?.id === id);
        // Never delete another scope's draft through this store.
        if (row && row.scope !== scope) return false;
        await backend.delete(id);
        return true;
      },
    };
  }

  const api = {POLICY, DraftError, staleReason, createDraftStore, idbBackend, memoryBackend};
  root.localplaudCaptureDrafts = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
