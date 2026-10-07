import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in the service environment.")
    return value


def positive_int(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


def state_dir():
    path = Path(required("DIAL_IN_STATE_DIR"))
    if not path.is_absolute():
        raise ValueError("DIAL_IN_STATE_DIR must be an absolute path.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class WebConfig:
    public_url: str
    account_sid: str
    auth_token: str = field(repr=False)
    phone_number: str
    code: str = field(repr=False)
    state: Path
    allowed_callers: frozenset[str] = frozenset()
    prompt: str = "Enter your access code, followed by pound."
    greeting_file: Path | None = None
    interruptible: bool = True
    digit_timeout: int = 10

    @classmethod
    def from_env(cls):
        public_url = required("PUBLIC_BASE_URL").rstrip("/")
        url = urlsplit(public_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin without a path or credentials.")
        account = required("TWILIO_ACCOUNT_SID")
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account):
            raise ValueError("TWILIO_ACCOUNT_SID must be an AC SID.")
        number = required("TWILIO_PHONE_NUMBER")
        allowed = frozenset(x.strip() for x in os.getenv("ALLOWED_CALLERS", "").split(",") if x.strip())
        if not all(re.fullmatch(r"\+[1-9][0-9]{7,14}", x) for x in (number, *allowed)):
            raise ValueError("Phone numbers must use E.164 format, such as +12025550100.")
        code = required("TRIGGER_CODE")
        if not re.fullmatch(r"[0-9*]{8,32}", code):
            raise ValueError("TRIGGER_CODE must contain 8–32 keypad characters (digits or *); # submits.")
        greeting = os.getenv("GREETING_FILE", "").strip()
        greeting_file = Path(greeting) if greeting else None
        if greeting_file and (not greeting_file.is_absolute() or not greeting_file.is_file()):
            raise ValueError("GREETING_FILE must be an absolute path to an existing audio file.")
        interruptible = os.getenv("GREETING_INTERRUPTIBLE", "true").lower()
        if interruptible not in {"true", "false"}:
            raise ValueError("GREETING_INTERRUPTIBLE must be true or false.")
        timeout = positive_int("DIGIT_TIMEOUT_SECONDS", 10)
        if timeout > 60:
            raise ValueError("DIGIT_TIMEOUT_SECONDS must be at most 60.")
        return cls(public_url, account, required("TWILIO_AUTH_TOKEN"), number, code,
                   state_dir(), allowed, os.getenv("VOICE_PROMPT", cls.prompt),
                   greeting_file, interruptible == "true", timeout)


@dataclass(frozen=True)
class WorkerConfig:
    state: Path
    script: Path
    timeout: int = 60

    @classmethod
    def from_env(cls):
        script = Path(required("TRIGGER_SCRIPT"))
        if not script.is_absolute() or not script.is_file() or not os.access(script, os.X_OK):
            raise ValueError("TRIGGER_SCRIPT must be an absolute path to an executable file.")
        return cls(state_dir(), script, positive_int("SCRIPT_TIMEOUT_SECONDS", 60))

