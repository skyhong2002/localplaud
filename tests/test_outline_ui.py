"""Execute workspace mutation/outline coordination in Node without a provider call."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def workspace_functions():
    source_root = Path(os.environ.get("LOCALPLAUD_TEST_SOURCE_ROOT", Path(__file__).parents[1]))
    source = (source_root / "src/localplaud/api/static/js/workspace.js").read_text()

    def extract(name):
        match = re.search(rf"  (?:async )?function {name}\(.*?\n  }}", source, re.DOTALL)
        assert match, f"Workspace function {name} is missing"
        return match.group(0)

    return extract


def run_node(functions, harness):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the executable workspace regression")
    script = "const assert = require('node:assert/strict');\n" + functions + "\n" + harness
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_speaker_merge_requires_confirmation_and_cancels_on_navigation(workspace_functions):
    run_node(workspace_functions("confirmSpeakerMerge"), r"""
const controller = new AbortController(), signal = controller.signal;
const dialog = new EventTarget();
dialog.open = false;
dialog.close = () => {dialog.open = false; dialog.dispatchEvent(new Event('close'));};
const document = {getElementById: () => dialog};
const label = {textContent: ''};
const $ = () => label;
const speakerLabel = value => value;
const dialogModal = () => ({open: () => {dialog.open = true;}});
(async () => {
  let pending = confirmSpeakerMerge('Alice', 'Bob', {});
  assert.equal(label.textContent, 'Alice → Bob');
  dialog.close(); // Escape/backdrop must not authorize a merge.
  assert.equal(await pending, false);
  pending = confirmSpeakerMerge('Alice', 'Bob', {});
  dialog.returnValue = 'merge'; dialog.close();
  assert.equal(await pending, true);
  pending = confirmSpeakerMerge('Alice', 'Bob', {});
  controller.abort();
  assert.equal(await pending, false);
  assert.equal(dialog.open, false);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")


def test_canonical_mutations_refresh_outline_only_after_success(workspace_functions):
    functions = "\n".join(workspace_functions(name) for name in (
        "postSegment", "renameSpeaker", "mergeSpeakers",
    ))
    run_node(functions, r"""
const signal = new AbortController().signal;
const cfg = {fileId: 'synthetic-recording'};
const state = {revision: 3, view: 'corrected', speakers: new Map([['s1', 'Original']])};
const player = {currentTime: 65.7};
const tr = value => value;
const saveErrors = {stale_revision: 'Reload before saving'};
const CSS = {escape: value => value};
const $$ = () => [];
const applySpeaker = () => {};
const updatedSpeakers = [];
const updateSpeakerEverywhere = key => updatedSpeakers.push(key);
const applySpeakerNoteProjection = () => {};
const revisions = [];
const setRevision = value => {state.revision = value; revisions.push(value);};
const refreshedAt = [];
const loadOutline = () => refreshedAt.push(state.revision);
class FormData extends Map { constructor(_form) { super(); } }
let reply;
const requests = [];
const fetch = async (url, options) => {requests.push({url, options}); return reply;};
const response = (value, ok=true) => ({ok, json: async () => value});
(async () => {
  reply = response({changed:true, revision:4});
  await postSegment({action:'/segment'}, {text:'Canonical edit'});
  assert.deepEqual(refreshedAt, [4]); // revision advances before checking stale state
  assert.equal(requests[0].options.body.get('t'), '65');
  assert.equal(requests[0].options.body.get('base_revision'), '3');
  reply = response({changed:false, revision:4});
  await postSegment({action:'/segment'}, {text:'Canonical edit'});
  reply = response({code:'stale_revision'}, false);
  await assert.rejects(postSegment({action:'/segment'}, {text:'rejected'}), /Reload/);
  assert.deepEqual(refreshedAt, [4]);
  assert.deepEqual(revisions, [4]);

  reply = response({name:'Renamed speaker'});
  await renameSpeaker('s1', 'Renamed speaker');
  assert.equal(state.speakers.get('s1'), 'Renamed speaker');
  assert.deepEqual(updatedSpeakers, ['s1']);
  assert.deepEqual(refreshedAt, [4, 4]);
  reply = response({error:'rename rejected'}, false);
  await assert.rejects(renameSpeaker('s1', 'Lost name'), /rename rejected/);
  assert.equal(state.speakers.get('s1'), 'Renamed speaker');
  assert.equal(refreshedAt.length, 2);

  reply = response({changed:true, revision:5});
  await mergeSpeakers('s1', 's2');
  assert.deepEqual(refreshedAt, [4, 4, 5]);
  reply = response({changed:false});
  await mergeSpeakers('s1', 's2');
  reply = response({error:'merge rejected'}, false);
  await assert.rejects(mergeSpeakers('s1', 's2'), /merge rejected/);
  assert.deepEqual(refreshedAt, [4, 4, 5]);
  assert.deepEqual(revisions, [4, 5]);
})().catch(error => {console.error(error); process.exitCode=1;});
""")


def test_outline_newer_stale_response_wins_over_slow_pre_edit_response(workspace_functions):
    run_node(workspace_functions("loadOutline"), r"""
const controller = new AbortController();
const signal = controller.signal;
const cfg = {fileId: 'synthetic-recording'};
const outlineSection = {};
let outlineLoadSequence = 0, outlineTimer = null;
const outlineStatus = {textContent:''};
const outlineRefresh = {hidden:true};
const outlineGenerate = {disabled:false};
const tr = value => value;
const rendered = [];
const renderOutline = data => rendered.push(data);
const requests = [];
const fetch = (url, options) => new Promise((resolve, reject) => {
  requests.push({url, options, resolve, reject});
});
const response = data => ({ok:true, json:async () => data});
(async () => {
  const beforeEdit = loadOutline();
  const afterEdit = loadOutline();
  assert.equal(requests[0].options.cache, 'no-store');
  requests[1].resolve(response({stale:true, revision:2}));
  await afterEdit;
  requests[0].resolve(response({stale:false, revision:1}));
  await beforeEdit;
  assert.deepEqual(rendered, [{stale:true, revision:2}]);

  const oldFailure = loadOutline();
  const newer = loadOutline();
  requests[3].resolve(response({stale:true, revision:3}));
  await newer;
  requests[2].reject(new Error('late network failure'));
  await oldFailure;
  assert.equal(outlineRefresh.hidden, true);
  assert.equal(outlineGenerate.disabled, false);
  assert.equal(outlineStatus.textContent, '');

  const currentFailure = loadOutline();
  requests[4].resolve({ok:false});
  await currentFailure;
  assert.equal(outlineRefresh.hidden, false);
  assert.equal(outlineGenerate.disabled, true);
  assert.match(outlineStatus.textContent, /Retry/);

  const pending = loadOutline();
  controller.abort();
  requests[5].resolve(response({stale:false, revision:4}));
  await pending;
  await loadOutline();
  assert.equal(requests.length, 6);
  assert.deepEqual(rendered, [{stale:true, revision:2}, {stale:true, revision:3}]);
})().catch(error => {console.error(error); process.exitCode=1;});
""")
