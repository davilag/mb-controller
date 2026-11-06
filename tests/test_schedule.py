"""No live Mercedes requests: one-night persistence, timing and fail-closed execution."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aiohttp import BasicAuth
from aiohttp.test_utils import TestClient, TestServer
import pytest

from server.bridge import BridgeError, Bridge
from server.runtime import TokenStore
from server.schedule import ChargeScheduler, DEFAULT_ENTRIES, normalize_config, make_jobs, local_transition

VIN = "WDD00000000000001"
LONDON = ZoneInfo("Europe/London")
def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
def config(**updates):
    return {"mode": "oneNight", "enabled": True, "date": "2026-11-06", "vin": VIN,
            "entries": deepcopy(DEFAULT_ENTRIES), **updates}
BEFORE = instant("2026-11-06T16:00Z")

class FakeBridge:
    def __init__(self, directory):
        self.directory = directory
        self.scheduled_charge = AsyncMock(return_value={"status": "confirmed", "sentAt": "2026-11-06T23:30:01+00:00", "confirmedAt": "2026-11-06T23:30:04+00:00", "message": "Vehicle confirmed 80%."})
        self.restore, self.stop = AsyncMock(), AsyncMock()
        self.cached = [{"vin": VIN, "commands": [{"id": "charge_limit", "chargeLimits": [50, 60, 70, 80, 90, 100]}]}]
    def state(self):
        return {"vehicles": self.cached}

@pytest.mark.parametrize("data", [config(mode="daily"), config(date="not-a-date"), config(entries=[]),
    config(entries=[{"time": "23:30", "maxSoc": 80}]),
    config(entries=[{"time": "24:00", "maxSoc": 80}, {"time": "05:30", "maxSoc": 50}]),
    config(entries=[{"time": "23:30", "maxSoc": True}, {"time": "05:30", "maxSoc": 50}]),
    config(entries=[{"time": "23:30", "maxSoc": 55}, {"time": "05:30", "maxSoc": 50}]),
    config(entries=[{"time": "23:30", "maxSoc": 80}, {"time": "23:30", "maxSoc": 50}])])
def test_reject_recurring_or_invalid_config(data):
    with pytest.raises(BridgeError): normalize_config(data)

@pytest.mark.parametrize("day,expected", [("2026-11-06", ["2026-11-06T23:30Z", "2026-11-07T05:30Z"]),
    ("2026-07-06", ["2026-07-06T22:30Z", "2026-07-07T04:30Z"])])
def test_absolute_dates_overnight_and_bst(day, expected):
    jobs = make_jobs(config(date=day), LONDON)
    assert [instant(j["at"]) for j in jobs] == [instant(v) for v in expected]

def test_dst_repeated_and_skipped_times():
    from datetime import date
    assert local_transition(date(2026, 3, 29), "01:30", LONDON) == instant("2026-03-29T01:00Z")
    assert local_transition(date(2026, 10, 25), "01:30", LONDON) == instant("2026-10-25T00:30Z")

@pytest.mark.asyncio
async def test_save_read_restart_and_cancel_are_network_silent(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config(), now=BEFORE)
    for now in [BEFORE, instant("2026-11-06T23:29:59Z")]:
        await scheduler.tick(now=now)
        scheduler.snapshot()
    restored = ChargeScheduler(bridge)
    assert restored.config == config()
    assert VIN.encode() not in restored.store.path.read_bytes()
    await restored.disable()
    await restored.tick(now=instant("2026-11-06T23:30Z"))
    bridge.scheduled_charge.assert_not_awaited()
    assert not ChargeScheduler(bridge).config["enabled"]

@pytest.mark.asyncio
async def test_exactly_two_connections_then_quiet_forever(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config(), now=BEFORE)
    await scheduler.tick(now=instant("2026-11-06T23:30Z"))
    bridge.scheduled_charge.assert_awaited_once_with(VIN, 80)
    assert scheduler.jobs[0]["status"] == "confirmed"
    for now in ["2026-11-06T23:30:20Z", "2026-11-07T01:00Z", "2026-11-07T05:29:59Z"]:
        await scheduler.tick(now=instant(now))
    assert bridge.scheduled_charge.await_count == 1
    bridge.scheduled_charge.return_value = {"status": "confirmed", "message": "Vehicle confirmed 50%."}
    await scheduler.tick(now=instant("2026-11-07T05:30:01Z"))
    bridge.scheduled_charge.assert_awaited_with(VIN, 50)
    assert bridge.scheduled_charge.await_count == 2
    assert scheduler.status == "completed" and not scheduler.config["enabled"]
    restored = ChargeScheduler(bridge)
    for now in ["2026-11-07T23:30Z", "2026-11-08T05:30Z", "2026-12-06T23:30Z"]:
        await restored.tick(now=instant(now))
    assert bridge.scheduled_charge.await_count == 2

@pytest.mark.asyncio
async def test_failed_attempt_never_retries_and_does_not_replay_on_restart(tmp_path):
    bridge = FakeBridge(tmp_path)
    bridge.scheduled_charge.return_value = {"status": "failed", "message": "No confirmation"}
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config(), now=BEFORE)
    await scheduler.tick(now=instant("2026-11-06T23:30Z"))
    scheduler = ChargeScheduler(bridge)
    for second in [1, 10, 30, 60]:
        await scheduler.tick(now=instant(f"2026-11-06T23:3{second//60}:{second%60:02}Z"))
    bridge.scheduled_charge.assert_awaited_once_with(VIN, 80)
    assert scheduler.jobs[0]["status"] == "failed"

@pytest.mark.asyncio
async def test_old_daily_schedule_retired_locally(tmp_path):
    old = {"enabled": True, "vin": VIN, "entries": [{"time": "05:30", "maxSoc": 50}, {"time": "23:30", "maxSoc": 80}]}
    TokenStore(tmp_path, "schedule.enc").save(old)
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    assert not scheduler.config["enabled"]
    await scheduler.tick(now=instant("2026-11-06T23:30Z"))
    bridge.scheduled_charge.assert_not_awaited()
    actual = Bridge(tmp_path)
    await actual.restore()
    assert actual.client is None
    assert actual.state()["vehicles"][0]["vin"] == VIN

@pytest.mark.asyncio
async def test_crash_after_persisting_running_is_not_replayed(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config(), now=BEFORE)
    scheduler.jobs[0]["status"] = "running"
    scheduler.persist()
    restarted = ChargeScheduler(bridge)
    assert restarted.jobs[0]["status"] == "interrupted"
    await restarted.tick(now=instant("2026-11-06T23:30:02Z"))
    bridge.scheduled_charge.assert_not_awaited()

@pytest.mark.asyncio
async def test_missing_startup_deadlines_are_skipped_not_caught_up(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config(), now=BEFORE)
    await scheduler.tick(now=instant("2026-11-07T06:00Z"))
    assert [j["status"] for j in scheduler.jobs] == ["missed", "missed"]
    assert not scheduler.config["enabled"]
    bridge.scheduled_charge.assert_not_awaited()

@pytest.mark.asyncio
async def test_no_past_save_and_no_unsupported_limits(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    with pytest.raises(BridgeError, match="future"):
        await scheduler.save(config(), now=instant("2026-11-07T06:00Z"))
    bridge.cached[0]["commands"][0]["chargeLimits"] = [50]
    with pytest.raises(BridgeError, match="not supported"):
        await scheduler.save(config(), now=BEFORE)
    bridge.scheduled_charge.assert_not_awaited()

@pytest.mark.asyncio
async def test_persistence_failure_cannot_send_and_keeps_previous_plan(tmp_path, monkeypatch):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.save(config(), now=BEFORE)
    original = deepcopy(scheduler.config)
    def fail(_): raise OSError("read only")
    monkeypatch.setattr(scheduler.store, "save", fail)
    with pytest.raises(OSError): await scheduler.save(config(date="2026-11-08"), now=BEFORE)
    assert scheduler.config == original
    with pytest.raises(OSError): await scheduler.tick(now=instant("2026-11-06T23:30Z"))
    bridge.scheduled_charge.assert_not_awaited()

@pytest.mark.asyncio
async def test_http_reads_and_schedule_save_do_not_connect(tmp_path, monkeypatch):
    import server.app as app_module
    bridge = FakeBridge(tmp_path)
    monkeypatch.setattr(app_module, "Bridge", lambda directory: bridge)
    http = TestClient(TestServer(app_module.create_app(data_dir=tmp_path, password="test-only")))
    await http.start_server()
    try:
        assert (await http.get("/api/schedule")).status == 401
        http.session.headers["Authorization"] = BasicAuth("admin", "test-only").encode()
        future = config(date="2099-11-06")
        assert (await http.put("/api/schedule", json=future, headers={"Origin": "https://untrusted.example"})).status == 403
        saved = await http.put("/api/schedule", json=future)
        assert saved.status == 200
        for _ in range(3):
            assert (await http.get("/api/state")).status == 200
        manual = await http.post(f"/api/vehicles/{VIN}/commands", json={"command": "charge_limit", "maxSoc": 80})
        assert manual.status == 400 and 'quiet mode' in (await manual.json())["error"]
        assert (await http.delete("/api/schedule")).status == 200
        bridge.scheduled_charge.assert_not_awaited()
    finally: await http.close()


@pytest.mark.asyncio
async def test_cancel_unconfigured_and_disabled_draft_restore_cleanly(tmp_path):
    bridge = FakeBridge(tmp_path)
    scheduler = ChargeScheduler(bridge)
    await scheduler.disable()
    assert ChargeScheduler(bridge).status == "disabled"
    await scheduler.save(config(enabled=False), now=BEFORE)
    restored = ChargeScheduler(bridge)
    assert restored.status == "disabled" and not restored.jobs
    bridge.scheduled_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_cannot_arm_a_night_inside_the_safety_cooldown(tmp_path):
    bridge = FakeBridge(tmp_path)
    bridge.state = lambda: {"vehicles": bridge.cached, "cooldownUntil": "2026-11-07T16:10:00+00:00"}
    scheduler = ChargeScheduler(bridge)
    with pytest.raises(BridgeError, match="cooldown"):
        await scheduler.save(config(), now=BEFORE)
    await scheduler.save(config(date="2026-11-07"), now=BEFORE)
    bridge.scheduled_charge.assert_not_awaited()
