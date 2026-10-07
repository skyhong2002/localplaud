"""Account-password text reader: real HTTP cookies, expiry and content retrieval."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/localplaud_read.py'
spec = importlib.util.spec_from_file_location('account_reader', SCRIPT)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


@pytest.fixture
def site(tmp_path):
    state = {'logins': 0, 'session': None, 'deny': False, 'bad_password': False, 'redirect': False}
    transcript = '00:01 Speaker 1：這是完整逐字稿。\n00:02 Speaker 2：包含最後一句。'
    notes = '# 測試筆記\n\n- 保留所有內容。'

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, content_type='text/plain; charset=utf-8'):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            assert self.path == '/login'
            assert self.headers['Origin'] == base
            assert parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()) == {
                'identifier': ['sky'], 'password': ['synthetic-test-password'], 'next': ['/account'],
            }
            state['logins'] += 1
            if state['bad_password']:
                return self.reply(200, 'Login failed', 'text/html')
            state['session'] = f"session-{state['logins']}"
            self.send_response(303)
            self.send_header('Set-Cookie', f"localplaud_session={state['session']}; Path=/; HttpOnly; Max-Age=600")
            self.send_header('Location', '/account')
            self.end_headers()

        def do_GET(self):
            if self.path == '/account':
                return self.reply(200, '<h1>Account</h1>', 'text/html')
            if self.headers.get('Cookie') != f"localplaud_session={state['session']}":
                return self.reply(401, 'Unauthorized')
            if state['deny']:
                return self.reply(403, 'Disabled')
            if state['redirect']:
                self.send_response(302)
                self.send_header('Location', 'https://accounts.google.com/')
                self.end_headers()
                return
            if self.path == '/api/files':
                return self.reply(200, json.dumps({'files': [{'id': 'test-recording'}]}), 'application/json')
            if self.path == '/file/test-recording/export/transcript.txt':
                return self.reply(200, transcript)
            if self.path == '/file/test-recording/export/notes.md':
                return self.reply(200, notes, 'text/markdown')
            self.reply(404, 'Not found')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    base = f'http://127.0.0.1:{server.server_port}'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    credential = tmp_path / 'account.json'
    credential.write_text(json.dumps({'base_url': base, 'username': 'sky', 'password': 'synthetic-test-password'}))
    credential.chmod(0o600)
    yield credential, state, transcript, notes
    server.shutdown()
    server.server_close()


def test_password_login_reads_full_text_and_reuses_private_session(site):
    credential, state, transcript, notes = site
    client = reader.Reader(credential)
    assert client.run('transcript', 'test-recording') == transcript
    assert reader.Reader(credential).run('notes', 'test-recording') == notes
    assert state['logins'] == 1
    assert credential.with_suffix('.cookies').stat().st_mode & 0o777 == 0o600
    result = json.loads(client.run('check'))
    assert result == {'ok': True, 'recordings': 1}


def test_revoked_session_logs_in_once_and_persists_replacement(site):
    credential, state, transcript, _ = site
    reader.Reader(credential).run('check')
    before = credential.with_suffix('.cookies').read_bytes()
    state['session'] = 'revoked'
    assert reader.Reader(credential).run('transcript', 'test-recording') == transcript
    assert state['logins'] == 2
    assert credential.with_suffix('.cookies').read_bytes() != before
    reader.Reader(credential).run('check')
    assert state['logins'] == 2


def test_wrong_password_is_not_reported_as_success(site):
    credential, state, _, _ = site
    state['bad_password'] = True
    with pytest.raises(reader.ReadError, match='login failed'):
        reader.Reader(credential).run('check')
    assert state['logins'] == 1
    assert not credential.with_suffix('.cookies').exists()


def test_missing_permission_does_not_loop_logins(site):
    credential, state, _, _ = site
    reader.Reader(credential).run('check')
    state['deny'] = True
    with pytest.raises(reader.ReadError, match='approval'):
        reader.Reader(credential).run('check')
    assert state['logins'] == 1


def test_external_redirect_is_rejected(site):
    credential, state, _, _ = site
    reader.Reader(credential).run('check')
    state['redirect'] = True
    with pytest.raises(reader.ReadError, match='outside'):
        reader.Reader(credential).run('transcript', 'test-recording')


@pytest.mark.parametrize('file_id', ['../login', 'id?token=x', 'https://example.test/'])
def test_read_rejects_non_recording_paths(site, file_id):
    credential, state, _, _ = site
    with pytest.raises(reader.ReadError, match='valid recording ID'):
        reader.Reader(credential).run('transcript', file_id)
    assert state['logins'] == 0
