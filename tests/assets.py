"""Read a rendered page together with the shared CSS/JS it links.

Shell CSS and JavaScript moved out of inline <style>/<script> blocks into
``/static/css/*.css`` and ``/static/js/app.js``. Contract tests that assert on
those rules or scripts read the page plus the same-origin assets it references,
exactly as a browser would load them.
"""

from __future__ import annotations

import re

_ASSET = re.compile(r'(?:href|src)="(/static/(?:css|js)/[^"?]+\.(?:css|js))(?:\?[^"]*)?"')


def with_assets(client, response) -> str:
    text = response.text if hasattr(response, "text") else str(response)
    parts = [text]
    for path in dict.fromkeys(_ASSET.findall(text)):
        asset = client.get(path)
        if asset.status_code == 200:
            parts.append(asset.text)
    return "\n".join(parts)
