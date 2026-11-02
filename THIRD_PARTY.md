# Upstream protocol implementation

This POC adapts the MIT-licensed [ReneNulschDE/mbapi2020](https://github.com/ReneNulschDE/mbapi2020)
at commit `8b5b75c4b0a6593047c2b2dd984f1f713b09ff0e` (manifest version 0.43.0).
Original copyright and license are retained in `server/mbapi/LICENSE`.

`server/mbapi/` contains the upstream protocol client, OAuth, REST/WebSocket handling,
vehicle parsing, and generated protobuf modules. Changes:

- Redirect internal imports to `server.mbapi`.
- Replace Home Assistant imports with the small adapter in `server/runtime.py`.
- Retain only protocol constants; omit HA entity metadata and service schemas.
- Reject legal-consent prompts and direct users to the official app instead of accepting terms automatically.

`server/bridge.py` adds the standalone lifecycle and HTTP-facing state/command model.
Home Assistant is not installed or required. Upstream code contains additional command
builders, but only the explicit allowlist in `bridge.py` is reachable through this app.

This is an unofficial integration of a private cloud API. Mercedes can change authentication
or vehicle endpoints independently of this project.
