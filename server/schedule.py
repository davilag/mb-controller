"""Persistent, at-most-once charge changes for a single selected night.

Reading state, saving, cancelling and restoring never contact Mercedes.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, time as clock_time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from .bridge import BridgeError
from .runtime import TokenStore

DEFAULT_ENTRIES = [{"time": "23:30", "maxSoc": 80}, {"time": "05:30", "maxSoc": 50}]
TERMINAL = {"confirmed", "already_set", "failed", "missed", "interrupted", "cancelled"}


def normalize_config(data):
    if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
        raise BridgeError("Choose whether to schedule this night.")
    if data.get("mode") != "oneNight":
        raise BridgeError("Recurring schedules are no longer supported. Choose one night.")
    vin = data.get("vin")
    if not isinstance(vin, str) or (not vin and data["enabled"]):
        raise BridgeError("Select your vehicle.")
    try:
        day = date.fromisoformat(data.get("date", ""))
    except (TypeError, ValueError):
        raise BridgeError("Choose the date your charging night starts.") from None
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) != 2:
        raise BridgeError("Choose an evening change and a morning change.")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BridgeError("Invalid scheduled change.")
        start, target = entry.get("time"), entry.get("maxSoc")
        if not isinstance(start, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start):
            raise BridgeError("Enter each time in 24-hour HH:MM format.")
        if type(target) is not int or target not in range(50, 101, 10):
            raise BridgeError("Choose a limit from 50% to 100%, in 10% steps.")
        normalized.append({"time": start, "maxSoc": target})
    if normalized[0]["time"] == normalized[1]["time"]:
        raise BridgeError("The two changes must have different times.")
    return {"mode": "oneNight", "enabled": data["enabled"], "vin": vin,
            "date": day.isoformat(), "entries": normalized}


def local_transition(day, start, zone):
    """First occurrence when clocks repeat; skipped times roll to gap end."""
    local = datetime.combine(day, clock_time.fromisoformat(start))
    for _ in range(181):
        candidate = local.replace(tzinfo=zone, fold=0)
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local:
            return candidate.astimezone(timezone.utc)
        local += timedelta(minutes=1)
    raise ValueError("Could not resolve local schedule time")


def make_jobs(config, zone):
    first, second = config["entries"]
    day = date.fromisoformat(config["date"])
    morning = day + timedelta(days=second["time"] < first["time"])
    times = [local_transition(day, first["time"], zone), local_transition(morning, second["time"], zone)]
    if times[1] <= times[0]:
        raise BridgeError("The morning change must follow the evening change.")
    return [{"at": at.isoformat(), "target": entry["maxSoc"], "status": "scheduled",
             "sentAt": None, "confirmedAt": None, "message": "Waiting for its scheduled time."}
            for at, entry in zip(times, config["entries"])]


def reported_limit(car):
    electric = getattr(car, "electric", None)
    for name in ("max_soc", "maxSoc"):
        value = getattr(electric, name, None)
        if value and value.retrievalstatus == "VALID":
            try:
                return int(value.value)
            except (TypeError, ValueError):
                pass
    return None


class ChargeScheduler:
    def __init__(self, bridge, *, timezone_name="Europe/London"):
        self.bridge = bridge
        self.zone = ZoneInfo(timezone_name)
        self.store = TokenStore(bridge.directory, "schedule.enc")
        self.config = {"mode": "oneNight", "enabled": False, "vin": "",
                       "date": datetime.now(self.zone).date().isoformat(), "entries": deepcopy(DEFAULT_ENTRIES)}
        self.jobs = []
        self.status, self.message = "disabled", "Quiet. No night scheduled."
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        try:
            stored = self.store.load()
            if stored.get("version") == 2:
                self.config = normalize_config(stored["config"])
                expected = make_jobs(self.config, self.zone)
                jobs = stored["jobs"]
                if (len(jobs) != 2 and (self.config["enabled"] or jobs)) or any(j["at"] != e["at"] or j["target"] != e["target"] or j["status"] not in TERMINAL | {"scheduled", "running"} for j, e in zip(jobs, expected)):
                    raise ValueError("Invalid saved jobs")
                self.jobs = jobs
                for job in self.jobs:
                    if job["status"] == "running":
                        job.update(status="interrupted", message="Server restarted during this change. It will not be replayed; check the official app.")
                self.refresh_status()
                self.persist()
            elif stored:
                self.message = "Previous daily schedule retired. Choose a night to schedule again."
        except Exception:
            self.config["enabled"] = False
            self.jobs = []
            self.status, self.message = "error", "Saved schedule could not be read. No connections will be made."

    def persist(self):
        self.store.save({"version": 2, "config": self.config, "jobs": self.jobs})

    def refresh_status(self):
        if not self.config["enabled"]:
            if self.jobs and any(j["status"] == "cancelled" for j in self.jobs) and not any(j["status"] in {"failed", "missed", "interrupted"} for j in self.jobs):
                self.status, self.message = "disabled", "Night cancelled. Quiet; no further connections scheduled."
            elif self.jobs and all(j["status"] in TERMINAL for j in self.jobs):
                self.status = "completed" if all(j["status"] in {"confirmed", "already_set"} for j in self.jobs) else "error"
                self.message = "Night finished. No more connections are scheduled." if self.status == "completed" else "Night ended with an unconfirmed change. No more connections are scheduled."
            else:
                self.status, self.message = "disabled", "Quiet. No night scheduled."
            return
        if all(j["status"] in TERMINAL for j in self.jobs):
            self.config["enabled"] = False
            return self.refresh_status()
        latest = next((j for j in reversed(self.jobs) if j["status"] != "scheduled"), None)
        self.status = "active" if latest and latest["status"] in {"confirmed", "already_set"} else "error" if latest and latest["status"] in {"failed", "missed", "interrupted"} else "waiting"
        self.message = latest["message"] if latest else "Armed for one night. Quiet until the first change."

    def snapshot(self, now=None):
        pending = next((j for j in self.jobs if j["status"] == "scheduled"), None) if self.config["enabled"] else None
        latest = next((j for j in reversed(self.jobs) if j["status"] != "scheduled"), None)
        return {"config": deepcopy(self.config), "timezone": self.zone.key, "status": self.status,
                "message": self.message, "jobs": deepcopy(self.jobs),
                "target": latest["target"] if latest else None,
                "nextChangeAt": pending["at"] if pending else None,
                "nextTarget": pending["target"] if pending else None,
                "lastSentAt": latest.get("sentAt") if latest else None,
                "attempts": int(bool(latest and latest["status"] != "scheduled"))}

    async def save(self, data, *, now=None):
        config = normalize_config(data)
        jobs = make_jobs(config, self.zone)
        now = now or datetime.now(timezone.utc)
        if config["enabled"] and datetime.fromisoformat(jobs[0]["at"]) <= now:
            raise BridgeError("Choose a future start time. Past changes will not be replayed.")
        async with self.lock:
            state = self.bridge.state()
            cooldown = state.get("cooldownUntil")
            if config["enabled"] and cooldown and datetime.fromisoformat(jobs[0]["at"]) < datetime.fromisoformat(cooldown):
                raise BridgeError("This night starts during the Mercedes safety cooldown. Choose a night after " + cooldown + ".")
            vehicles = state.get("vehicles", [])
            car = next((v for v in vehicles if v["vin"] == config["vin"]), None)
            if not car:
                raise BridgeError("Set up your vehicle before scheduling a night.")
            options = next((c.get("chargeLimits", []) for c in car["commands"] if c["id"] == "charge_limit"), [])
            if any(e["maxSoc"] not in options for e in config["entries"]):
                raise BridgeError("One or more limits are not supported by your vehicle.")
            # Atomic save first; failures leave the previous night untouched.
            self.store.save({"version": 2, "config": config, "jobs": jobs if config["enabled"] else []})
            self.config, self.jobs = config, jobs if config["enabled"] else []
            self.refresh_status()
            self.wake.set()
        return self.snapshot()

    async def disable(self):
        async with self.lock:
            config = {**self.config, "enabled": False}
            jobs = deepcopy(self.jobs)
            for job in jobs:
                if job["status"] == "scheduled":
                    job.update(status="cancelled", message="Cancelled. No connection made.")
            self.store.save({"version": 2, "config": config, "jobs": jobs})
            self.config, self.jobs = config, jobs
            self.status, self.message = "disabled", "Night cancelled. Quiet; no further connections scheduled."
            self.wake.set()

    async def tick(self, *, now=None, monotonic=None):
        now = now or datetime.now(timezone.utc)
        async with self.lock:
            if not self.config["enabled"]:
                return
            for job in self.jobs:
                if job["status"] != "scheduled" or datetime.fromisoformat(job["at"]) > now:
                    continue
                # A small timer grace is not permission to replay an old night's changes.
                if (now - datetime.fromisoformat(job["at"])).total_seconds() > 90:
                    job.update(status="missed", message="Server missed this change. No late command or retry was sent.")
                else:
                    job.update(status="running", message="Connecting for this change only.")
                    self.status, self.message = "pending", job["message"]
                    self.persist()  # At-most-once, including crashes after send.
                    try:
                        result = await self.bridge.scheduled_charge(self.config["vin"], job["target"])
                        job.update(result)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        job.update(status="failed", message=str(exc) if isinstance(exc, BridgeError) else "This change failed. No automatic retry will be made.")
                self.refresh_status()
                self.persist()
            self.refresh_status()

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed persistence write must not result in a network retry.
                self.config["enabled"] = False
                self.status, self.message = "error", "Scheduler stopped safely. Check the saved night before scheduling again."
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
