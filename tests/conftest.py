from __future__ import annotations

import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the test suite runs 100% offline with zero API keys or outbound sockets."""
    for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ELEVENLABS_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guarded_connect(self: socket.socket, address: Any) -> Any:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return real_connect(self, address)
        raise RuntimeError("Outbound network call blocked by offline test guard")

    def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return real_connect_ex(self, address)
        raise RuntimeError("Outbound network call blocked by offline test guard")

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
