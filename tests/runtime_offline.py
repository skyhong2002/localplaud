"""Opt-in guard: pytest -p tests.runtime_offline rejects real network calls.

In-process TestClient and mocked HTTP transports remain usable. Synthetic child
programs used by runtime tests only manipulate fixtures and processing claims.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_outbound_sockets(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def blocked(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("real network access disabled for isolated runtime tests")
        return connect(self, address)

    def blocked_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("real network access disabled for isolated runtime tests")
        return connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_ex)
