"""Tests must never require an ONC token or contact ONC/WoRMS."""
import pytest
import requests


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in the test suite")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
