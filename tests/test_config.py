"""Settings parsing: integer thresholds must be integers, and the error names the variable."""

import pytest

from app.config import _env_int


def test_env_int_rejects_non_integer_with_named_variable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_OFF_TOPIC", "three")
    with pytest.raises(ValueError, match=r"MAX_OFF_TOPIC must be an integer, got 'three'"):
        _env_int("MAX_OFF_TOPIC", 3)


def test_env_int_default_and_whitespace(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MAX_OFF_TOPIC", raising=False)
    assert _env_int("MAX_OFF_TOPIC", 3) == 3
    monkeypatch.setenv("MAX_OFF_TOPIC", " 5 ")
    assert _env_int("MAX_OFF_TOPIC", 3) == 5
