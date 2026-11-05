import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType

from server.connection_diagnostics import ConnectionDiagnostics
from server.mbapi.websocket import Websocket


def test_bounded_evidence_and_no_unapproved_fields(caplog):
    evidence = ConnectionDiagnostics()
    evidence.record('connected')
    evidence.received()
    evidence.record('idle_timeout')
    item = evidence.snapshot()['events'][-1]
    assert item['messagesReceived'] == 1
    assert item['silenceSeconds'] >= 0
    with pytest.raises(TypeError):
        evidence.record('network_error', token='do-not-log')
    with pytest.raises(ValueError):
        evidence.record('do-not-log')
    assert 'do-not-log' not in caplog.text
    for _ in range(50):
        evidence.record('remote_close', code=1000)
    assert len(evidence.snapshot()['events']) == 40


@pytest.mark.asyncio
async def test_server_close_evidence_excludes_raw_reason():
    socket = object.__new__(Websocket)
    socket._region = 'Europe'
    socket.is_stopping = False
    socket._LOGGER = Mock()
    socket._blocked_since_time = None
    socket.diagnostics = ConnectionDiagnostics()
    socket._watchdog = SimpleNamespace(trigger=AsyncMock())
    socket._websocket_connection_headers = AsyncMock(return_value={})
    connection = SimpleNamespace(closed=False, close_code=1001,
        receive=AsyncMock(return_value=SimpleNamespace(type=WSMsgType.CLOSE, data=1001, extra='private server reason')))
    await socket._websocket_handler(SimpleNamespace(ws_connect=AsyncMock(return_value=connection)))
    events = socket.diagnostics.snapshot()['events']
    assert [event['event'] for event in events] == ['connected', 'remote_close']
    assert events[-1]['code'] == 1001
    assert 'private server reason' not in str(events)


@pytest.mark.asyncio
async def test_idle_watchdog_records_local_cause_before_shutdown():
    socket = object.__new__(Websocket)
    socket._ignition_states = {}
    socket._watchdog = Mock()
    socket._reconnectwatchdog = Mock()
    socket._graceful_shutdown_and_optionally_reconnect = AsyncMock()
    await socket.initiatiate_connection_disconnect_with_reconnect()
    await socket._shutdown_task
    assert socket.diagnostics.snapshot()['events'][-1]['event'] == 'idle_timeout'


def test_new_connection_resets_silence_baseline(monkeypatch):
    evidence = ConnectionDiagnostics()
    monkeypatch.setattr('server.connection_diagnostics.time.time', lambda: 1000)
    evidence.record('connected')
    evidence.received()
    monkeypatch.setattr('server.connection_diagnostics.time.time', lambda: 1600)
    evidence.record('connected')
    monkeypatch.setattr('server.connection_diagnostics.time.time', lambda: 1630)
    evidence.record('idle_timeout')
    assert evidence.snapshot()['events'][-1]['silenceSeconds'] == 30
