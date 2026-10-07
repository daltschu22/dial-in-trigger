import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pytest
from twilio.request_validator import RequestValidator

from dial_in_trigger.config import WebConfig, WorkerConfig
from dial_in_trigger.store import Store
from dial_in_trigger.web import create_app
from dial_in_trigger.worker import run_next, worker_lock


@pytest.fixture
def config(tmp_path):
    return WebConfig("https://dial.example.com", "AC" + "a" * 32, "test-auth-token",
                     "+12025550100", "86753091", tmp_path)


def fields(config, index=1, **extra):
    return {"AccountSid": config.account_sid, "CallSid": f"CA{index:032x}",
            "From": "+16175550123", "To": config.phone_number, **extra}


def post(app, config, path, data, **headers):
    signature = RequestValidator(config.auth_token).compute_signature(config.public_url + path, data)
    with app.test_client() as client:
        return client.post(path, data=data, headers={"X-Twilio-Signature": signature, **headers})


def start(app, config, index=1, **extra):
    data = fields(config, index, **extra)
    response = post(app, config, "/voice", data)
    assert response.status_code == 200
    gather = ElementTree.fromstring(response.data).find("Gather")
    assert gather is not None
    action = urlsplit(gather.attrib["action"])
    return action.path + "?" + action.query, data


def test_authenticated_call_queues_and_runs_actual_script_once(config):
    app = create_app(config)
    action, data = start(app, config)
    data["Digits"] = config.code
    for _ in range(3):
        assert b"Command accepted" in post(app, config, action, data).data
    store = Store(config.state)
    assert len(store.jobs()) == 1
    script = Path(__file__).resolve().parents[1] / "scripts/demo.sh"
    worker = WorkerConfig(config.state, script)
    assert run_next(worker, store, threading.Event())
    assert not run_next(worker, store, threading.Event())
    assert store.jobs()[0]["status"] == "succeeded"
    assert (config.state / "demo-runs.log").read_text().count(data["CallSid"]) == 1
    # Persistence across application restarts also prevents replay execution.
    assert b"Command accepted" in post(create_app(config), config, action, data).data
    assert not run_next(worker, store, threading.Event())
    assert all(config.code.encode() not in p.read_bytes() for p in config.state.glob("*.sqlite3*"))


@pytest.mark.parametrize("changes", [{"AccountSid": "AC" + "b" * 32}, {"To": "+16175559999"},
                                     {"CallSid": "not-a-call"}, {"From": ""}])
def test_wrong_account_number_or_call_rejected(config, changes):
    app = create_app(config)
    assert post(app, config, "/voice", fields(config, **changes)).status_code == 403
    assert not Store(config.state).jobs()


def test_signature_tampering_and_body_size(config):
    app = create_app(config)
    with app.test_client() as client:
        assert client.post("/voice", data=fields(config)).status_code == 403
        assert client.post("/voice", json=fields(config)).status_code == 415
        assert client.post("/voice", data={"Digits": "1" * 17000}).status_code == 413
    action, data = start(app, config)
    data["Digits"] = config.code
    assert post(app, config, action, data, **{"X-Twilio-Signature": "bad"}).status_code == 403
    assert not Store(config.state).jobs()


def test_proxy_signature_uses_external_origin(config):
    app = create_app(config)
    assert post(app, config, "/voice", fields(config), Host="internal:8787",
                **{"X-Forwarded-Host": "untrusted.example"}).status_code == 200


@pytest.mark.parametrize("digits", ["", "00000000", "86753091;touch /tmp/injected", "🌈"])
def test_wrong_code_cannot_retry_with_correct_code_in_same_call(config, digits):
    app = create_app(config)
    action, data = start(app, config)
    assert b"Access denied" in post(app, config, action, {**data, "Digits": digits}).data
    assert b"Access denied" in post(app, config, action, {**data, "Digits": config.code}).data
    assert b"Gather" not in post(app, config, "/voice", data).data
    assert not Store(config.state).jobs()


def test_expired_session_and_mismatched_nonce(config):
    app = create_app(config)
    action, data = start(app, config)
    store = Store(config.state)
    assert b"Access denied" in post(app, config, "/activate?nonce=" + "x" * 32,
                                    {**data, "Digits": config.code}).data
    with store.transaction() as db:
        db.execute("UPDATE calls SET created=?", (time.time() - 301,))
    assert b"Access denied" in post(app, config, action, {**data, "Digits": config.code}).data
    assert not store.jobs()


def test_different_call_or_caller_cannot_use_session(config):
    app = create_app(config)
    action, data = start(app, config)
    for changes in ({"CallSid": "CA" + "b" * 32}, {"From": "+16175559999"}):
        assert b"Access denied" in post(app, config, action,
                                       {**data, **changes, "Digits": config.code}).data
    assert not Store(config.state).jobs()


def test_caller_filter_and_persistent_rate_limit(config):
    restricted = dataclasses.replace(config, allowed_callers=frozenset({"+16175559999"}))
    assert post(create_app(restricted), restricted, "/voice", fields(config)).status_code == 403
    app = create_app(config)
    for index in range(1, 6):
        start(app, config, index)
    # Initial webhook retries consume no additional allowance.
    start(app, config, 1)
    response = post(create_app(config), config, "/voice", fields(config, 6))
    assert b"Access unavailable" in response.data


def test_global_limit_also_covers_different_callers(config):
    app = create_app(config)
    for index in range(20):
        start(app, config, index, From=f"+1617555{index:04d}")
    assert b"Access unavailable" in post(app, config, "/voice", fields(config, 21, From="anonymous")).data


def test_concurrent_callbacks_only_enqueue_one_job(config):
    app = create_app(config)
    action, data = start(app, config)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post(app, config, action, {**data, "Digits": config.code}), range(12)))
    assert all(b"Command accepted" in response.data for response in responses)
    assert len(Store(config.state).jobs()) == 1


def test_busy_queue_and_cooldown(config):
    app = create_app(config)
    action, data = start(app, config)
    post(app, config, action, {**data, "Digits": config.code})
    action2, data2 = start(app, config, 2)
    assert b"busy" in post(app, config, action2, {**data2, "Digits": config.code}).data
    store = Store(config.state)
    store.finish(data["CallSid"], "succeeded", 0)
    action3, data3 = start(app, config, 3)
    assert b"busy" in post(app, config, action3, {**data3, "Digits": config.code}).data
    with store.transaction() as db:
        db.execute("UPDATE jobs SET created=?", (time.time() - 31,))
    action4, data4 = start(app, config, 4)
    assert b"Command accepted" in post(app, config, action4, {**data4, "Digits": config.code}).data


@pytest.mark.parametrize("interruptible", [True, False])
def test_audio_greeting_timing_and_public_media(config, interruptible):
    recording = config.state / "greeting.wav"
    recording.write_bytes(b"RIFF-test-recording")
    config = dataclasses.replace(config, greeting_file=recording, interruptible=interruptible, digit_timeout=15)
    app = create_app(config)
    tree = ElementTree.fromstring(post(app, config, "/voice", fields(config)).data)
    assert tree.find("Gather").attrib["timeout"] == "15"
    assert tree.find("Gather").attrib["finishOnKey"] == "#"
    assert tree.find("Gather").attrib["actionOnEmptyResult"] == "true"
    assert tree.find("Gather/Play" if interruptible else "Play").text == config.public_url + "/greeting"
    assert tree.find("Gather/Say") is None
    with app.test_client() as client:
        assert client.get("/greeting").data == recording.read_bytes()
        assert client.get("/greeting/../../.env").status_code == 404


def queued(config):
    app = create_app(config)
    action, data = start(app, config)
    post(app, config, action, {**data, "Digits": config.code})
    return Store(config.state)


def script_file(config, content):
    script = config.state / "script.sh"
    script.write_text("#!/bin/sh\nset -eu\n" + content)
    script.chmod(0o700)
    return script


def test_script_cannot_inherit_credentials(config, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "sensitive")
    monkeypatch.setenv("TRIGGER_CODE", "sensitive")
    script = script_file(config, 'test -z "${TWILIO_AUTH_TOKEN:-}"\ntest -z "${TRIGGER_CODE:-}"\n')
    store = queued(config)
    run_next(WorkerConfig(config.state, script), store, threading.Event())
    assert store.jobs()[0]["status"] == "succeeded"


def test_script_failure_is_recorded_and_not_retried(config):
    script = script_file(config, "exit 7\n")
    store = queued(config)
    run_next(WorkerConfig(config.state, script), store, threading.Event())
    assert store.jobs()[0]["status"] == "failed"
    assert store.jobs()[0]["exit_code"] == 7
    assert not run_next(WorkerConfig(config.state, script), store, threading.Event())


def test_timeout_terminates_script(config):
    script = script_file(config, 'sleep 3\necho late > "$DIAL_IN_STATE_DIR/late"\n')
    store = queued(config)
    run_next(WorkerConfig(config.state, script, timeout=0.1), store, threading.Event())
    assert store.jobs()[0]["status"] == "timed_out"
    assert not (config.state / "late").exists()


def test_crash_recovery_never_retries_claimed_job(config):
    store = queued(config)
    assert store.claim()
    assert store.recover() == 1
    assert store.jobs()[0]["status"] == "interrupted"
    assert store.claim() is None


def test_only_one_worker_can_hold_lock(config):
    with worker_lock(config.state):
        with pytest.raises(RuntimeError, match="already running"):
            with worker_lock(config.state):
                pass


def test_stale_queued_job_expires(config):
    store = queued(config)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET created=?", (time.time() - 301,))
    assert store.claim() is None
    assert store.jobs()[0]["status"] == "expired"
