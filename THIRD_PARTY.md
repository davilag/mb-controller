# Upstream protocol implementation

This POC adapts the MIT-licensed [ReneNulschDE/mbapi2020](https://github.com/ReneNulschDE/mbapi2020)
at commit `3eae55854ac62fe3dd21474a1578658d9c3e3468` (manifest version 0.44.0).
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

Upgraded to release v0.44.0 on 2026-11-20. Home Assistant-only entity/service setup is not bundled; standalone adapters retain those responsibilities. Local rate-limit guards, bounded sessions and sanitized error evidence are retained.
