"""One local HTTP server serves the built React app and the Mercedes backend."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import hmac
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

from .bridge import Bridge, BridgeError
from .schedule import ChargeScheduler

ROOT = Path(__file__).resolve().parent.parent


def create_app(*, data_dir=None, password=None):
    app_password = os.environ.get("APP_PASSWORD", "") if password is None else password
    public_origin = os.environ.get("PUBLIC_ORIGIN", "http://localhost:8080").rstrip("/")
    allowed_hosts = {"localhost", "127.0.0.1", "::1", urlsplit(public_origin).hostname}
    sessions = {}
    attempts = defaultdict(deque)
    bridge = Bridge(Path(data_dir or os.environ.get("DATA_DIR", ROOT / "data")))
    scheduler = ChargeScheduler(bridge, timezone_name=os.environ.get("TZ", "Europe/London"))

    def authenticated(request):
        expiry = sessions.get(request.cookies.get("mb_session", ""), 0)
        return not app_password or expiry > time.time()

    @web.middleware
    async def guard(request, handler):
        host = urlsplit("//" + request.host).hostname
        if host not in allowed_hosts:
            return web.json_response({"error": "Host not allowed. Set PUBLIC_ORIGIN to your server URL."}, status=403)
        if request.path.startswith("/api/"):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("Origin")
                same_origin = f"{request.scheme}://{request.host}"
                if origin and origin not in {same_origin, public_origin}:
                    return web.json_response({"error": "Cross-origin request rejected."}, status=403)
            if request.path not in ("/api/health", "/api/session") and not authenticated(request):
                return web.json_response({"error": "Sign in to MB Local first."}, status=401)
        try:
            response = await handler(request)
        except BridgeError as error:
            response = web.json_response({"error": str(error)}, status=400)
        except asyncio.TimeoutError:
            response = web.json_response({"error": "Mercedes did not respond in time. Check status before retrying."}, status=504)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({"error": "Invalid request."}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).error("Request failed (%s)", request.path)
            response = web.json_response({"error": "Request failed. Check the backend connection and try again."}, status=500)
        response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    app = web.Application(middlewares=[guard], client_max_size=16 * 1024)

    async def health(request):
        return web.json_response({"ok": True})

    async def session_status(request):
        return web.json_response({"authenticated": authenticated(request), "passwordRequired": bool(app_password)})

    def rate_limit(request, group, limit, period):
        key = (request.remote, group)
        history = attempts[key]
        now = time.monotonic()
        while history and history[0] < now - period:
            history.popleft()
        if len(history) >= limit:
            raise BridgeError("Too many attempts. Wait a few minutes and try again.")
        history.append(now)

    async def session_login(request):
        rate_limit(request, "local-login", 10, 300)
        data = await request.json()
        candidate = data.get("password", "")
        if not isinstance(candidate, str) or not hmac.compare_digest(candidate.encode(), app_password.encode()):
            return web.json_response({"error": "Incorrect local app password."}, status=401)
        now = time.time()
        for old in [key for key, expiry in sessions.items() if expiry < now]:
            sessions.pop(old)
        token = secrets.token_urlsafe(32)
        sessions[token] = now + 12 * 3600
        response = web.json_response({"authenticated": True})
        response.set_cookie("mb_session", token, max_age=12 * 3600, httponly=True, samesite="Strict", secure=public_origin.startswith("https://"))
        return response

    async def session_logout(request):
        sessions.pop(request.cookies.get("mb_session", ""), None)
        response = web.json_response({"ok": True})
        response.del_cookie("mb_session")
        return response

    async def state(request):
        return web.json_response({**bridge.state(), "schedule": scheduler.snapshot()})

    async def get_schedule(request):
        return web.json_response(scheduler.snapshot())

    async def save_schedule(request):
        return web.json_response(await scheduler.save(await request.json()))

    async def disable_schedule(request):
        await scheduler.disable()
        return web.json_response(scheduler.snapshot())

    async def connect(request):
        rate_limit(request, "mercedes-login", 4, 300)
        if bridge.operation_lock.locked() or bridge.command_lock.locked():
            raise BridgeError("An operation is already in progress. Please wait.")
        data = await request.json()
        email, password_value = data.get("email", ""), data.get("password", "")
        region = data.get("region", "Europe")
        if region not in ("Europe", "North America", "Asia-Pacific"):
            raise BridgeError("Select a supported region.")
        if not isinstance(email, str) or "@" not in email or not isinstance(password_value, str) or not password_value:
            raise BridgeError("Enter your Mercedes account email and password.")
        await bridge.connect(email.strip(), password_value, region)
        return web.json_response({**bridge.state(), "schedule": scheduler.snapshot()})

    async def disconnect(request):
        await scheduler.disable()
        await bridge.disconnect()
        return web.json_response({**bridge.state(), "schedule": scheduler.snapshot()})

    async def command(request):
        data = await request.json()
        name, pin = data.get("command"), data.get("pin", "")
        if not isinstance(name, str) or not isinstance(pin, str):
            raise BridgeError("Invalid command or security PIN.")
        if name == "charge_limit" and scheduler.config["enabled"] and scheduler.config["vin"] == request.match_info["vin"]:
            raise BridgeError("Turn off the charge schedule before setting a manual limit.")
        result = await bridge.command(request.match_info["vin"], name, pin, data.get("maxSoc"))
        return web.json_response(result, status=202)

    async def frontend(request):
        dist = ROOT / "dist"
        path = (dist / request.match_info.get("path", "")).resolve()
        if not path.is_relative_to(dist.resolve()):
            raise web.HTTPNotFound()
        if path.is_file():
            return web.FileResponse(path)
        if (dist / "index.html").exists() and not request.path.startswith("/api/"):
            return web.FileResponse(dist / "index.html")
        raise web.HTTPNotFound(text="Build the React app with npm run build first.")

    async def lifecycle(app):
        restore_task = asyncio.create_task(bridge.restore())
        schedule_task = asyncio.create_task(scheduler.run())
        yield
        schedule_task.cancel()
        await asyncio.gather(schedule_task, return_exceptions=True)
        restore_task.cancel()
        await asyncio.gather(restore_task, return_exceptions=True)
        await bridge.stop()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/session", session_status)
    app.router.add_post("/api/session", session_login)
    app.router.add_delete("/api/session", session_logout)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/schedule", get_schedule)
    app.router.add_put("/api/schedule", save_schedule)
    app.router.add_delete("/api/schedule", disable_schedule)
    app.router.add_post("/api/account", connect)
    app.router.add_delete("/api/account", disconnect)
    app.router.add_post("/api/vehicles/{vin}/commands", command)
    app.router.add_get("/{path:.*}", frontend)
    return app


if __name__ == "__main__":
    # Upstream auth errors can contain response bodies. Keep them out of routine logs.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("server.mbapi").setLevel(logging.CRITICAL)
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("APP_PASSWORD"):
        raise SystemExit("Set APP_PASSWORD before binding to a network interface.")
    web.run_app(create_app(), host=host, port=int(os.environ.get("PORT", "8080")), access_log=None)
