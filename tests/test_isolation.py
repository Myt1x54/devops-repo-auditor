from __future__ import annotations

import os
import socket

import pytest


def test_network_is_blocked_in_tests():
    with pytest.raises(RuntimeError, match="network connection"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)  # TEST-NET-1, never routable


def test_real_api_keys_are_not_visible_to_tests():
    assert not os.environ.get("GEMINI_API_KEY")
    assert not os.environ.get("ANTHROPIC_API_KEY")
