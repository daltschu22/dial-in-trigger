import pytest

from dial_in_trigger.config import WebConfig, WorkerConfig


@pytest.fixture
def env(monkeypatch, tmp_path):
    values = {"TWILIO_ACCOUNT_SID": "AC" + "a" * 32, "TWILIO_AUTH_TOKEN": "test-auth-token",
              "TWILIO_PHONE_NUMBER": "+12025550100", "PUBLIC_BASE_URL": "https://dial.example.com",
              "TRIGGER_CODE": "86753091", "DIAL_IN_STATE_DIR": str(tmp_path), "ALLOWED_CALLERS": "",
              "VOICE_PROMPT": "", "GREETING_FILE": "", "GREETING_INTERRUPTIBLE": "true",
              "DIGIT_TIMEOUT_SECONDS": "10"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_valid_config_hides_secrets(env):
    config = WebConfig.from_env()
    assert config.prompt == ""
    assert config.code not in repr(config)
    assert config.auth_token not in repr(config)


@pytest.mark.parametrize("name,value", [
    ("TWILIO_AUTH_TOKEN", ""), ("TWILIO_ACCOUNT_SID", "not-an-account"),
    ("TRIGGER_CODE", "1234"), ("TRIGGER_CODE", "12345678#"),
    ("PUBLIC_BASE_URL", "http://dial.example.com"),
    ("PUBLIC_BASE_URL", "https://user:pass@dial.example.com"),
    ("PUBLIC_BASE_URL", "https://dial.example.com/prefix"),
    ("PUBLIC_BASE_URL", "https://dial.example.com?query=1"),
    ("DIAL_IN_STATE_DIR", "relative"), ("TWILIO_PHONE_NUMBER", "2025550100"),
    ("ALLOWED_CALLERS", "anyone"), ("DIGIT_TIMEOUT_SECONDS", "0"),
    ("DIGIT_TIMEOUT_SECONDS", "61"), ("GREETING_INTERRUPTIBLE", "maybe"),
    ("GREETING_FILE", "/nonexistent/greeting.wav"),
])
def test_invalid_web_config_fails_closed(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        WebConfig.from_env()


def test_worker_requires_an_executable(env, monkeypatch, tmp_path):
    path = tmp_path / "script.sh"
    path.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("TRIGGER_SCRIPT", str(path))
    with pytest.raises(ValueError):
        WorkerConfig.from_env()
    path.chmod(0o700)
    assert WorkerConfig.from_env().script == path
