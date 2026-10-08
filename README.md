# Dial-in trigger

Call your Twilio number, hear a recording or spoken prompt, enter your secret
keypad sequence, and run a script on **your own server**.

```text
Phone → Twilio (audio + keypad decoding) → signed HTTPS webhook
      → web container → persistent SQLite queue → worker container → local script
```

Both containers run on your server. Twilio handles the call; the local worker executes the script.
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

You need a voice-enabled Twilio number, its account SID and Auth Token, and an
HTTPS hostname that can reach the web container. All examples use fictional
identifiers. This project does not buy a phone number or modify its settings.

### Docker Compose

```sh
cp .env.example .env
chmod 600 .env
# Edit .env: account SID, Auth Token, your number, HTTPS origin, and private code.
docker compose up -d --build
```

The HTTP listener binds to `127.0.0.1:8787` by default. Put your HTTPS reverse
proxy or tunnel in front of it, using `deploy/Caddyfile.example` as a routing
example. A containerized proxy needs access to the Compose network; a proxy on
the same host can forward to `127.0.0.1:8787`. Set `WEB_PORT` if that port is busy.
Only expose POST `/voice`, POST `/activate`, and GET/HEAD `/greeting` publicly.
Keep `/healthz` private.

The named volumes retain SQLite state, scripts, and greeting audio across
rebuilds. The scripts volume is initially populated with the harmless demo,
which writes a timestamp and call ID to `/data/demo-runs.log`. Do not run
`docker compose down -v` unless you intend to delete this state and its replay
protection. Back up the volumes before migrating or replacing a deployment.

In Twilio, set the number's **Voice → A call comes in** webhook to
`https://YOUR-HOST/voice`, method **POST**. `PUBLIC_BASE_URL` must match that
HTTPS origin exactly. An API key secret cannot replace the Account Auth Token
for webhook signature verification.

### Coolify

Create a Git repository application, select the **Docker Compose** build pack,
and use `/docker-compose.yaml`. Set the variables below in the application's
runtime environment before deploying. The Compose file explicitly passes
credentials only to the web service; the worker receives no Twilio secrets.
Disable previews so they cannot receive production credentials or share state.

| Variable | Purpose |
| --- | --- |
| `TWILIO_ACCOUNT_SID` | Your account's AC identifier |
| `TWILIO_AUTH_TOKEN` | Your account's secret webhook signing token |
| `TWILIO_PHONE_NUMBER` | Your number in E.164 format |
| `PUBLIC_BASE_URL` | Your external HTTPS origin |
| `TRIGGER_CODE` | The private keypad sequence |
| `VOICE_PROMPT` | Spoken greeting when no audio file is selected |
| `GREETING_FILE` | Optional absolute path under `/media` |
| `GREETING_INTERRUPTIBLE` | Whether a key can interrupt the audio |
| `DIGIT_TIMEOUT_SECONDS` | Allowed pause between digits, default 10 |
| `ALLOWED_CALLERS` | Optional comma-separated caller filter |
| `TRIGGER_SCRIPT` | Script path inside the worker, default `/scripts/trigger.sh` |
| `SCRIPT_TIMEOUT_SECONDS` | Script time limit, default 60 |
| `WEB_BIND_ADDRESS` / `WEB_PORT` | Local listener, default `127.0.0.1:8787` |

This Compose definition uses an external HTTPS proxy/tunnel, with automatic
Traefik routing disabled. Route only the three public endpoints described above.
Use Coolify's Git integration for code deployments. Terraform can manage the
application and its runtime variables separately; personal deployment values
and Terraform state should not live in a public application repository.

To install a recording, use an administrator maintenance container or the media
volume's host directory to copy in a readable audio file. Set
`GREETING_FILE=/media/greeting.wav` and redeploy. Use the same process to install
an executable script in the scripts volume. Both mounts are read-only inside
the running application containers. The project does not include third-party
recordings.

### Podman

`deploy/*.container` provides alternative rootful Quadlets. They expect host
folders under `/opt/services/dial-in-trigger` and a shared `services.network`.
Create the network or adapt the units to your proxy's network. Give the `data`
directory UID/GID 10001 and mode 0700; scripts and media must be readable by that
UID. Create root-only `web.env` and `worker.env` files containing the appropriate
variables from the table. Keep the Auth Token and keypad code out of `worker.env`.
Install the Quadlets, reload systemd, and start both services. Do not run the
Compose and Quadlet workers against independent copies of the same live state.

## Running your script

Replace `trigger.sh` in the scripts volume with your executable script. It runs as
UID 10001, without a shell-built command or caller-supplied arguments. The default
image includes Python and `/bin/sh`; add required packages to the Containerfile.
Use `/data` for writable state, or explicitly add the required bind mounts.

The child receives only `PATH`, `LANG`, `HOME`, `DIAL_IN_STATE_DIR`, and
`TRIGGER_CALL_SID`. It does not inherit the Twilio token or keypad code. Use the
call ID as an idempotency key if your script invokes another service.

Inspect execution without exposing configuration:

```sh
docker compose exec worker python -m dial_in_trigger jobs
docker compose logs worker
docker compose exec worker cat /data/demo-runs.log
```

Script stdout/stderr go to the worker's container logs; scripts should avoid printing
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
- The always-running worker polls for work every half-second while idle.
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

## License

MIT. See [LICENSE](LICENSE).
