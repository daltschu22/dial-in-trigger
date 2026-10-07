# Dial-in trigger

Call your Twilio number, hear a recording or spoken prompt, enter your secret
keypad sequence, and run a script on **your own server**.

```text
Phone → Twilio (audio + keypad decoding) → signed HTTPS webhook
      → web container → persistent SQLite queue → worker container → local script
```

Both containers run on `container-host`. Twilio does not execute the script.
The worker executes an explicitly configured file inside its container. Mount
specific directories if the script needs server files; it has no implicit access
to the host filesystem or container socket. For host-level actions, integrate a
narrow, separately authorized host service instead of making the container privileged.

Asterisk is also valid. This version uses Twilio's call handling so a dedicated
PBX VM is unnecessary.

## Call behavior

- `/voice` returns TwiML with DTMF-only `<Gather>`.
- `GREETING_FILE=/media/greeting.wav` plays your recording. Leave it empty to use
  `VOICE_PROMPT`; set both empty for silence. WAV is preferable for prompt latency.
- `GREETING_INTERRUPTIBLE=true` lets the first key interrupt the recording.
  Set it to `false` to finish the recording before listening for the code.
- `DIGIT_TIMEOUT_SECONDS=10` allows pauses of less than ten seconds between keys.
  `#` submits immediately; a timeout also submits the digits collected so far.
  The code accepts 2–32 digits or `*`, but not the terminating `#`. Short codes
  such as `12` are suitable for the demo; prefer at least 8 random digits for real actions.
- A correct code queues the command and says **“Command accepted.”** This means
  queued, not completed. A wrong/empty code ends the call; redial to try again.
- Ordinary pauses are supported. Precise pause lengths and key-hold duration
  are **not** part of authentication: `<Gather>` delivers the digit sequence.

Twilio retrieves the greeting over HTTPS at `/greeting`; that one recording is
publicly fetchable. Use a recording intended for callers. No directory browsing
or arbitrary media-file endpoint is exposed.

## Setup

The example number is **+12025550100**, with existing phone-number SID
`PN00000000000000000000000000000000`. This project does not buy a number.

1. Build the image: `podman build -t localhost/dial-in-trigger:1 .`
2. Create persistent host directories under `/opt/services/dial-in-trigger`:
   `data` (UID/GID 10001, mode 0700), `scripts`, and `media`.
3. Copy `scripts/demo.sh` to the host's `scripts/trigger.sh`, mode 0755.
   It writes a timestamp and call ID to `/data/demo-runs.log` and does nothing else.
4. Create root-readable `web.env` using `.env.example`, with the account's Auth
   Token, your private code, and the actual public HTTPS origin. An API key secret
   cannot replace the Auth Token for webhook signature validation.
5. Create a separate root-readable `worker.env` containing only:

   ```dotenv
   DIAL_IN_STATE_DIR=/data
   TRIGGER_SCRIPT=/scripts/trigger.sh
   SCRIPT_TIMEOUT_SECONDS=60
   ```

6. Install the two `deploy/*.container` Quadlets in `/etc/containers/systemd/`.
   They use the existing `services.network`. Run `systemctl daemon-reload`, then
   `systemctl start dial-in-trigger-web dial-in-trigger-worker`. Quadlet's
   `[Install]` section handles startup at boot; do not enable generated units.
7. Route the hostname through Caddy and the existing Cloudflare Tunnel. Expose
   only POST `/voice`, POST `/activate`, and GET/HEAD `/greeting`; see the Caddy
   example. Keep `/healthz` internal. `PUBLIC_BASE_URL` must match the external
   origin exactly, even though Caddy forwards plain HTTP inside the container network.
8. Configure the existing number's **Voice → A call comes in** webhook to
   `https://YOUR-HOST/voice`, method **POST**. Inspect its current voice application
   or SIP trunk first; those can take precedence over the webhook. SMS settings
   and outgoing calls are separate.

`home-ansible` owns deployment on the homelab and `home-tf/cloudflare` owns public
DNS/tunnel routing. Twilio resources are not currently Terraform-managed. The
existing account, number, and other app's stored credentials can be reused.

## Running your script

Replace the host's `scripts/trigger.sh` with your executable script. It runs as
UID 10001, without a shell-built command or caller-supplied arguments. The default
image includes Python and `/bin/sh`; add required packages to the Containerfile.
Use `/data` for writable state, or explicitly add the required bind mounts.

The child receives only `PATH`, `LANG`, `HOME`, `DIAL_IN_STATE_DIR`, and
`TRIGGER_CALL_SID`. It does not inherit the Twilio token or keypad code. Use the
call ID as an idempotency key if your script invokes another service.

Inspect execution without exposing configuration:

```sh
sudo podman exec dial-in-trigger-worker python -m dial_in_trigger jobs
sudo journalctl -u dial-in-trigger-worker
sudo cat /opt/services/dial-in-trigger/data/demo-runs.log
```

Script stdout/stderr go to the worker's journal; scripts should avoid printing
their own secrets. Execution has a configurable timeout, and shutdown/timeout
terminates the script's process group. Scripts must run in the foreground and
must not daemonize or escape their process group.

## Reliability and access

- Twilio's SDK validates every voice webhook signature against the exact public
  URL and form body; account, called number, call ID, and optional caller allowlist
  are checked independently. No validation-disable option exists.
- One code attempt per call; five new calls per caller and twenty globally in a
  rolling five-minute window. Caller ID is an extra filter, not authentication.
- Each call requires its original five-minute session nonce. Decisions and call
  IDs persist, so retries and application restarts cannot enqueue the call twice.
- SQLite transactions serialize submissions. A process lock permits one worker.
  One queued/running job and a thirty-second cooldown prevent overlapping launches.
- Queued jobs expire after five minutes. A job interrupted after being claimed
  is marked `interrupted` and is never automatically retried. Delivery is
  **at most once**, not a promise of completion: a crash between claiming a job
  and launching it can leave it unexecuted. Check the local result before redialing.
- Code digits are never written to this app's database or logs. They travel
  through Twilio; Twilio's own call/request records have separate retention.
- Preserve `/data` across rebuilds. Deleting the database also deletes replay
  protection and job history. It contains call IDs, caller numbers, and decisions.

## Development and verification

```sh
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[test]'
.venv/bin/python -m pytest -q
```

`requirements.lock` pins tested runtime and test dependencies. Tests use fake
Twilio credentials/signatures and harmless local scripts. They cover rejection,
retries, concurrency, rate limits, expiry, media prompts, timeouts, and crash
recovery. They do not place calls or alter Twilio.

References: [Gather](https://www.twilio.com/docs/voice/twiml/gather),
[Play](https://www.twilio.com/docs/voice/twiml/play),
[request validation](https://www.twilio.com/docs/usage/security),
[phone-number API](https://www.twilio.com/docs/phone-numbers/api/incomingphonenumber-resource).
