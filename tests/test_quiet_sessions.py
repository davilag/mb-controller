"""Transport/session tests use local fakes only; no Mercedes credentials or calls."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from server.bridge import Bridge, BridgeError
from server.mbapi import websocket as ws
from server.mbapi.car import Car, CarAttribute, Electric
from server.mbapi.errors import MercedesRateLimitError
from server.runtime import TokenStore

VIN = "WDD00000000000001"

@pytest.mark.asyncio
async def test_startup_and_reads_do_not_even_create_an_http_session(tmp_path, monkeypatch):
    TokenStore(tmp_path).save({"token": {"refresh_token": "fake"}})
    TokenStore(tmp_path, "vehicles.enc").save({"region": "Europe", "vehicles": [{"vin": VIN}]})
    monkeypatch.setattr(aiohttp.ClientSession, "__init__", Mock(side_effect=AssertionError("Unexpected network session")))
    bridge = Bridge(tmp_path)
    await bridge.restore()
    for _ in range(5):
        assert bridge.state()["intentionalIdle"]
        assert bridge.state()["configured"]
        assert bridge.client is None and bridge.task is None
    await bridge.stop()
    aiohttp.ClientSession.__init__.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, ConnectionResetError(), aiohttp.WSServerHandshakeError(None, (), status=429, headers={"Retry-After": "172800"})])
async def test_one_shot_socket_never_retries_relogs_or_arms_background_watchers(failure):
    socket = object.__new__(ws.Websocket)
    socket.single_attempt = True
    socket.is_stopping = False
    socket.account_blocked = False
    socket._websocket_handler = AsyncMock(side_effect=failure)
    socket._connection_event = Mock()
    socket.component_reload_watcher = SimpleNamespace(trigger=AsyncMock())
    socket._reconnectwatchdog = SimpleNamespace(trigger=AsyncMock())
    await socket._start_websocket_handler(SimpleNamespace(closed=False))
    socket._websocket_handler.assert_awaited_once()
    socket.component_reload_watcher.trigger.assert_not_awaited()
    socket._reconnectwatchdog.trigger.assert_not_awaited()
    assert socket.is_stopping
    assert socket.account_blocked == (getattr(failure, "status", None) == 429)
    if socket.account_blocked:
        import time
        assert socket.retry_after_until > time.time() + 172790

@pytest.mark.asyncio
async def test_closed_session_cannot_reconnect_even_for_ack_or_command():
    socket = object.__new__(ws.Websocket)
    socket.single_attempt, socket._unloaded, socket.is_stopping = True, False, False
    socket._connection = SimpleNamespace(closed=True)
    socket.async_connect = AsyncMock()
    for car_command in [True, False]:
        with pytest.raises(Exception, match="no automatic reconnect"):
            await socket.call(b"test", car_command=car_command)
    socket.async_connect.assert_not_awaited()

@pytest.mark.asyncio
async def test_cooldown_survives_restart_and_blocks_before_discovery(tmp_path):
    import time
    until = time.time() + 86400
    TokenStore(tmp_path, "connection-safety.enc").save({"cooldownUntil": until})
    for _ in range(2):
        bridge = Bridge(tmp_path)
        bridge.create_client = AsyncMock()
        await bridge.restore()
        with pytest.raises(BridgeError, match="cooldown"):
            await bridge.scheduled_charge(VIN, 80)
        with pytest.raises(BridgeError, match="cooldown"):
            await bridge.connect("fake@example.com", "unused", "Europe")
        bridge.create_client.assert_not_awaited()
        assert bridge.state()["accountBlocked"]

@pytest.mark.asyncio
async def test_rest_429_propagates_without_body_and_without_retry(tmp_path, monkeypatch):
    from server.mbapi import webapi
    count = 0
    async def limited(request):
        nonlocal count
        count += 1
        return web.Response(status=429, text="sensitive-provider-body", headers={"Retry-After": "172800"})
    app = web.Application(); app.router.add_get('/v1/config', limited)
    async with TestServer(app) as server:
        bridge = Bridge(tmp_path)
        await bridge.create_client({"region": "Europe"})
        client = bridge.client
        monkeypatch.setattr(webapi.helper, "Rest_url", lambda region: str(server.make_url('')).rstrip('/'))
        client.oauth.async_get_cached_token = AsyncMock(return_value={"access_token": "fake"})
        client.webapi._app_version.async_refresh = AsyncMock()
        try:
            with pytest.raises(MercedesRateLimitError) as error:
                await client.webapi.get_config()
            assert "sensitive-provider-body" not in str(error.value)
            assert count == 1
        finally: await bridge.stop()

@pytest.mark.asyncio
async def test_rest_rate_limit_in_session_persists_safety_pause(tmp_path):
    import time
    bridge = Bridge(tmp_path)
    bridge.store.save({"token": {"refresh_token": "fake"}})
    bridge.discover = AsyncMock(side_effect=MercedesRateLimitError("172800"))
    result = await bridge.scheduled_charge(VIN, 80)
    assert result["status"] == "failed"
    assert bridge.client is None and bridge.runtime is None
    assert Bridge(tmp_path).cooldown_until() > time.time() + 172790

@pytest.mark.asyncio
@pytest.mark.parametrize("initial,expected", [(50, "confirmed"), (80, "already_set")])
async def test_fresh_session_confirms_then_always_closes(tmp_path, initial, expected):
    bridge = Bridge(tmp_path)
    bridge.store.save({"token": {"refresh_token": "fake"}})
    car = Car(VIN); car.features = {"BATTERY_MAX_SOC_CONFIGURE": True}
    car.electric = Electric()
    sent = []
    async def discover():
        bridge.client.cars[VIN] = car
        async def command(vin, target):
            sent.append(target)
            bridge.client.sent_request = "fake-request"
            car.electric.max_soc.value = target
            car.electric.max_soc.timestamp = time.time() + .01
            car._last_message_received += 1
        bridge.client.set_charge_limit = command
    async def socket():
        bridge.client.websocket._connection = SimpleNamespace(closed=False, close=AsyncMock())
        car.electric.max_soc = CarAttribute(initial, "VALID", time.time())
        car._last_message_received = 1
        await asyncio.Event().wait()
    bridge.discover, bridge.run_socket = discover, socket
    result = await bridge.scheduled_charge(VIN, 80)
    assert result["status"] == expected
    assert sent == ([80] if initial == 50 else [])
    assert result.get("confirmedAt")
    assert bridge.client is None and bridge.runtime is None and bridge.task is None
    assert bridge.state()["vehicles"][0]["attributes"]["electric.max_soc"]["value"] == 80


@pytest.mark.asyncio
async def test_429_without_retry_after_has_no_invented_day_pause(tmp_path):
    bridge = Bridge(tmp_path)
    bridge.store.save({"token": {"refresh_token": "fake"}})
    bridge.discover = AsyncMock(side_effect=MercedesRateLimitError())
    result = await bridge.scheduled_charge(VIN, 80)
    assert result["status"] == "failed"
    assert "No cooldown duration was supplied" in result["message"]
    assert Bridge(tmp_path).cooldown_until() == 0
    assert bridge.client is None and bridge.task is None
    bridge.discover.assert_awaited_once()


@pytest.mark.asyncio
async def test_short_provider_retry_after_is_not_extended_to_a_day(tmp_path):
    import time
    bridge = Bridge(tmp_path)
    bridge.store.save({"token": {"refresh_token": "fake"}})
    bridge.discover = AsyncMock(side_effect=MercedesRateLimitError("600"))
    result = await bridge.scheduled_charge(VIN, 80)
    assert result["status"] == "failed"
    remaining = Bridge(tmp_path).cooldown_until() - time.time()
    assert 590 < remaining <= 600

async def attempt_fixture(tmp_path, monkeypatch, *, initial=50, stamp=None, response=None, update=None):
    import server.bridge as module
    monkeypatch.setattr(module, 'SESSION_SECONDS', .4)
    bridge = Bridge(tmp_path)
    bridge.store.save({'token': {'refresh_token': 'fake'}})
    car = Car(VIN); car.features = {'BATTERY_MAX_SOC_CONFIGURE': True}
    car.electric = Electric()
    commands = []
    async def discover():
        bridge.client.cars[VIN] = car
        async def command(vin, target):
            commands.append(target)
            bridge.client.sent_request = 'our-request'
            if response:
                bridge.client.command_observer(*response)
            if update:
                update(car)
        bridge.client.set_charge_limit = command
    async def socket():
        bridge.client.websocket._connection = SimpleNamespace(closed=False, close=AsyncMock())
        car.electric.max_soc = CarAttribute(initial, 'VALID', stamp if stamp is not None else time.time())
        car._last_message_received = 1
        await asyncio.Event().wait()
    bridge.discover, bridge.run_socket = discover, socket
    return bridge, commands

@pytest.mark.asyncio
async def test_stale_matching_value_is_not_already_set(tmp_path, monkeypatch):
    bridge, commands = await attempt_fixture(tmp_path, monkeypatch, initial=80, stamp=time.time()-86400)
    result = await bridge.scheduled_charge(VIN, 80)
    assert commands == []
    assert result['status'] == 'failed' and result['sentAt'] is None
    assert result['diagnostics']['stage'] == 'fresh_matching_telemetry'
    assert result['diagnostics']['failureCategory'] == 'timeout'
    assert 'stale' in result['message']
    assert bridge.client is None

@pytest.mark.asyncio
async def test_acknowledged_command_without_new_limit_is_not_confirmed(tmp_path, monkeypatch):
    bridge, commands = await attempt_fixture(tmp_path, monkeypatch, response=('our-request','FINISHED',None))
    result = await bridge.scheduled_charge(VIN, 80)
    assert commands == [80] and result['sentAt']
    assert result['status'] == 'failed'
    assert result['diagnostics']['stage'] == 'confirmation'
    assert any(e['event']=='command_response' for e in result['diagnostics']['events'])

@pytest.mark.asyncio
async def test_rejection_is_recorded_and_stops_without_retry(tmp_path, monkeypatch):
    bridge, commands = await attempt_fixture(tmp_path, monkeypatch, response=('our-request','FAILED','VEHICLE_UNREACHABLE'))
    result = await bridge.scheduled_charge(VIN,80)
    assert commands == [80]
    assert result['diagnostics']['failureCategory'] == 'command_rejected'
    assert 'rejected' in result['message']
    assert result['diagnostics']['events'][1]['code'] == 'VEHICLE_UNREACHABLE'

@pytest.mark.asyncio
async def test_unrelated_response_and_old_matching_update_do_not_confirm(tmp_path, monkeypatch):
    def update(car):
        car.electric.max_soc.value = 80
        car.electric.max_soc.timestamp = time.time()-86400
        car._last_message_received += 1
    bridge, commands = await attempt_fixture(tmp_path, monkeypatch,
        response=('other-request','FAILED','UNRELATED'), update=update)
    result = await bridge.scheduled_charge(VIN,80)
    assert commands == [80] and result['status']=='failed'
    assert not any(e['event']=='command_response' for e in result['diagnostics']['events'])
    assert result['diagnostics']['lastLimit']==80

@pytest.mark.asyncio
async def test_exception_details_do_not_leak_secret_text(tmp_path, monkeypatch):
    bridge, _ = await attempt_fixture(tmp_path, monkeypatch)
    bridge.discover = AsyncMock(side_effect=ValueError('SECRET_TOKEN_AND_VIN'))
    result = await bridge.scheduled_charge(VIN,80)
    assert result['diagnostics']['exceptionType']=='ValueError'
    assert result['diagnostics']['stage']=='discovery'
    assert 'SECRET_TOKEN_AND_VIN' not in str(result)


def test_batched_command_responses_deliver_each_request(monkeypatch):
    import json
    import server.mbapi.client as module
    from server.bridge import LocalClient
    car = Car(VIN)
    car.publish_updates = Mock()
    client = object.__new__(LocalClient)
    client.cars = {VIN: car}
    client._write_debug_output = Mock()
    client.command_observer = Mock()
    payload = {'apptwin_command_status_updates_by_vin': {'updates_by_vin': {VIN: {
        'vin': VIN, 'updates_by_pid': {
            'unrelated': {'state': 'FINISHED'},
            'our-request': {'state': 'FAILED', 'errors': [{'code': 'VEHICLE_OFFLINE', 'message':'private provider text'}]}
        }}}}}
    monkeypatch.setattr(module, 'MessageToJson', lambda *a, **k: json.dumps(payload))
    client._process_apptwin_command_status_updates_by_vin(None)
    client.command_observer.assert_any_call('our-request', 'FAILED', 'VEHICLE_OFFLINE')
    assert client.command_observer.call_count == 2
