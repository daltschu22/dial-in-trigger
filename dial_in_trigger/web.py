import hmac
import os
import re

from flask import Flask, Response, abort, request, send_file
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from .config import WebConfig
from .store import Store


def create_app(config=None):
    os.umask(0o077)
    config = config or WebConfig.from_env()
    store = Store(config.state)
    validator = RequestValidator(config.auth_token)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    def xml(response):
        return Response(str(response), mimetype="application/xml", headers={"Cache-Control": "no-store"})

    def goodbye(message):
        response = VoiceResponse()
        response.say(message)
        response.hangup()
        return xml(response)

    def validate():
        # Use the configured external origin, not attacker-controlled Host/Forwarded headers.
        if request.mimetype != "application/x-www-form-urlencoded":
            abort(415)
        url = config.public_url + request.path
        if request.query_string:
            try:
                url += "?" + request.query_string.decode("ascii", errors="strict")
            except UnicodeDecodeError:
                abort(400)
        if not validator.validate(url, request.form, request.headers.get("X-Twilio-Signature", "")):
            abort(403)
        if any(len(request.form.getlist(key)) != 1 for key in request.form):
            abort(400)
        if (request.form.get("AccountSid") != config.account_sid
                or request.form.get("To") != config.phone_number
                or not re.fullmatch(r"CA[0-9a-fA-F]{32}", request.form.get("CallSid", ""))
                or not request.form.get("From")
                or (config.allowed_callers and request.form["From"] not in config.allowed_callers)):
            abort(403)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/greeting")
    def greeting():
        # Deliberately public: Twilio's media fetcher/cache needs to retrieve this recording.
        if config.greeting_file is None:
            abort(404)
        return send_file(config.greeting_file, conditional=True, max_age=0)

    @app.post("/voice")
    def voice():
        validate()
        if request.query_string:
            abort(400)
        nonce = store.open_call(request.form["CallSid"], request.form["From"])
        if not nonce:
            return goodbye("Access unavailable. Goodbye.")
        response = VoiceResponse()
        if config.greeting_file and not config.interruptible:
            response.play(config.public_url + "/greeting")
        gather = response.gather(input="dtmf", action=config.public_url + "/activate?nonce=" + nonce,
                                 method="POST", finish_on_key="#", timeout=config.digit_timeout,
                                 action_on_empty_result=True)
        if config.greeting_file and config.interruptible:
            gather.play(config.public_url + "/greeting")
        elif not config.greeting_file and config.prompt:
            gather.say(config.prompt)
        response.hangup()
        return xml(response)

    @app.post("/activate")
    def activate():
        validate()
        if set(request.args) != {"nonce"} or len(request.args.getlist("nonce")) != 1:
            abort(400)
        nonce = request.args["nonce"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", nonce):
            abort(400)
        digits = request.form.get("Digits", "")
        matches = hmac.compare_digest(digits.encode(), config.code.encode())
        result = store.activate(request.form["CallSid"], request.form["From"], nonce, matches)
        messages = {"accepted": "Command accepted.", "busy": "The command is busy. Please try again later.",
                    "denied": "Access denied. Goodbye."}
        return goodbye(messages[result])

    return app

