"""Executes the configured file on this machine, outside the web request."""

import fcntl
import logging
import os
import pwd
import signal
import subprocess
import threading
import time
from contextlib import contextmanager

from .store import Store

logger = logging.getLogger(__name__)


@contextmanager
def worker_lock(state):
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another worker is already running for this state directory.") from None
        yield


def terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Also kill descendants whose parent exited after SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_next(config, store, stop):
    sid = store.claim()
    if sid is None:
        return False
    logger.info("Starting job %s", sid)
    process = None
    status, exit_code = "failed", None
    try:
        # Do not pass Twilio credentials, the keypad code, or caller input to the script.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "HOME": pwd.getpwuid(os.getuid()).pw_dir,
               "DIAL_IN_STATE_DIR": str(config.state), "TRIGGER_CALL_SID": sid}
        process = subprocess.Popen([str(config.script)], cwd=config.script.parent, env=env,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + config.timeout
        while process.poll() is None:
            if stop.is_set() or time.monotonic() >= deadline:
                status = "interrupted" if stop.is_set() else "timed_out"
                terminate_group(process)
                break
            stop.wait(0.1)
        else:
            status = "succeeded" if process.returncode == 0 else "failed"
        exit_code = process.returncode
    except OSError:
        logger.error("Unable to launch configured script for job %s", sid)
    finally:
        if process is not None and process.poll() is None:
            terminate_group(process)
        store.finish(sid, status, exit_code)
    logger.info("Job %s: %s (exit %s)", sid, status, exit_code)
    return True


def serve(config):
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with worker_lock(config.state):
        store = Store(config.state)
        interrupted = store.recover()
        if interrupted:
            logger.warning("Marked %s unfinished jobs interrupted; they will not be retried.", interrupted)
        while not stop.is_set():
            if not run_next(config, store, stop):
                stop.wait(0.5)

