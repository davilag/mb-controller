import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import BasicAuth
from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio

from server.app import create_app
from server.bridge import Bridge, BridgeError, LocalClient, available_commands, serialize_car, charge_limit_options
from server.mbapi.car import Car, CarAttribute, Electric
from server.mbapi.proto import client_pb2, vehicle_events_pb2
from server.runtime import ConfigEntry, HomeAssistant, TokenStore, async_get_clientsession

VIN = "WDD00000000000001"


@pytest_asyncio.fixture
async def http(tmp_path):
    client = TestClient(TestServer(create_app(data_dir=tmp_path, password="local-test-password")))
    await client.start_server()
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_http_authentication_origin_and_validation(http):
    assert (await http.get("/api/health")).status == 200
    assert (await http.get("/api/state")).status == 401
    challenge = await http.get("/")
    assert challenge.status == 401
    assert challenge.headers["WWW-Authenticate"].startswith('Basic realm="MB Local"')
    for authorization in ["Basic invalid", "Bearer token", BasicAuth("wrong", "local-test-password").encode(), BasicAuth("admin", "bad").encode()]:
        assert (await http.get("/api/state", headers={"Authorization": authorization})).status == 401
    http.session.headers["Authorization"] = BasicAuth("admin", "local-test-password").encode()
    assert (await http.get("/api/state")).status == 200
    forbidden = await http.post("/api/account", json={}, headers={"Origin": "https://untrusted.example"})
    assert forbidden.status == 403
    bad_host = await http.get("/api/state", headers={"Host": "untrusted.example"})
    assert bad_host.status == 403
    assert (await http.post("/api/account", json={"email": "invalid"})).status == 400
    assert (await http.post(f"/api/vehicles/{VIN}/commands", json={"command": "unlock", "pin": "1234"})).status == 400
    assert (await http.post("/api/session", json={"password": "local-test-password"})).status == 405
    del http.session.headers["Authorization"]
    assert (await http.get("/api/state")).status == 401


@pytest.mark.asyncio
async def test_basic_auth_username_health_and_attempt_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "owner")
    client = TestClient(TestServer(create_app(data_dir=tmp_path, password="test-secret")))
    await client.start_server()
    try:
        good = {"Authorization": BasicAuth("owner", "test-secret").encode()}
        bad = {"Authorization": BasicAuth("admin", "test-secret").encode()}
        assert (await client.get("/api/state", headers=good)).status == 200
        assert (await client.post("/api/session", headers=good, json={})).status == 405
        for _ in range(10):
            assert (await client.get("/api/state", headers=bad)).status == 401
        blocked = await client.get("/api/state", headers=bad)
        assert blocked.status == 429
        assert blocked.headers["Retry-After"] == "300"
        assert (await client.get("/api/health", headers=bad)).status == 200
        assert (await client.get("/api/state", headers=good)).status == 200
        assert (await client.get("/", cookies={"mb_session": "old-cookie"})).status == 401
    finally:
        await client.close()


def test_token_encryption_and_delete(tmp_path):
    store = TokenStore(tmp_path)
    store.save({"token": {"refresh_token": "secret-test-refresh-token"}})
    assert b"secret-test-refresh-token" not in store.path.read_bytes()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.load()["token"]["refresh_token"] == "secret-test-refresh-token"
    assert TokenStore(tmp_path).load() == store.load()
    store.clear()
    assert not store.path.exists()


@pytest.mark.asyncio
async def test_real_upstream_unlock_protobuf_and_command_rejections(tmp_path):
    bridge = Bridge(tmp_path)
    await bridge.create_client({"region": "Europe"})
    client = bridge.client
    car = Car(VIN)
    car.features = {"DOORS_UNLOCK": True, "DOORS_LOCK": True}
    client.cars[VIN] = car
    original_socket = client.websocket
    send = AsyncMock()
    client.websocket = SimpleNamespace(_connection=SimpleNamespace(closed=False), call=send, connection_state="connected")
    try:
        with pytest.raises(BridgeError, match="four-digit"):
            await bridge.command(VIN, "unlock", "bad")
        with pytest.raises(BridgeError, match="Unknown"):
            await bridge.command(VIN, "engine_start")
        with pytest.raises(BridgeError, match="not enabled"):
            await bridge.command(VIN, "flash")
        result = await bridge.command(VIN, "unlock", "1234")
        assert result["state"] == "sent"
        payload = send.call_args.args[0]
        message = client_pb2.ClientMessage.FromString(payload)
        assert message.commandRequest.vin == VIN
        assert message.commandRequest.doors_unlock.pin == "1234"
        assert message.commandRequest.request_id == result["requestId"]
        assert "pin" not in str(bridge.state())
        with pytest.raises(BridgeError, match="few seconds"):
            await bridge.command(VIN, "lock")
        bridge.last_sent = 0
        client.websocket._connection.closed = True
        send.side_effect = BridgeError("Vehicle connection is offline")
        with pytest.raises(BridgeError, match="offline"):
            await bridge.command(VIN, "lock")
    finally:
        client.websocket = original_socket
        await bridge.stop()


def test_vehicle_attributes_and_subcapabilities():
    car = Car(VIN)
    car.electric = Electric()
    car.electric.soc = CarAttribute(74, "VALID", 1234, unit="PERCENT")
    car.features = {"SIGPOS_START": True}
    car.capabilities = {"commands": [{"commandName": "SIGPOS_START", "parameters": [{"parameterName": "SIGPOS_TYPE", "allowedEnums": ["HORN_ONLY"]}]}]}
    state = serialize_car(car)
    assert state["attributes"]["electric.soc"]["value"] == 74
    assert not next(c for c in state["commands"] if c["id"] == "flash")["available"]
    assert state["updatedAt"] is None


@pytest.mark.asyncio
async def test_upstream_vehicle_update_parsing_and_acknowledgment(tmp_path):
    bridge = Bridge(tmp_path)
    await bridge.create_client({"region": "Europe"})
    client = bridge.client
    car = Car(VIN)
    client.cars[VIN] = car
    client._on_dataload_complete = AsyncMock()
    # Find the upstream server envelope by its vepUpdates field.
    envelope = next(getattr(vehicle_events_pb2, name) for name, descriptor in vehicle_events_pb2.DESCRIPTOR.message_types_by_name.items() if "vepUpdates" in descriptor.fields_by_name)
    event = envelope()
    update = event.vepUpdates.updates[VIN]
    update.vin = VIN
    update.attributes["soc"].int_value = 74
    update.attributes["soc"].status = 0
    event.vepUpdates.sequence_number = 17
    try:
        acknowledgment = client.on_data(event)
        assert acknowledgment.acknowledge_vep_updates_by_vin.sequence_number == 17
        # Protobuf JSON represents int64 values as strings.
        assert str(serialize_car(car)["attributes"]["electric.soc"]["value"]) == "74"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,wire_field", [
    ("CHARGE_PROGRAM_CONFIGURE", "charge_program_configure"),
    ("CHARGING_CONFIGURE", "charging_configure"),
    ("BATTERY_MAX_SOC_CONFIGURE", "battery_max_soc"),
])
async def test_charge_limit_encodes_target_and_preserves_home_program(tmp_path, capability, wire_field):
    bridge = Bridge(tmp_path)
    await bridge.create_client({"region": "Europe"})
    client = bridge.client
    car = Car(VIN)
    car.features = {capability: True}
    car.capabilities = {"commands": [{"commandName": capability, "parameters": [{"parameterName": "MAX_SOC", "minValue": 50, "maxValue": 100}]}]}
    car.electric = Electric()
    car.electric.selectedChargeProgram = CarAttribute(2, "VALID", 1234)
    client.cars[VIN] = car
    original_socket = client.websocket
    send = AsyncMock()
    client.websocket = SimpleNamespace(_connection=SimpleNamespace(closed=False), call=send, connection_state="connected")
    try:
        for invalid in [None, True, 0, 49, 55, 101, "80", 80.0]:
            with pytest.raises(BridgeError, match="supported charge limit"):
                await bridge.command(VIN, "charge_limit", max_soc=invalid)
        send.assert_not_awaited()
        result = await bridge.command(VIN, "charge_limit", max_soc=80)
        message = client_pb2.ClientMessage.FromString(send.call_args.args[0])
        request = message.commandRequest
        assert request.WhichOneof("command") == wire_field
        assert request.request_id == result["requestId"]
        assert request.vin == VIN
        target = getattr(request, wire_field).max_soc
        assert (target if isinstance(target, int) else target.value) == 80
        if capability == "CHARGE_PROGRAM_CONFIGURE":
            assert request.charge_program_configure.charge_program == 2
            # Never silently switch Home to Standard when telemetry is missing.
            bridge.last_sent = 0
            car.electric.selectedChargeProgram.retrievalstatus = "NOT_RECEIVED"
            with pytest.raises(BridgeError, match="active charging program"):
                await bridge.command(VIN, "charge_limit", max_soc=90)
            assert send.await_count == 1
    finally:
        client.websocket = original_socket
        await bridge.stop()


def test_charge_limit_uses_advertised_bounds_and_requires_max_soc_support():
    car = Car(VIN)
    car.features = {"CHARGE_PROGRAM_CONFIGURE": True}
    assert charge_limit_options(car) == []
    car.capabilities = {"commands": [{"commandName": "CHARGE_PROGRAM_CONFIGURE", "parameters": [{"parameterName": "MAX_SOC", "minValue": 60, "maxValue": 90}]}]}
    assert charge_limit_options(car) == [60, 70, 80, 90]
    car.features["CHARGE_PROGRAM_CONFIGURE"] = False
    assert charge_limit_options(car) == []


@pytest.mark.asyncio
async def test_command_reconnects_from_idle_without_password_login(tmp_path):
    bridge = Bridge(tmp_path)
    await bridge.create_client({"region": "Europe"})
    client = bridge.client
    car = Car(VIN)
    car.features = {"DOORS_LOCK": True}
    client.cars[VIN] = car
    socket = client.websocket
    socket.is_stopping = True
    transport = SimpleNamespace(closed=False, send_bytes=AsyncMock(), close=AsyncMock())
    async def connect():
        socket._connection = transport
    socket.async_connect = AsyncMock(side_effect=connect)
    client.oauth.async_login_new = AsyncMock()
    try:
        result = await bridge.command(VIN, "lock")
        assert result["state"] == "sent"
        socket.async_connect.assert_awaited_once()
        transport.send_bytes.assert_awaited_once()
        client.oauth.async_login_new.assert_not_awaited()
        bridge.last_sent = 0
        socket.account_blocked = True
        with pytest.raises(BridgeError, match="rate limited"):
            await bridge.command(VIN, "lock")
        assert transport.send_bytes.await_count == 1
    finally:
        await bridge.stop()
