# MB Local — standalone React + Mercedes POC

A small self-hosted control panel built on the protocol code from
[mbapi2020](https://github.com/ReneNulschDE/mbapi2020). **No Home Assistant required.**

## Quick start with Docker

```sh
cp .env.example .env
# Edit .env and set APP_PASSWORD to your own local app password.
docker compose pull
docker compose up -d
```

Open **http://localhost:8080**, use the browser's HTTP Basic authentication prompt
(username `admin`, password `APP_PASSWORD`), then enter your Mercedes
email/password and choose **Europe** for UK accounts. Credentials go to your local backend;
the backend connects to Mercedes cloud services. This does not make the car connection offline/local.

For another device on your LAN, set both values in `.env` before starting:

```dotenv
BIND_ADDRESS=0.0.0.0
PUBLIC_ORIGIN=http://192.168.1.20:8080
```

Replace the address with your server's LAN address. When using an HTTPS reverse proxy,
set `PUBLIC_ORIGIN` to its HTTPS URL, preserve the Host header, and proxy to port 8080.
Use HTTPS on untrusted networks; passwords and PINs pass through the browser connection.

### Images and updates

Every push to `main` builds and publishes
`ghcr.io/davilag/mb-controller` for **linux/amd64** and **linux/arm64**.
Pull requests build the amd64 image without publishing it. The workflow uses
GitHub's built-in `GITHUB_TOKEN`; no personal registry token is needed in CI.

Published tags are `latest`, `sha-<full commit SHA>` and `sha-<short commit SHA>`.
For reproducible deployments, set `MB_CONTROLLER_IMAGE` in `.env` to a commit
tag or image digest rather than `latest`. To update a running installation:

```sh
docker compose pull
docker compose up -d
```

If the GHCR package is private, authenticate before pulling:
`docker login ghcr.io -u YOUR_GITHUB_USER` using a token with `read:packages`.
For anonymous pulls, the package owner must make the GHCR package public.

To build locally instead of using the published image:

```sh
docker compose -f compose.yaml -f compose.build.yaml up --build -d
```

### Runtime dependencies

Compose needs **only the `mb-local` container**. There is no separate database,
Redis, MQTT broker, or Home Assistant dependency. Python, its pinned libraries
(`aiohttp`, `protobuf`, `cryptography` and transitive dependencies), timezone
data, and the compiled React frontend are bundled in the image. Node/npm is
only used while building the frontend, not at runtime.

Provide:

- `APP_PASSWORD`: a strong password for this web app, supplied through `.env`.
- `APP_USERNAME`: HTTP Basic authentication username (default `admin`). The browser
  prompts before loading the app; the health endpoint remains public for Docker.
- `PUBLIC_ORIGIN`: the URL used by your browser, including its port. This is
  also checked against incoming requests; it must match your LAN/proxy URL.
- `TZ`: the charging-schedule timezone, default `Europe/London`.
- Persistent volume `mb-data` mounted at `/app/data` for encrypted account
  tokens, their encryption key, and schedule configuration. Keep it across
  container updates; `docker compose down -v` deletes this stored state.
- Outbound DNS and HTTPS/WebSocket connectivity to Mercedes cloud services.
- An inbound mapping to container port `8080` (loopback-only by default).

Use one backend instance per data volume. Mercedes account credentials and
the vehicle PIN are entered in the web UI, not added to Compose. An HTTPS
reverse proxy is optional for trusted local-only access and recommended for
access across untrusted networks. The image includes an `/api/health` check.

## One-night charging

MB Local is quiet by default. Its mobile-friendly page lets you choose a **night starting**
date and exactly two changes, for example **80% at 23:30** and **50% at 05:30 the next day**.
Click **Schedule this night**. Nothing repeats the following night. **Cancel night** removes
pending changes without contacting Mercedes. Replacing a night also remains local-only.

- Startup, restarts, browser refreshes, health checks, API state reads, saving and cancelling
  do **not** refresh Mercedes tokens, fetch vehicle data or open a WebSocket.
- At each due time the server opens **one** session, with a 65-second overall deadline for
  token refresh if necessary, discovery, fresh telemetry, command submission and confirmation.
  It closes the socket and HTTP sessions after success or failure. There is no background
  reconnect loop, automatic command retry, password relogin or keep-alive between slots.
- A fresh matching vehicle limit is labelled **Already at target** without sending a command.
  Otherwise the UI says **Confirmed by car** only after a new telemetry update reports the
  requested target. A sent request without matching telemetry is **Not confirmed**.
- Every slot is durably marked as running **before** opening the connection. An interrupted
  slot is not replayed after restart, even if the command may have reached the car.
- Missed slots more than 90 seconds late are marked **Missed**, never caught up. This grace
  only accommodates timer/startup jitter. After the second slot, the schedule disarms itself.
- HTTP 429 from discovery, auth refresh or the WebSocket immediately fails the session.
  The app persists a conservative **24-hour minimum safety cooldown**, extended if Mercedes
  supplies a longer `Retry-After`. Slots falling within that cooldown are skipped without
  connecting, and manual account login is blocked too. This is a local safety policy,
  **not a guarantee that Mercedes removes the restriction after 24 hours**.
- The former recurring daily schedule is deliberately **not migrated into an armed night**.
  Previously selected vehicle details remain available locally; migrated limit choices are
  checked against current vehicle capabilities when the scheduled session actually runs.
- Manual remote command endpoints are disabled in quiet mode. Initial/replacement account
  setup is an explicit exception: it contacts Mercedes once for login and vehicle discovery,
  then closes the HTTP sessions without starting background telemetry.

All times use the backend's `TZ`, default **Europe/London**, including BST/GMT changes.
Skipped spring times run at the next valid local minute; repeated autumn times use their
first occurrence. The UI previews the separate evening and morning calendar dates.

Keep the server running overnight; this is **not** a schedule stored inside the car.
Lowering the limit will not discharge the battery. Check an unconfirmed or failed slot in the
official Mercedes app. Configuration, execution records, cached vehicle details, tokens and
cooldown persist encrypted in the Docker data volume. Run exactly one backend per volume.

### Verification

```bash
uv run --with-requirements requirements-dev.txt python -m pytest -q
npm ci
npm run build
# Optional UI check (owns a disposable static server; no Mercedes calls):
uv run --with playwright playwright install chromium webkit
uv run --with playwright python scripts/check-mobile.py
MB_UI_ENGINE=webkit uv run --with playwright python scripts/check-mobile.py
```

The browser check verifies 320/390/768/1280px layouts, 44px touch controls, no horizontal
page overflow, arm/cancel interactions, and cooldown display. Screenshots go to
`/tmp/mb-one-night-ui`. Set `MB_UI_TEST_ORIGIN` for another frontend server, or
`MB_UI_CHROMIUM` for an existing Chromium executable. All API responses in that check
are synthetic; backend transport tests also use local fakes, not real vehicle commands.

## Account requirements and limits

Sign in to the official Mercedes-Benz app first and accept any current terms there.
Your account must have access to the vehicle and the necessary remote-service subscriptions.
The upstream password flow does not support MFA-required accounts. Prefer a dedicated secondary
vehicle account when possible; simultaneous use of unofficial and official clients can cause
account blocking. See the upstream README for its account recommendations.

This is an unofficial cloud integration. Previous scheduled 80% and 50% changes were
confirmed by vehicle telemetry; the new one-night strategy is tested using local transport
fakes without sending extra live commands. Available capabilities differ by vehicle and subscription.

The password and vehicle PIN are not saved. Refresh tokens are encrypted in the Docker volume,
with the encryption key in the same private volume; this protects token files, not a compromised
server or a full-volume backup. Back up the volume privately. Run one backend process per volume.
After restart the app remains quiet. If a scheduled session reports expired credentials,
use **Account** to set up the account again when no rate-limit cooldown is active. Upstream automatic credential login is unavailable because the password
is not retained. Protect internet-facing access with an authenticated HTTPS proxy.

## Local development

### Keep the local Mac running overnight

The local POC can run independently of the chat with a detached supervisor and macOS
sleep prevention. Stop any manually started backend before starting the supervisor:

```sh
.venv/bin/python scripts/overnight.py start
.venv/bin/python scripts/overnight.py status
.venv/bin/python scripts/overnight.py stop
```

The supervisor restarts the backend after an unexpected exit. It keeps the Mac awake
while running, but does not prevent shutdown, logout, or lid-close sleep. Leave the laptop
plugged in, with its lid open and Internet access available. It does not auto-start after
a reboot. Logs are in `data/logs/overnight.log`. Stopping it releases its sleep assertions.

### Build and run manually

Requires Node 22+ and Python 3.12+.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm ci
npm run build
.venv/bin/python -m server.app
```

The local server binds to `127.0.0.1:8080` by default. Optional: set `APP_PASSWORD` for
HTTP Basic authentication (username `admin`, or `APP_USERNAME`). A non-loopback backend binding requires it. For frontend hot reload,
run `npm run dev` in another terminal; Vite forwards `/api` to the backend.

```sh
npm run build
.venv/bin/python -m pytest -q
docker compose config
```

See [THIRD_PARTY.md](THIRD_PARTY.md) for attribution and the pinned upstream revision.

## Connection lifecycle and monitoring

The vendored protocol retains its old reconnect implementation for upstream compatibility,
but the app runs sockets in **single-attempt mode**. This bypasses all reconnect/relogin
loops and refuses command or acknowledgement sends that would open a second socket.
The upstream 30-second idle protection remains; the entire scheduled session also has
an outer deadline. No reconnect watchdog or component reload watcher is armed for a slot.

Authenticated `/api/state` exposes `quietMode`, `intentionalIdle`, `accountBlocked`,
`cooldownUntil`, cached vehicle details and the current session's sanitized
`connectionDiagnostics`. Expected disconnected state is not an outage. Inspect each
`schedule.jobs` execution status to monitor confirmation, missed slots and failures;
a saved schedule or sent command alone is not confirmation.

## Upstream findings (2026-11-05)

- [#449](https://github.com/ReneNulschDE/mbapi2020/issues/449#issuecomment-5928026090):
  the maintainer explicitly says the 30-second idle disconnect is intentional
  protection against Mercedes account throttling. Do not "fix" this by keeping
  an otherwise idle socket alive indefinitely. The maintainer's reported daily
  connected-time threshold is an observation, not an official Mercedes guarantee.
- [#407](https://github.com/ReneNulschDE/mbapi2020/issues/407#issuecomment-4395172359):
  actual commands must bypass background reconnect cooldowns; otherwise they can
  be silently dropped. The standalone wrapper must not preempt that path merely
  because the socket is currently idle. Never mark a command sent after a failed
  transport call, and never mark vehicle application confirmed on transport alone.
- [#467](https://github.com/ReneNulschDE/mbapi2020/issues/467): a connected socket
  does not guarantee current telemetry. In that report, accepting updated privacy
  terms in the official app resolved missing data; this is not evidence that every
  missing update is the same problem.
- Follow upstream's [account guidance](https://github.com/ReneNulschDE/mbapi2020#how-to-prevent-account-blocking):
  use a dedicated invited secondary account for the integration rather than using
  the same account concurrently in the integration and official apps.

The UI distinguishes intentional idle from rate limiting. Operational monitors
should not treat routine protective idle as an outage; failures, rate limiting,
and unconfirmed schedule application still require attention.
