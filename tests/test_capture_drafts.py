"""Opt-in phone-capture drafts: scoped, bounded, expiring, never uploaded on their own."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_accounts import client as accounts_client  # noqa: F401  (fixture)
from tests.test_accounts import create_user, login
from tests.test_phone_shell import _client

STATIC = Path(__file__).parents[1] / "src/localplaud/api/static"

_HARNESS = r"""
const assert = require('node:assert/strict');
const drafts = require(process.argv[1]);
const {createDraftStore, memoryBackend, POLICY, DraftError} = drafts;
const blob = (size, type = 'audio/webm') => new File([new Uint8Array(size)], 'Recording.webm', {type});
(async () => {
  let clock = 1_800_000_000_000;
  const backend = memoryBackend();
  const mine = createDraftStore({backend, scope: 'account:1:workspace:1', now: () => clock, estimate: null});
  const other = createDraftStore({backend, scope: 'account:2:workspace:2', now: () => clock, estimate: null});

  // A scope is mandatory: never fall back to an unscoped shared store.
  assert.throws(() => createDraftStore({backend, scope: ''}), DraftError);

  const kept = await mine.keep(blob(1024), {durationSeconds: 61.4});
  assert.equal(kept.durationSeconds, 61);
  assert.equal(kept.expiresAt, kept.createdAt + POLICY.ttlMs);
  assert.equal((await mine.list()).length, 1);
  // Another account/workspace on the same browser neither lists nor opens nor deletes it.
  assert.deepEqual(await other.list(), []);
  assert.equal(await other.open(kept.id), null);
  assert.equal(await other.discard(kept.id), false);
  assert.equal((await mine.list()).length, 1);

  const opened = await mine.open(kept.id);
  assert.equal(opened.file.size, 1024);
  assert.equal(opened.file.type, 'audio/webm');

  // Bounded: size and per-scope count are refused, not evicted.
  await assert.rejects(mine.keep(blob(POLICY.maxBytes + 1)), {code: 'too-large'});
  await assert.rejects(mine.keep(blob(0)), {code: 'invalid'});
  await mine.keep(blob(10)); await mine.keep(blob(10));
  await assert.rejects(mine.keep(blob(10)), {code: 'limit'});
  assert.equal((await other.keep(blob(10))).size, 10);

  // Storage-pressure estimate refuses before writing.
  const tight = createDraftStore({backend: memoryBackend(), scope: 's', now: () => clock,
    estimate: async () => ({usage: 95, quota: 100})});
  await assert.rejects(tight.keep(blob(1)), {code: 'quota'});
  const unknownQuota = createDraftStore({backend: memoryBackend(), scope: 's', now: () => clock,
    estimate: async () => { throw new Error('unsupported'); }});
  assert.ok(await unknownQuota.keep(blob(1)));

  // Stale, malformed, oversized and future-dated records are purged in every scope.
  backend.rows.set('bad', {id: 'bad', version: 1, scope: 'account:1:workspace:1'});
  backend.rows.set('huge', {id: 'huge', version: 1, scope: 'x', createdAt: clock, durationSeconds: 1,
    blob: blob(POLICY.maxBytes + 1)});
  backend.rows.set('future', {id: 'future', version: 1, scope: 'x', createdAt: clock + 2 * POLICY.futureSkewMs,
    durationSeconds: 1, blob: blob(1)});
  backend.rows.set('old-version', {id: 'old-version', version: 0, scope: 'x', createdAt: clock, durationSeconds: 1, blob: blob(1)});
  await mine.list();
  for (const id of ['bad', 'huge', 'future', 'old-version']) assert.ok(!backend.rows.has(id), id);

  clock += POLICY.ttlMs;
  assert.deepEqual(await mine.list(), []);
  assert.deepEqual(await other.list(), []);
  assert.equal(backend.rows.size, 0);
  assert.equal(await mine.open(kept.id), null);

  // Explicit discard removes only this scope's draft.
  const again = await mine.keep(blob(5));
  assert.equal(await mine.discard(again.id), true);
  assert.deepEqual(await mine.list(), []);

  // Unavailable IndexedDB surfaces as a typed, recoverable error.
  const missing = createDraftStore({backend: drafts.idbBackend(null), scope: 's', estimate: null});
  await assert.rejects(missing.list(), {code: 'unavailable'});
  const throwing = createDraftStore({backend: drafts.idbBackend({open() { throw new Error('SecurityError'); }}), scope: 's', estimate: null});
  await assert.rejects(throwing.keep(blob(1)), {code: 'unavailable'});
})().catch(error => { console.error(error); process.exit(1); });
"""


def test_draft_store_is_scoped_bounded_and_expiring():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the draft store")
    result = subprocess.run(
        [node, "-e", _HARNESS, str(STATIC / "js/capture-drafts.js")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _sheet(html: str) -> str:
    match = re.search(r'<dialog class="sheet" id="add-sheet"[^>]*>', html)
    assert match, "add sheet missing"
    return match.group(0)


def test_single_user_shell_offers_opt_in_drafts_with_disclosure(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    page = client.get("/home")
    assert page.status_code == 200
    assert 'data-capture-draft-scope="single:workspace:1"' in _sheet(page.text)
    assert "js/capture-drafts.js" in page.text
    assert "data-capture-keep hidden" in page.text
    # Retention, origin, device and non-recovery limits are disclosed next to the opt-in.
    assert "expires after 7 days" in page.text
    assert "the next time localplaud checks browser storage" in page.text
    assert "Anyone with access to this browser profile" in page.text
    assert "is not uploaded until you tap Save" in page.text
    assert "interrupted before you tap Stop cannot be recovered" in page.text


def test_app_never_uploads_a_draft_without_save():
    source = (STATIC / "js/app.js").read_text()
    start = source.index("phone \"+\" sheet")
    region = source[start:source.index("/* ------------------------------------------------------- service worker */")]
    # The only upload path is uploadAudio, invoked from Save or the file picker.
    assert region.count("uploadAudio(") == 3  # definition + save + file picker
    assert "if (recordingFile && !save.disabled) uploadAudio(recordingFile)" in region
    # Cleanup happens only after the server confirmed the saved recording.
    confirmed = region.index("xhr.status >= 200 && xhr.status < 300 && data.id")
    assert region.index("drafts.discard(savedDraftId)") > confirmed
    assert "confirmDeleteDraft()" in region


def test_accounts_scope_drafts_to_the_signed_in_account_workspace(accounts_client):  # noqa: F811
    user_id = create_user("member")
    login(accounts_client, "member")
    page = accounts_client.get("/home")
    assert page.status_code == 200
    sheet = _sheet(page.text)
    scope = re.search(r'data-capture-draft-scope="account:(\d+):workspace:(\d+)"', sheet)
    assert scope and int(scope.group(1)) == user_id


def test_other_account_gets_a_different_scope(accounts_client):  # noqa: F811
    create_user("sky", role="owner")
    create_user("member")
    scopes = set()
    for name in ("sky", "member"):
        accounts_client.cookies.clear()
        login(accounts_client, name)
        scopes.add(re.search(r'data-capture-draft-scope="([^"]+)"', accounts_client.get("/home").text).group(1))
    assert len(scopes) == 2
