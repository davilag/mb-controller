"""Small runtime adapter for the upstream protocol core; no Home Assistant dependency."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from cryptography.fernet import Fernet

STATE_UNKNOWN = "unknown"
EVENT_HOMEASSISTANT_STOP = "stop"


class HomeAssistantError(Exception):
    pass


class ConfigEntryAuthFailed(HomeAssistantError):
    pass


class ServiceValidationError(HomeAssistantError):
    pass


def callback(fn):
    return fn


class TokenStore:
    """Encrypt refresh tokens at rest; keep the key in the private data volume."""

    def __init__(self, directory: Path, filename="account.enc"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / filename
        key = directory / "encryption.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())

    def load(self):
        return json.loads(self.cipher.decrypt(self.path.read_bytes())) if self.path.exists() else {}

    def save(self, data):
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(self.cipher.encrypt(json.dumps(data).encode()))
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


@dataclass
class ConfigEntry:
    data: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    entry_id: str = "local"


class HomeAssistant:
    """Only the scheduling, session, and storage hooks used by the vendored core."""

    def __init__(self, directory: Path, store: TokenStore):
        self.data = {}
        self.sessions = []
        self.tasks = set()
        self.store = store
        self.reload_requested = False
        self.config = SimpleNamespace(path=lambda *parts: str(directory.joinpath(*parts)))
        self.config_entries = SimpleNamespace(
            async_update_entry=self.update_entry,
            async_schedule_reload=self.schedule_reload,
        )
        self.bus = SimpleNamespace(async_listen_once=lambda *args: lambda: None)

    def schedule_reload(self, entry_id):
        self.reload_requested = True

    def update_entry(self, entry, *, data):
        entry.data = data
        self.store.save(data)

    def async_create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def async_add_executor_job(self, fn, *args):
        return self.async_create_task(asyncio.to_thread(fn, *args))

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await asyncio.gather(*(session.close() for session in self.sessions))


def async_create_clientsession(runtime, verify_ssl=True, **kwargs):
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=verify_ssl),
        timeout=aiohttp.ClientTimeout(total=45), **kwargs,
    )
    runtime.sessions.append(session)
    return session


def async_get_clientsession(runtime, verify_ssl=True):
    if not runtime.sessions or runtime.sessions[0].closed:
        return async_create_clientsession(runtime, verify_ssl)
    return runtime.sessions[0]


def async_call_later(runtime, delay, fn):
    def invoke():
        result = fn(datetime.now(timezone.utc))
        if inspect.isawaitable(result):
            runtime.async_create_task(result)
    handle = asyncio.get_running_loop().call_later(delay, invoke)
    return handle.cancel


async def get_system_info(runtime):
    return {"os_version": platform.platform()}


def parse_datetime(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


system_info = SimpleNamespace(async_get_system_info=get_system_info)
dt_util = SimpleNamespace(
    parse_datetime=parse_datetime,
    DEFAULT_TIME_ZONE=ZoneInfo(os.environ.get("TZ", "Europe/London")),
)
