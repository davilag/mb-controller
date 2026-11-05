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

## POC features

- Mercedes password login, encrypted token persistence and token refresh.
- Assigned vehicle discovery, vehicle picker, protobuf WebSocket telemetry, and reconnect handling.
- Battery/fuel/range/odometer/locks plus a searchable table of received attributes.
- Lock/unlock, preconditioning start/stop, auxiliary heating start/stop, and flash lights,
  gated by the capabilities Mercedes reports for each vehicle.
- Charge-limit selector (50–100% in 10% steps, restricted to advertised bounds).
  Uses the supported charging command and preserves the active program; a requested
  limit is not displayed as confirmed until vehicle telemetry reports it.
- Configurable daily charge-limit schedule that runs on the backend with the browser closed.
- Explicit confirmation for commands; unlock asks for the four-digit vehicle security PIN.
- Command submission is reported as **sent**, never as vehicle completion. Last reported
  command state/errors appear separately and may refer to another command until an update arrives.
- Disconnect deletes saved account tokens. HTTP Basic credentials are cached by your
  browser; close its session or use a private window to avoid sharing access.

## Daily charge-limit schedule

In **Charge-limit schedule**, edit the start time and target percentage for each period.
The initial example is **50% at 05:30** and **80% at 23:30**. Each target lasts until the next
period; the last wraps to the next day. Add or remove periods as needed. Turn on **Enable daily
schedule** and click **Save and enable schedule** to activate it. Saving while the checkbox is off
only stores the configuration and does not send a vehicle command.

All times use the backend's `TZ` setting, defaulting to **Europe/London**, including BST/GMT changes.
On the spring clock change, a time skipped by the clock runs at the next valid local minute;
on the autumn change, a repeated time runs on its first occurrence. Docker includes timezone data.

The scheduler applies the active period when enabled, after startup or reconnection, and at each
boundary. It checks every 20 seconds and maintains the active target if it observes a different
reported limit. While enabled, manual limit changes in this app are blocked; **Turn schedule off**
first to set a manual limit. Lowering a charge limit does not discharge the battery.

Settings persist encrypted in the same data volume as the account. The server and Mercedes
connection must remain available; this is not a schedule stored in the car. If the backend was
offline at a boundary, it applies the current period when it reconnects rather than replaying
missed periods. A requested change remains pending until vehicle telemetry confirms the target.
Up to three requests are sent, with retries after 2 and then 4 minutes. If the third request
is still unconfirmed after another 8 minutes, the UI reports an error. Save the schedule again
to retry. Each new period gets a fresh retry allowance.
Disconnecting the account disables the schedule.

## Account requirements and limits

Sign in to the official Mercedes-Benz app first and accept any current terms there.
Your account must have access to the vehicle and the necessary remote-service subscriptions.
The upstream password flow does not support MFA-required accounts. Prefer a dedicated secondary
vehicle account when possible; simultaneous use of unofficial and official clients can cause
account blocking. See the upstream README for its account recommendations.

This is an early POC using an unofficial cloud API. Login and telemetry have been observed on
the connected EQB; charge-limit commands and overnight scheduling still need live verification.
Available features differ by vehicle, region, and subscription. China login,
engine start, route sending, vehicle-stored departure schedules, and charging start/stop are outside this POC.
Empty metrics mean data has not arrived or is not available; no telemetry is invented.

The password and vehicle PIN are not saved. Refresh tokens are encrypted in the Docker volume,
with the encryption key in the same private volume; this protects token files, not a compromised
server or a full-volume backup. Back up the volume privately. Run one backend process per volume.
After restart the app attempts token refresh and reconnects; if Mercedes requires login again,
use **Reconnect account**. Upstream automatic credential login is unavailable because the password
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

## Mercedes reconnection timing

Automatic reconnects wait for the next wall-clock ten-minute boundary
(`:00`, `:10`, `:20`, `:30`, `:40`, `:50`). A disconnect at either 10:32
or 10:38 retries at 10:40; a failed attempt waits for the following boundary.
Initial startup/account connection remains immediate. Existing idle-disconnect
and user-command behavior is unchanged. Vehicle telemetry can be unavailable
between reconnects, so the charging scheduler may wait for a connection.

Connection diagnostics are available in authenticated `/api/state` under
`connectionDiagnostics`. A bounded event history and the separate `mb.connection`
logger distinguish local idle timeouts, remote WebSocket close codes, transport
failures and HTTP handshake errors without logging credentials or message bodies.
