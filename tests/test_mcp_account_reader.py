"""Run with the existing adapter's MCP v1 runtime (mcp<2)."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip('mcp.server.fastmcp')


@pytest.fixture
def adapter(monkeypatch):
    directory = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location('localplaud_ro', directory / 'localplaud_ro.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_tool_names_and_parameters_remain_available(adapter):
    tools = {t.name: t for t in asyncio.run(adapter.mcp.list_tools())}
    assert {'localplaud_diagnostics', 'localplaud_list_recordings',
            'localplaud_recording_page', 'localplaud_recording_usage'} <= tools.keys()
    assert set(tools['localplaud_recording_page'].inputSchema['properties']) == {'file_id', 'max_chars', 'offset'}
    assert tools['localplaud_recording_page'].inputSchema['required'] == ['file_id']


def test_recording_page_reads_content_export_and_all_continuations(adapter, monkeypatch):
    content = '# Notes\n測試內容。\n## Transcript\n' + '甲乙丙丁戊。\n' * 5000 + '最後一句'
    paths = []

    def request(path):
        paths.append(path)
        return content

    monkeypatch.setattr(adapter, '_request', request)
    offset = 0
    parts = []
    while True:
        result = adapter.localplaud_recording_page('recording-1', max_chars=1000, offset=offset)
        header, chunk = result.split('\n\n', 1)
        parts.append(chunk)
        offset += len(chunk)
        assert f'total_chars={len(content)}' in header
        if 'end_of_document=true' in header:
            break
        assert f'next_offset={offset};' in header
    assert ''.join(parts) == content
    assert set(paths) == {'/file/recording-1/export.md'}


def test_specific_content_tools_use_canonical_exports(adapter, monkeypatch):
    paths = []
    monkeypatch.setattr(adapter, '_request', lambda path: paths.append(path) or '實際內容')
    assert '實際內容' in adapter.localplaud_recording_transcript('one')
    assert '實際內容' in adapter.localplaud_recording_notes('one')
    assert paths == ['/file/one/export/transcript.txt', '/file/one/export/notes.md']
