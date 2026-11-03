from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import asyncio

from aiohttp import BasicAuth
from aiohttp.test_utils import TestClient, TestServer
import pytest

from server.bridge import BridgeError
from server.mbapi.car import Car, CarAttribute, Electric
from server.schedule import ChargeScheduler, DEFAULT_ENTRIES, normalize_config, schedule_window

VIN = "WDD00000000000001"
LONDON = ZoneInfo("Europe/London")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def config(enabled=True, entries=None):
    return {"enabled": enabled, "vin": VIN, "entries": deepcopy(entries if entries is not None else DEFAULT_ENTRIES)}


class FakeBridge:
    """Never connects to Mercedes or transmits real vehicle commands."""
    def __init__(self, directory):
        self.directory = directory
        self.online = True
        car = Car(VIN)
        car.features = {"BATTERY_MAX_SOC_CONFIGURE": True}
        car.electric = Electric()
        car.electric.max_soc = CarAttribute(80, "VALID", 1)
        self.client = SimpleNamespace(cars={VIN: car})
        self.operation_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.command = AsyncMock(return_value={"state": "sent", "requestId": "test-only"})

    def state(self):
        return {"connected": self.online}

    async def restore(self):
        pass

    async def stop(self):
        pass


@pytest.mark.parametrize("now,target,next_time,next_target", [
    ("2026-11-02T00:00Z", 80, "2026-11-02T05:30Z", 50),
    ("2026-11-02T05:29:59Z", 80, "2026-11-02T05:30Z", 50),
    ("2026-11-02T05:30Z", 50, "2026-11-02T23:30Z", 80),
    ("2026-11-02T23:29:59Z", 50, "2026-11-02T23:30Z", 80),
    ("2026-11-02T23:30Z", 80, "2026-11-03T05:30Z", 50),
    ("2026-11-02T23:59:59Z", 80, "2026-11-03T05:30Z", 50),
    ("2026-07-02T04:30Z", 50, "2026-07-02T22:30Z", 80),
])
def test_daily_boundaries_midnight_and_bst(now, target, next_time, next_target):
    window = schedule_window(DEFAULT_ENTRIES, instant(now), LONDON)
    assert window["target"] == target
    assert instant(window["nextChangeAt"]) == instant(next_time)
    assert window["nextTarget"] == next_target


def test_dst_skipped_and_repeated_local_times():
    entries = [{"time": "00:00", "maxSoc": 50}, {"time": "01:30", "maxSoc": 80}, {"time": "03:00", "maxSoc": 50}]
    before = schedule_window(entries, instant("2026-03-29T00:59Z"), LONDON)
    assert before["target"] == 50
    assert instant(before["nextChangeAt"]) == instant("2026-03-29T01:00Z")
    assert schedule_window(entries, instant("2026-03-29T01:00Z"), LONDON)["target"] == 80
    # After the clocks repeat 01:00, don't undo the 01:30 change already made.
    assert schedule_window(entries, instant("2026-10-25T00:30Z"), LONDON)["target"] == 80
    assert schedule_window(entries, instant("2026-10-25T01:10Z"), LONDON)["target"] == 80


@pytest.mark.parametrize("entries", [
    [], [{"time": "24:00", "maxSoc": 50}], [{"time": "5:30", "maxSoc": 50}],
    [{"time": "05:60", "maxSoc": 50}], [{"time": "05:30", "maxSoc": 55}],
    [{"time": "05:30", "maxSoc": True}], [{"time": "05:30", "maxSoc": "80"}],
    [{"time": "05:30", "maxSoc": 50}, {"time": "05:30", "maxSoc": 80}],
])
def test_reject_invalid_or_duplicate_periods(entries):
    with pytest.raises(BridgeError):
        normalize_config(config(entries=entries))


@pytest.mark.asyncio
async def test_scheduler_applies_current_period_then_waits_for_confirmation(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    assert not scheduler.config["enabled"]
    await scheduler.save(config())
    morning = instant("2026-11-02T06:00Z")
    await scheduler.tick(now=morning, monotonic=0)
    bridge.command.assert_awaited_once_with(VIN, "charge_limit", max_soc=50)
    assert scheduler.status == "pending"
    await scheduler.tick(now=morning, monotonic=20)
    assert bridge.command.await_count == 1
    bridge.client.cars[VIN].electric.max_soc.value = 50
    await scheduler.tick(now=morning, monotonic=40)
    assert scheduler.status == "active"
    await scheduler.tick(now=instant("2026-11-02T23:30Z"), monotonic=60)
    assert bridge.command.await_count == 2
    bridge.command.assert_awaited_with(VIN, "charge_limit", max_soc=80)
    await scheduler.disable()
    await scheduler.tick(now=morning, monotonic=1000)
    assert bridge.command.await_count == 2
    assert scheduler.status == "disabled"


@pytest.mark.asyncio
async def test_persistence_and_reconnection_catch_up(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config())
    assert VIN.encode() not in scheduler.store.path.read_bytes()
    restored = ChargeScheduler(bridge)
    assert restored.config == config()
    bridge.online = False
    await restored.tick(now=instant("2026-11-02T06:00Z"), monotonic=0)
    bridge.command.assert_not_awaited()
    assert restored.status == "waiting"
    bridge.online = True
    await restored.tick(now=instant("2026-11-02T06:00Z"), monotonic=20)
    bridge.command.assert_awaited_once_with(VIN, "charge_limit", max_soc=50)
    await restored.disable()
    assert not ChargeScheduler(bridge).config["enabled"]


@pytest.mark.asyncio
async def test_retries_are_delayed_and_bounded(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config())
    now = instant("2026-11-02T06:00Z")
    for tick, expected_count in [(0, 1), (119, 1), (120, 2), (359, 2), (360, 3), (839, 3), (840, 3), (1000, 3)]:
        await scheduler.tick(now=now, monotonic=tick)
        assert bridge.command.await_count == expected_count
    assert scheduler.status == "error"
    await scheduler.save(config())
    await scheduler.tick(now=now, monotonic=1001)
    assert bridge.command.await_count == 4


@pytest.mark.asyncio
async def test_missing_telemetry_and_unsupported_targets_never_send(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config())
    car = bridge.client.cars[VIN]
    car.electric.max_soc.retrievalstatus = "NOT_RECEIVED"
    await scheduler.tick(now=instant("2026-11-02T06:00Z"), monotonic=0)
    assert scheduler.status == "waiting"
    bridge.command.assert_not_awaited()
    car.features = {}
    await scheduler.tick(now=instant("2026-11-02T06:00Z"), monotonic=20)
    assert scheduler.status == "error"
    bridge.command.assert_not_awaited()
    with pytest.raises(BridgeError, match="not supported"):
        await scheduler.save(config())
    await scheduler.disable()  # Turning off remains possible when capabilities disappear.
    assert scheduler.status == "disabled"


@pytest.mark.asyncio
async def test_schedule_http_authorization_save_and_manual_override_guard(tmp_path, monkeypatch):
    import server.app as app_module
    bridge = FakeBridge(tmp_path)
    monkeypatch.setattr(app_module, "Bridge", lambda directory: bridge)
    http = TestClient(TestServer(app_module.create_app(data_dir=tmp_path, password="test-only")))
    await http.start_server()
    try:
        assert (await http.get("/api/schedule")).status == 401
        assert (await http.put("/api/schedule", json=config())).status == 401
        http.session.headers["Authorization"] = BasicAuth("admin", "test-only").encode()
        assert (await http.put("/api/schedule", json=config(), headers={"Origin": "https://untrusted.example"})).status == 403
        # Fixed all-day target matches fake telemetry, so saving never sends a command.
        saved = await http.put("/api/schedule", json=config(entries=[{"time": "00:00", "maxSoc": 80}]))
        assert saved.status == 200
        assert (await saved.json())["config"]["enabled"]
        manual = await http.post(f"/api/vehicles/{VIN}/commands", json={"command": "charge_limit", "maxSoc": 70})
        assert manual.status == 400
        assert "Turn off" in (await manual.json())["error"]
        bridge.command.assert_not_awaited()
        assert (await http.delete("/api/schedule")).status == 200
        manual = await http.post(f"/api/vehicles/{VIN}/commands", json={"command": "charge_limit", "maxSoc": 70})
        assert manual.status == 202
        bridge.command.assert_awaited_once_with(VIN, "charge_limit", "", 70)
    finally:
        await http.close()
