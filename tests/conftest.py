"""CI tests must never reach flight providers, SMTP servers, or other networks."""
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("External network access is forbidden in application tests")
        return original_connect(connection, address)

    def connect_ex(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("External network access is forbidden in application tests")
        return original_connect_ex(connection, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
