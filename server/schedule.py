"""Persistent daily charge-limit scheduling, independent of browser sessions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, time as clock_time, timedelta, timezone
import re
import time
from zoneinfo import ZoneInfo

from .bridge import BridgeError, charge_limit_options
from .runtime import TokenStore

DEFAULT_ENTRIES = [{"time": "05:30", "maxSoc": 50}, {"time": "23:30", "maxSoc": 80}]


def normalize_config(data):
    if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
        raise BridgeError("Choose whether the schedule is enabled.")
    vin = data.get("vin")
    if not isinstance(vin, str) or not vin:
        raise BridgeError("Select the vehicle for this schedule.")
    entries = data.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
        raise BridgeError("Add between one and twelve daily periods.")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BridgeError("Invalid schedule period.")
        start, target = entry.get("time"), entry.get("maxSoc")
        if not isinstance(start, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start):
            raise BridgeError("Enter each start time in 24-hour HH:MM format.")
        if type(target) is not int or target not in range(50, 101, 10):
            raise BridgeError("Choose a charge limit from 50% to 100%, in 10% steps.")
        normalized.append({"time": start, "maxSoc": target})
    if len({entry["time"] for entry in normalized}) != len(normalized):
        raise BridgeError("Each period must have a different start time.")
    return {"enabled": data["enabled"], "vin": vin, "entries": sorted(normalized, key=lambda entry: entry["time"])}


def local_transition(day, start, zone):
    """Use the first occurrence of repeated times; roll skipped times to the gap's end."""
    local = datetime.combine(day, clock_time.fromisoformat(start))
    for _ in range(181):
        candidate = local.replace(tzinfo=zone, fold=0)
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local:
            return candidate.astimezone(timezone.utc)
        local += timedelta(minutes=1)
    raise ValueError("Could not resolve local schedule time")


def schedule_window(entries, now, zone):
    now = now.astimezone(timezone.utc)
    today = now.astimezone(zone).date()
    transitions = sorted(
        (local_transition(today + timedelta(days=offset), entry["time"], zone), entry["time"], entry["maxSoc"])
        for offset in (-1, 0, 1, 2)
        for entry in entries
    )
    active = max(item for item in transitions if item[0] <= now)
    following = min(item for item in transitions if item[0] > now)
    return {
        "target": active[2], "periodKey": f"{active[0].isoformat()}/{active[1]}",
        "periodStartedAt": active[0].isoformat(),
        "nextChangeAt": following[0].isoformat(), "nextTarget": following[2],
    }


def reported_limit(car):
    for name in ("max_soc", "maxSoc"):
        value = getattr(car.electric, name, None)
        if value and value.retrievalstatus == "VALID":
            try:
                return int(value.value)
            except (TypeError, ValueError):
                continue
    return None


class ChargeScheduler:
    def __init__(self, bridge, *, timezone_name="Europe/London"):
        self.bridge = bridge
        self.zone = ZoneInfo(timezone_name)
        self.store = TokenStore(bridge.directory, "schedule.enc")
        self.config = {"enabled": False, "vin": "", "entries": deepcopy(DEFAULT_ENTRIES)}
        self.status = "disabled"
        self.message = "Schedule is off."
        try:
            stored = self.store.load()
            if stored:
                self.config = normalize_config(stored)
        except Exception:
            # Fail closed: a corrupt configuration must never activate commands.
            self.message = "Saved schedule could not be read. Configure it again."
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.period_key = None
        self.attempts = 0
        self.next_try = 0
        self.last_sent_at = None

    def snapshot(self, now=None):
        window = schedule_window(self.config["entries"], now or datetime.now(timezone.utc), self.zone)
        return {
            "config": deepcopy(self.config), "timezone": self.zone.key,
            "status": self.status, "message": self.message,
            "target": window["target"], "nextChangeAt": window["nextChangeAt"],
            "nextTarget": window["nextTarget"], "lastSentAt": self.last_sent_at,
            "attempts": self.attempts,
        }

    async def save(self, data):
        config = normalize_config(data)
        async with self.lock:
            client = self.bridge.client
            car = client.cars.get(config["vin"]) if client else None
            if car is None:
                raise BridgeError("Connect the scheduled vehicle before saving its schedule.")
            allowed = charge_limit_options(car)
            if any(entry["maxSoc"] not in allowed for entry in config["entries"]):
                raise BridgeError("One or more percentages are not supported by this vehicle.")
            # Persist before activating; a failed write leaves the previous schedule intact.
            self.store.save(config)
            self.config = config
            self.period_key = None
            self.attempts = 0
            self.next_try = 0
            self.status = "waiting" if config["enabled"] else "disabled"
            self.message = "Applying the current period when the vehicle is ready." if config["enabled"] else "Schedule is off."
            self.wake.set()
        return self.snapshot()

    async def disable(self):
        async with self.lock:
            config = {**self.config, "enabled": False}
            if config["vin"]:
                self.store.save(config)
            self.config = config
            self.status, self.message = "disabled", "Schedule is off."
            self.wake.set()

    async def tick(self, *, now=None, monotonic=None):
        now = now or datetime.now(timezone.utc)
        monotonic = time.monotonic() if monotonic is None else monotonic
        async with self.lock:
            if not self.config["enabled"]:
                self.status = "disabled"
                return
            window = schedule_window(self.config["entries"], now, self.zone)
            if self.period_key != window["periodKey"]:
                self.period_key = window["periodKey"]
                self.attempts, self.next_try = 0, 0
            client = self.bridge.client
            car = client.cars.get(self.config["vin"]) if client else None
            if not car or self.bridge.operation_lock.locked():
                self.status, self.message = "waiting", "Waiting for the scheduled vehicle to connect."
                return
            if self.bridge.state().get("accountBlocked"):
                self.status, self.message = "error", "Mercedes has rate limited the account. Scheduled commands are paused."
                return
            target = window["target"]
            if target not in charge_limit_options(car):
                self.status, self.message = "error", "The vehicle no longer advertises this charge limit. Check the schedule."
                return
            current = reported_limit(car)
            if current is None:
                self.status, self.message = "waiting", "Waiting for reported charge-limit data."
                return
            if current == target:
                self.status, self.message = "active", f"Last reported vehicle limit matches the scheduled {target}% target."
                self.attempts, self.next_try = 0, 0
                return
            if monotonic < self.next_try:
                return
            if self.attempts >= 3:
                self.status, self.message = "error", "Charge limit was not confirmed after three attempts. Check the car and save the schedule to retry."
                return
            if self.bridge.command_lock.locked():
                self.status, self.message = "waiting", "Waiting for another vehicle command to finish."
                return
            self.attempts += 1
            self.next_try = monotonic + 120 * (2 ** (self.attempts - 1))
            try:
                await self.bridge.command(self.config["vin"], "charge_limit", max_soc=target)
                self.last_sent_at = now.isoformat()
                self.status, self.message = "pending", f"Requested {target}%. Waiting for vehicle telemetry to confirm it."
            except Exception:
                self.status, self.message = "waiting", "Could not apply the charge limit. Will retry with a delay."

    async def run(self):
        while True:
            # Clear before tick so a save during the wait wakes the loop immediately.
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status, self.message = "error", "Scheduler could not check the vehicle. Retrying shortly."
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
