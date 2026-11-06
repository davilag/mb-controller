"""Transport/session tests use local fakes only; no Mercedes credentials or calls."""
import asyncio
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
            car._last_message_received += 1
        bridge.client.set_charge_limit = command
    async def socket():
        bridge.client.websocket._connection = SimpleNamespace(closed=False, close=AsyncMock())
        car.electric.max_soc = CarAttribute(initial, "VALID", 1)
        car._last_message_received = 1
        await asyncio.Event().wait()
    bridge.discover, bridge.run_socket = discover, socket
    result = await bridge.scheduled_charge(VIN, 80)
    assert result["status"] == expected
    assert sent == ([80] if initial == 50 else [])
    assert result.get("confirmedAt")
    assert bridge.client is None and bridge.runtime is None and bridge.task is None
    assert bridge.state()["vehicles"][0]["attributes"]["electric.max_soc"]["value"] == 80
