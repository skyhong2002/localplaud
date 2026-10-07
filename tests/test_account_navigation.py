"""Exercise the actual navigation predicate for standalone account documents."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_account_links_use_document_navigation():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for navigation regression')
    source = (Path(__file__).parents[1] / 'src/localplaud/api/static/js/app.js').read_text()
    predicate = re.search(r'    const isPartialNavigation=link=>\{.*?\n    \};', source, re.S)
    assert predicate
    result = subprocess.run([node, '-e', '''
const assert = require('node:assert/strict');
const location = new URL('https://example.test/home');
''' + predicate.group() + '''
function link(path) {
  return {href:new URL(path, location).href, dataset:{}, target:'',
    hasAttribute:()=>false, getAttribute:()=>path};
}
for (const path of ['/account', '/admin/users?notice=updated', '/login', '/register',
                    '/auth/google', '/auth/google/callback', '/logout']) {
  assert.equal(isPartialNavigation(link(path)), false, path);
}
for (const path of ['/home', '/settings', '/file/example', '/search?q=test']) {
  assert.equal(isPartialNavigation(link(path)), true, path);
}
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
