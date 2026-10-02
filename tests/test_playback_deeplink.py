"""Exercise the real workspace deep-link helpers with cached and cold media."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE_JS = Path(__file__).parents[1] / "src/localplaud/api/static/js/workspace.js"


def _helpers() -> str:
    source = WORKSPACE_JS.read_text()
    start = source.index("// --- deep-link helpers")
    end = source.index("// --- end deep-link helpers")
    return source[start:end]


def _run(harness: str) -> dict:
    result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is required for DOM event regression"
)
@pytest.mark.parametrize("ready_state", [0, 1, 4])
def test_citation_timestamp_survives_cached_media_loading(ready_state):
    harness = f"""
const location = {{search: '?t=49.98'}};
const cleanupController = new AbortController();
const player = new EventTarget();
player.readyState = {ready_state};
player.currentTime = 0;
player.loads = 0;
player.load = () => {{
  player.loads++;
  player.readyState = 0;
  player.currentTime = 0;
  setImmediate(() => {{player.readyState = 1; player.dispatchEvent(new Event('loadedmetadata'));}});
}};
{_helpers()}
seekWhenReady(player, deepLinkSeconds(location.search), cleanupController.signal);
setImmediate(() => console.log(JSON.stringify({{time: player.currentTime, loads: player.loads}})));
"""
    result = _run(harness)
    assert result["time"] == 49.98
    assert result["loads"] == (1 if ready_state == 0 else 0)


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is required for DOM event regression"
)
def test_deep_link_rejects_non_finite_and_negative_offsets():
    harness = f"""
{_helpers()}
console.log(JSON.stringify(['?t=inf', '?t=-inf', '?t=Infinity', '?t=1e309', '?t=-3', '?t=abc', '', '?t=0', '?t=12.5']
  .map(search => deepLinkSeconds(search))));
"""
    assert _run(harness) == [None, None, None, None, None, None, None, 0, 12.5]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for player regression")
def test_return_to_playing_recording_does_not_rewind_to_saved_position():
    harness = f"""
{_helpers()}
const media = {{readyState: 4, currentTime: 325.5}};
const restore = playbackRestoreSeconds('', '280', true);
if (restore !== null) seekWhenReady(media, restore, new AbortController().signal);
console.log(JSON.stringify({{
  continuedTime: media.currentTime,
  freshReload: playbackRestoreSeconds('', '280', false),
  explicitCitation: playbackRestoreSeconds('?t=49.98', '280', true),
  explicitStart: playbackRestoreSeconds('?t=0', '280', true),
  noSavedPosition: playbackRestoreSeconds('', null, false),
}}));
"""
    assert _run(harness) == {
        "continuedTime": 325.5, "freshReload": 280, "explicitCitation": 49.98,
        "explicitStart": 0, "noSavedPosition": None,
    }
