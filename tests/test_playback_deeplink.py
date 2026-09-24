"""Exercise the real detail-page deep-link script with cached and cold media."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is required for DOM event regression"
)
@pytest.mark.parametrize("ready_state", [0, 1, 4])
def test_citation_timestamp_survives_cached_media_loading(ready_state):
    template = (Path(__file__).parents[1] / "src/localplaud/api/templates/detail.html").read_text()
    script = template.split("// deep-link: /file/{id}?t={seconds}", 1)[1]
    script = script[script.index("(function()") : script.index("// Reloading mid-listen")]
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
{script}
setImmediate(() => console.log(JSON.stringify({{time: player.currentTime, loads: player.loads}})));
"""
    result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
    result = json.loads(result.stdout)
    assert result["time"] == 49.98
    assert result["loads"] == (1 if ready_state == 0 else 0)
